"""Pipeline orchestrator — central controller for the lead generation lifecycle.

The :class:`LeadGenOrchestrator` manages the full pipeline as an async state
machine, coordinating discovery, filtering, personalisation, human review,
and completion phases.  It handles graceful shutdown, per-phase error recovery,
and comprehensive statistics collection.
"""

from __future__ import annotations as _annotations

import asyncio
import contextlib
import signal
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from freelance_lead_gen.agents.filtering_agent import (
    FilteringPipeline,
)
from freelance_lead_gen.agents.personalization_agent import (
    PersonalizationAgent,
)
from freelance_lead_gen.agents.profile_matcher import TargetProfile
from freelance_lead_gen.agents.verification_agent import (
    VerificationAgent,
    VerificationResult,
)
from freelance_lead_gen.config.settings import Settings, get_settings
from freelance_lead_gen.llm import LLMClient
from freelance_lead_gen.models.opportunity import (
    LeadOpportunity,
    LeadStatus,
    OutboundDraft,
)
from freelance_lead_gen.models.pipeline import PipelineState
from freelance_lead_gen.storage.repository import OpportunityRepository

if TYPE_CHECKING:
    from freelance_lead_gen.discovery.discovery_agent import DiscoveryAgent

logger = structlog.get_logger(__name__)


# ── Pipeline Phase Enum ────────────────────────────────────────────────────────


class PipelinePhase(StrEnum):
    """Human-readable phase labels for the orchestration pipeline."""

    DISCOVERY = "discovery"
    FILTERING = "filtering"
    PERSONALIZATION = "personalization"
    VERIFICATION = "verification"
    HITL_REVIEW = "hitl_review"
    COMPLETION = "completion"


# ── Orchestrator Report ────────────────────────────────────────────────────────


@dataclass
class OrchestratorReport:
    """Aggregated report from a full pipeline run."""

    success: bool = False
    """Whether the pipeline completed without fatal errors."""

    phases_completed: list[str] = field(default_factory=list)
    """Phases that completed successfully (in order)."""

    phases_failed: list[str] = field(default_factory=list)
    """Phases that encountered errors."""

    total_discovered: int = 0
    """Total opportunities discovered (or provided as input)."""

    total_qualified: int = 0
    """Opportunities that passed filtering."""

    total_drafted: int = 0
    """Opportunities with successful drafts."""

    total_verified_pass: int = 0
    """Drafts that passed verification."""

    total_verified_fail: int = 0
    """Drafts that failed verification."""

    total_reviewed: int = 0
    """Drafts that were human-reviewed and approved."""

    total_errors: int = 0
    """Total non-fatal errors across all phases."""

    errors: list[dict[str, str]] = field(default_factory=list)
    """Error details (phase, opportunity_id, message)."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When the pipeline run started."""

    completed_at: datetime | None = None
    """When the pipeline run completed."""

    @property
    def elapsed_seconds(self) -> float | None:
        """Return elapsed seconds, or *None* if not completed."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def summary(self) -> dict[str, Any]:
        """Return a human-readable summary dict."""
        return {
            "success": self.success,
            "phases_completed": self.phases_completed,
            "phases_failed": self.phases_failed,
            "discovered": self.total_discovered,
            "qualified": self.total_qualified,
            "drafted": self.total_drafted,
            "verified_pass": self.total_verified_pass,
            "verified_fail": self.total_verified_fail,
            "reviewed": self.total_reviewed,
            "errors": self.total_errors,
            "elapsed_seconds": self.elapsed_seconds,
        }


# ── LeadGen Orchestrator ───────────────────────────────────────────────────────


class LeadGenOrchestrator:
    """Central pipeline orchestrator for the freelance lead generation system.

    Manages the full lifecycle of lead processing through five phases:

    1. **Discovery** — finds new opportunities via :class:`DiscoveryAgent`.
    2. **Filtering** — scores and qualifies opportunities via
       :class:`FilteringPipeline`.
    3. **Personalisation** — generates outreach drafts via
       :class:`PersonalizationAgent`.
    4. **Verification** — quality-checks drafts via :class:`VerificationAgent`.
    5. **HITL Review** — presents drafts for human approval.
    6. **Completion** — finalises results and collects statistics.

    The orchestrator supports graceful shutdown (SIGINT/SIGTERM), per-phase
    error recovery (partial completion), and comprehensive stats reporting.

    Parameters
    ----------
    settings : Settings or None
        Application settings.  Loaded from environment if not provided.
    discovery_agent : DiscoveryAgent or None
        Agent for opportunity discovery.
    filtering_pipeline : FilteringPipeline or None
        Pipeline for scoring and filtering.
    personalization_agent : PersonalizationAgent or None
        Agent for generating outreach drafts.
    verification_agent : VerificationAgent or None
        Agent for verifying draft quality.
    repository : OpportunityRepository or None
        Repository for persistence.
    llm_client : LLMClient or None
        Shared LLM client for all agents.

    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        discovery_agent: DiscoveryAgent | None = None,
        filtering_pipeline: FilteringPipeline | None = None,
        personalization_agent: PersonalizationAgent | None = None,
        verification_agent: VerificationAgent | None = None,
        repository: OpportunityRepository | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._settings: Settings = settings or get_settings()

        # Shared LLM client.
        self._llm: LLMClient = llm_client or LLMClient(settings=self._settings)

        # Phase agents.
        self._discovery: DiscoveryAgent | None = discovery_agent
        self._filtering: FilteringPipeline = filtering_pipeline or FilteringPipeline(
            llm_client=self._llm,
            settings=self._settings,
        )
        self._personalization: PersonalizationAgent = personalization_agent or PersonalizationAgent(
            llm_client=self._llm,
            settings=self._settings,
        )
        self._verification: VerificationAgent = verification_agent or VerificationAgent(
            llm_client=self._llm,
            settings=self._settings,
        )
        self._repository: OpportunityRepository = repository or OpportunityRepository()

        # Shutdown coordination.
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._is_running: bool = False

        # Lifetime statistics.
        self._stats: dict[str, Any] = {
            "runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "total_discovered": 0,
            "total_qualified": 0,
            "total_drafted": 0,
            "total_errors": 0,
            "first_run_at": None,
            "last_run_at": None,
        }

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Return a copy of lifetime statistics."""
        return dict(self._stats)

    @property
    def is_running(self) -> bool:
        """Return *True* if a pipeline run is currently in progress."""
        return self._is_running

    @property
    def shutdown_requested(self) -> bool:
        """Return *True* if a graceful shutdown has been requested."""
        return self._shutdown_event.is_set()

    # ── Public API ───────────────────────────────────────────────────────

    async def run_full_pipeline(
        self,
        *,
        opportunities: list[LeadOpportunity] | None = None,
        run_discovery: bool = True,
        run_filtering: bool = True,
        run_personalization: bool = True,
        run_verification: bool = True,
        run_hitl: bool | None = None,
        profile: TargetProfile | None = None,
    ) -> OrchestratorReport:
        """Execute the complete pipeline from discovery through to completion.

        Parameters
        ----------
        opportunities : list of LeadOpportunity or None
            Pre-discovered opportunities to process.  When provided and
            *run_discovery* is *True*, these are used instead of running
            the discovery agent.
        run_discovery : bool
            Whether to run the discovery phase (default *True*).
        run_filtering : bool
            Whether to run the filtering phase (default *True*).
        run_personalization : bool
            Whether to run the personalisation phase (default *True*).
        run_verification : bool
            Whether to run the verification phase (default *True*).
        run_hitl : bool or None
            Whether to wait for human review.  ``None`` (default) uses
            the setting from ``settings.hitl.enabled``.
        profile : TargetProfile or None
            Profile to use for matching.  Uses defaults if not provided.

        Returns
        -------
        OrchestratorReport
            Aggregated report covering all phases.

        """
        if self._is_running:
            msg = "Pipeline is already running"
            raise RuntimeError(msg)

        self._is_running = True
        self._shutdown_event.clear()

        if profile is None:
            profile = TargetProfile.default()

        report = OrchestratorReport()
        report.started_at = datetime.now(UTC)

        hitl_enabled = (
            run_hitl if run_hitl is not None else self._settings.hitl.enabled
        )

        try:
            # ── Phase 1: Discovery ───────────────────────────────────────
            current_opps: list[LeadOpportunity] = []
            if run_discovery:
                current_opps = await self._run_discovery_phase(report)

            # Use provided opportunities (or merge with discovered).
            if opportunities:
                # Deduplicate by id.
                seen_ids = {o.id for o in current_opps}
                for opp in opportunities:
                    if opp.id not in seen_ids:
                        current_opps.append(opp)
                        seen_ids.add(opp.id)
                report.total_discovered += len(opportunities)

            if not current_opps:
                logger.info("orchestrator.no_opportunities")
                report.success = True
                report.completed_at = datetime.now(UTC)
                return report

            # ── Phase 2: Filtering ───────────────────────────────────────
            if self._check_shutdown(report):
                return report

            qualified: list[LeadOpportunity] = []
            if run_filtering:
                qualified = await self._run_filtering_phase(
                    current_opps, profile, report
                )
            else:
                qualified = current_opps

            if not qualified:
                logger.info("orchestrator.no_qualified_opportunities")
                report.success = True
                report.completed_at = datetime.now(UTC)
                return report

            # ── Phase 3: Personalisation ─────────────────────────────────
            if self._check_shutdown(report):
                return report

            drafts: list[OutboundDraft] = []
            if run_personalization:
                drafts = await self._run_personalization_phase(
                    qualified, profile, report
                )
            else:
                # Create minimal drafts even without generation.
                for opp in qualified:
                    draft = OutboundDraft(opportunity_id=opp.id)
                    draft.add_version("(draft generation skipped)")
                    with contextlib.suppress(Exception):
                        await self._repository.create_draft(draft)
                    drafts.append(draft)

            if not drafts:
                logger.info("orchestrator.no_drafts_generated")
                report.success = True
                report.completed_at = datetime.now(UTC)
                return report

            # ── Phase 4: Verification ────────────────────────────────────
            if self._check_shutdown(report):
                return report

            verified_drafts: list[tuple[OutboundDraft, VerificationResult]] = []
            if run_verification:
                verified_drafts = await self._run_verification_phase(
                    drafts, report
                )
            else:
                # Bypass verification — every draft passes.
                for d in drafts:
                    verified_drafts.append((
                        d,
                        VerificationResult(
                            passed=True,
                            score=100,
                            word_count=len((d.current_body or "").split()),
                            paragraph_count=len(
                                [p for p in (d.current_body or "").split("\n\n") if p.strip()]
                            ),
                        ),
                    ))

            # ── Phase 5: HITL Review ─────────────────────────────────────
            if self._check_shutdown(report):
                return report

            if hitl_enabled and verified_drafts:
                await self._run_hitl_phase(verified_drafts, report)
            elif verified_drafts:
                # Auto-approve.
                for draft, _ in verified_drafts:
                    draft.approve()
                    with contextlib.suppress(Exception):
                        await self._repository.update_draft(draft)
                    report.total_reviewed += 1

            # ── Completion ───────────────────────────────────────────────
            report.phases_completed.append(PipelinePhase.COMPLETION)
            report.success = True

        except asyncio.CancelledError:
            logger.info("orchestrator.pipeline_cancelled")
            report.phases_failed.append("cancelled")
            report.success = False

        except Exception as exc:
            logger.error("orchestrator.pipeline_fatal_error", error=str(exc), exc_info=True)
            report.phases_failed.append("fatal")
            report.total_errors += 1
            report.errors.append({
                "phase": "global",
                "opportunity_id": "",
                "message": str(exc),
            })
            report.success = False

        finally:
            self._is_running = False
            report.completed_at = datetime.now(UTC)

            # Update lifetime stats.
            self._stats["runs"] += 1
            if report.success:
                self._stats["successful_runs"] += 1
            else:
                self._stats["failed_runs"] += 1
            self._stats["total_discovered"] += report.total_discovered
            self._stats["total_qualified"] += report.total_qualified
            self._stats["total_drafted"] += report.total_drafted
            self._stats["total_errors"] += report.total_errors
            if self._stats["first_run_at"] is None:
                self._stats["first_run_at"] = report.started_at.isoformat()
            self._stats["last_run_at"] = report.completed_at.isoformat()

            logger.info(
                "orchestrator.pipeline_completed",
                success=report.success,
                phases_completed=report.phases_completed,
                phases_failed=report.phases_failed,
                discovered=report.total_discovered,
                qualified=report.total_qualified,
                drafted=report.total_drafted,
                reviewed=report.total_reviewed,
                errors=report.total_errors,
                elapsed_seconds=report.elapsed_seconds,
            )

        return report

    async def run_phase(
        self,
        phase: str,
        *,
        opportunities: list[LeadOpportunity] | None = None,
        profile: TargetProfile | None = None,
    ) -> Any:
        """Run a single pipeline phase in isolation.

        Useful for testing or when only one phase needs to execute.

        Parameters
        ----------
        phase : str
            One of ``"discovery"``, ``"filtering"``, ``"personalization"``,
            ``"verification"``, ``"hitl"``.
        opportunities : list of LeadOpportunity or None
            Input opportunities for phases that need them.
        profile : TargetProfile or None
            Profile for filtering/personalisation.

        Returns
        -------
        Any
            Phase-specific result:

            - ``"discovery"``: list of LeadOpportunity
            - ``"filtering"``: tuple of (list of LeadOpportunity, FilteringReport)
            - ``"personalization"``: tuple of (list of OutboundDraft, PersonalizationReport)
            - ``"verification"``: list of (OutboundDraft, VerificationResult) tuples
            - ``"hitl"``: None

        """
        if profile is None:
            profile = TargetProfile.default()

        if phase == "discovery":
            report = OrchestratorReport()
            return await self._run_discovery_phase(report)

        if phase == "filtering":
            if not opportunities:
                msg = "opportunities are required for the filtering phase"
                raise ValueError(msg)
            report = OrchestratorReport()
            return await self._run_filtering_phase(opportunities, profile, report)

        if phase == "personalization":
            if not opportunities:
                msg = "opportunities are required for the personalization phase"
                raise ValueError(msg)
            report = OrchestratorReport()
            return await self._run_personalization_phase(opportunities, profile, report)

        if phase == "verification":
            if not opportunities:
                msg = "drafts are required for the verification phase"
                raise ValueError(msg)
            report = OrchestratorReport()
            return await self._run_verification_phase(opportunities, report)

        if phase == "hitl":
            if not opportunities:
                msg = "verified draft tuples are required for the hitl phase"
                raise ValueError(msg)
            report = OrchestratorReport()
            return await self._run_hitl_phase(opportunities, report)

        msg = f"Unknown phase: {phase!r}"
        raise ValueError(msg)

    async def initialize(self) -> None:
        """Prepare all agents for pipeline execution.

        Call once at application startup before :meth:`run_full_pipeline`.
        """
        logger.info("orchestrator.initialising")

        if self._discovery is not None:
            await self._discovery.initialize()

        logger.info("orchestrator.initialised")

    async def shutdown(self) -> None:
        """Gracefully shut down all agents and release resources.

        Sets the shutdown flag so that running phases can exit early.
        """
        logger.info("orchestrator.shutting_down")
        self._shutdown_event.set()

        if self._discovery is not None:
            try:
                await self._discovery.shutdown()
            except Exception as exc:
                logger.warning("orchestrator.discovery_shutdown_error", error=str(exc))

        await self._llm.close()

        logger.info("orchestrator.shutdown_complete")

    def setup_signal_handlers(self) -> None:
        """Configure signal handlers for graceful shutdown.

        Registers handlers for SIGINT (Ctrl+C) and SIGTERM that trigger
        the shutdown event.  Should be called once at startup.
        """
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            sig_name = sig.name  # type: ignore[attr-defined]

            def _handler(s: signal.Signals = sig, name: str = sig_name) -> None:  # type: ignore[assignment]
                if self._is_running:
                    logger.info("orchestrator.shutdown_signal_received", signal=name)
                    self._shutdown_event.set()
                else:
                    # Not running — exit immediately.
                    logger.info("orchestrator.exit_signal_received", signal=name)
                    sys.exit(0)

            try:
                loop.add_signal_handler(sig, _handler)
            except NotImplementedError:
                # Windows or non-main-thread — fall back to standard signal.
                signal.signal(sig, lambda _sig, _frame: _handler())  # type: ignore[arg-type]

    # ── Phase Implementations ────────────────────────────────────────────

    async def _run_discovery_phase(
        self,
        report: OrchestratorReport,
    ) -> list[LeadOpportunity]:
        """Execute the discovery phase.

        If a :class:`DiscoveryAgent` is configured, runs one discovery cycle.
        Otherwise returns an empty list.
        """
        report.phases_completed.append(PipelinePhase.DISCOVERY)
        logger.info("orchestrator.phase_discovery_starting")

        if self._discovery is None:
            logger.info("orchestrator.discovery_skipped_no_agent")
            return []

        # Capture the timestamp *before* discovery so we can retrieve exactly
        # the opportunities created in this cycle, rather than relying on
        # the DISCOVERED status (which may include stale rows from earlier
        # cycles that were never processed).
        cycle_start_time = datetime.now(UTC)

        try:
            cycle_report = await self._discovery.run_discovery_cycle()
            report.total_discovered = cycle_report.total_new
            report.total_errors += cycle_report.total_errors

            # Load the newly discovered opportunities by created_at range.
            discovered = await self._repository.search(
                date_from=cycle_start_time,
                limit=cycle_report.total_new or 100,
            )

            logger.info(
                "orchestrator.phase_discovery_completed",
                found=cycle_report.total_found,
                new=cycle_report.total_new,
                errors=cycle_report.total_errors,
                elapsed_seconds=cycle_report.elapsed_seconds,
            )

            return discovered

        except Exception as exc:
            report.phases_failed.append(PipelinePhase.DISCOVERY)
            report.total_errors += 1
            report.errors.append({
                "phase": PipelinePhase.DISCOVERY,
                "opportunity_id": "",
                "message": str(exc),
            })
            logger.error(
                "orchestrator.phase_discovery_failed",
                error=str(exc),
                exc_info=True,
            )
            return []

    async def _run_filtering_phase(
        self,
        opportunities: list[LeadOpportunity],
        profile: TargetProfile,
        report: OrchestratorReport,
    ) -> list[LeadOpportunity]:
        """Execute the filtering/qualification phase."""
        report.phases_completed.append(PipelinePhase.FILTERING)
        logger.info(
            "orchestrator.phase_filtering_starting",
            count=len(opportunities),
        )

        try:
            qualified, filter_report = await self._filtering.run(
                opportunities,
                use_llm=True,
                persist=True,
            )

            report.total_qualified = len(qualified)
            report.total_errors += filter_report.errors

            for opp in qualified:
                self._update_pipeline_context(opp, PipelineState.QUALIFIED)

            logger.info(
                "orchestrator.phase_filtering_completed",
                input=len(opportunities),
                qualified=len(qualified),
                high=filter_report.high_count,
                potential=filter_report.potential_count,
                errors=filter_report.errors,
            )

            return qualified

        except Exception as exc:
            report.phases_failed.append(PipelinePhase.FILTERING)
            report.total_errors += 1
            report.errors.append({
                "phase": PipelinePhase.FILTERING,
                "opportunity_id": "",
                "message": str(exc),
            })
            logger.error(
                "orchestrator.phase_filtering_failed",
                error=str(exc),
                exc_info=True,
            )
            return opportunities  # Return everything as best-effort.

    async def _run_personalization_phase(
        self,
        qualified: list[LeadOpportunity],
        profile: TargetProfile,
        report: OrchestratorReport,
    ) -> list[OutboundDraft]:
        """Execute the personalisation/drafting phase."""
        report.phases_completed.append(PipelinePhase.PERSONALIZATION)
        logger.info(
            "orchestrator.phase_personalization_starting",
            count=len(qualified),
        )

        drafts: list[OutboundDraft] = []
        sem = asyncio.Semaphore(5)  # Limit concurrent LLM calls.

        async def _draft_one(opp: LeadOpportunity) -> tuple[LeadOpportunity, OutboundDraft] | None:
            """Generate a draft for one opportunity, returning ``None`` on failure."""
            async with sem:
                if self._check_shutdown(report):
                    return None
                try:
                    draft = await self._personalization.generate_draft(
                        opp,
                        profile,
                        max_retries_on_quality=2,
                    )

                    opp.status = LeadStatus.DRAFTED

                    logger.debug(
                        "orchestrator.draft_created",
                        opportunity_id=opp.id,
                        draft_id=draft.id,
                    )
                    return (opp, draft)

                except Exception as exc:
                    report.total_errors += 1
                    report.errors.append({
                        "phase": PipelinePhase.PERSONALIZATION,
                        "opportunity_id": opp.id,
                        "message": str(exc),
                    })
                    logger.warning(
                        "orchestrator.draft_failed",
                        opportunity_id=opp.id,
                        error=str(exc),
                    )
                    return None  # Partial completion — continue with other opps.

        tasks = [_draft_one(opp) for opp in qualified]
        results = await asyncio.gather(*tasks)

        for opp_result in results:
            if opp_result is None:
                continue
            _opp, draft = opp_result
            drafts.append(draft)
            report.total_drafted += 1

        logger.info(
            "orchestrator.phase_personalization_completed",
            attempted=len(qualified),
            drafted=len(drafts),
            errors=report.total_errors,
        )

        return drafts

    async def _run_verification_phase(
        self,
        drafts: list[OutboundDraft],
        report: OrchestratorReport,
    ) -> list[tuple[OutboundDraft, VerificationResult]]:
        """Execute the verification phase."""
        report.phases_completed.append(PipelinePhase.VERIFICATION)
        logger.info(
            "orchestrator.phase_verification_starting",
            count=len(drafts),
        )

        verified: list[tuple[OutboundDraft, VerificationResult]] = []
        sem = asyncio.Semaphore(5)  # Limit concurrent LLM calls.

        async def _verify_one(draft: OutboundDraft) -> tuple[OutboundDraft, VerificationResult] | None:
            async with sem:
                try:
                    result = await self._verification.verify(draft, use_llm=False)
                    return (draft, result)
                except Exception as exc:
                    report.total_errors += 1
                    report.errors.append({
                        "phase": PipelinePhase.VERIFICATION,
                        "opportunity_id": draft.opportunity_id,
                        "message": str(exc),
                    })
                    logger.warning(
                        "orchestrator.verification_failed",
                        draft_id=draft.id,
                        error=str(exc),
                    )
                    return None

        tasks = [_verify_one(d) for d in drafts]
        results = await asyncio.gather(*tasks)

        for result in results:
            if result is not None:
                verified.append(result)
                _draft, v_result = result
                if v_result.passed:
                    report.total_verified_pass += 1
                else:
                    report.total_verified_fail += 1

        logger.info(
            "orchestrator.phase_verification_completed",
            total=len(drafts),
            passed=report.total_verified_pass,
            failed=report.total_verified_fail,
        )

        return verified

    async def _run_hitl_phase(
        self,
        verified_drafts: list[tuple[OutboundDraft, VerificationResult]],
        report: OrchestratorReport,
    ) -> None:
        """Execute the human-in-the-loop review phase.

        When HITL is enabled, marks drafts as awaiting review and stores
        them for an external UI to present.  If auto-approve is on, approves
        passing drafts immediately.
        """
        report.phases_completed.append(PipelinePhase.HITL_REVIEW)
        auto_approve = self._settings.hitl.auto_approve

        if auto_approve:
            logger.info("orchestrator.hitl_auto_approve_enabled")
            for draft, v_result in verified_drafts:
                if v_result.passed:
                    draft.approve()
                    with contextlib.suppress(Exception):
                        await self._repository.update_draft(draft)
                    report.total_reviewed += 1
        else:
            logger.info(
                "orchestrator.hitl_awaiting_review",
                count=len(verified_drafts),
            )
            for draft, v_result in verified_drafts:
                if v_result.passed:
                    # Update the opportunity status to indicate it's awaiting
                    # human review.  The external UI will pick these up.
                    with contextlib.suppress(Exception):
                        await self._repository.update_draft(draft)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _check_shutdown(self, report: OrchestratorReport) -> bool:
        """Check if shutdown was requested and record the interruption.

        Returns *True* if the pipeline should stop.
        """
        if self._shutdown_event.is_set():
            logger.info("orchestrator.shutdown_detected_halting_phase")
            report.phases_failed.append("shutdown")
            # completed_at is set by the finally block in run_full_pipeline.
            return True
        return False

    def _update_pipeline_context(
        self,
        opportunity: LeadOpportunity,
        state: PipelineState,
    ) -> None:
        """Create or update a pipeline context for an opportunity.

        Currently a no-op that logs the transition; full context tracking
        is available via the :class:`PipelineContext` model when needed.
        """
        logger.debug(
            "orchestrator.pipeline_transition",
            opportunity_id=opportunity.id,
            state=state.value,
            score=opportunity.score,
        )

    def get_report_snapshot(self, report: OrchestratorReport) -> dict[str, Any]:
        """Return a JSON-safe snapshot of a pipeline run report.

        Parameters
        ----------
        report : OrchestratorReport
            The report to snapshot.

        Returns
        -------
        dict
            JSON-serialisable summary.

        """
        return {
            "success": report.success,
            "phases_completed": list(report.phases_completed),
            "phases_failed": list(report.phases_failed),
            "total_discovered": report.total_discovered,
            "total_qualified": report.total_qualified,
            "total_drafted": report.total_drafted,
            "total_verified_pass": report.total_verified_pass,
            "total_verified_fail": report.total_verified_fail,
            "total_reviewed": report.total_reviewed,
            "total_errors": report.total_errors,
            "started_at": report.started_at.isoformat(),
            "completed_at": report.completed_at.isoformat() if report.completed_at else None,
            "elapsed_seconds": report.elapsed_seconds,
        }
