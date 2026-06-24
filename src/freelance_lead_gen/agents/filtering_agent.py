"""Filtering and scoring of discovered opportunities.

The :class:`FilteringPipeline` takes raw discovered leads, scores them
through a combination of rule-based heuristics and LLM-based classification,
and outputs qualified leads with scores and reasoning attached.
"""

from __future__ import annotations as _annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field

from freelance_lead_gen.agents.profile_matcher import (
    ProfileMatcher,
    TargetProfile,
)
from freelance_lead_gen.config.prompts import QUALIFICATION_PROMPT
from freelance_lead_gen.config.settings import Settings, get_settings
from freelance_lead_gen.llm import LLMClient
from freelance_lead_gen.models.opportunity import (
    LeadOpportunity,
    LeadScoringResult,
    LeadStatus,
)
from freelance_lead_gen.storage.repository import OpportunityRepository

logger = structlog.get_logger(__name__)

# ── Tier thresholds ────────────────────────────────────────────────────────────


class ScoringThresholds(BaseModel):
    """Score boundaries for each qualification tier.

    Opportunities are classified as HIGH, POTENTIAL, or LOW based on their
    overall score compared to these thresholds.
    """

    high: int = Field(default=75, ge=0, le=100, description="Minimum score for HIGH tier.")
    potential: int = Field(default=50, ge=0, le=100, description="Minimum score for POTENTIAL tier.")
    low: int = Field(default=0, ge=0, le=100, description="Below this is LOW (typically 0).")

    @property
    def tiers(self) -> list[tuple[str, int]]:
        """Return the thresholds as ordered (label, min_score) pairs."""
        return [
            ("HIGH", self.high),
            ("POTENTIAL", self.potential),
            ("LOW", self.low),
        ]


# ── LLM Classification Schema ──────────────────────────────────────────────────


class _LLMClassification(BaseModel):
    """Structured output schema for the LLM classification call."""

    qualified: bool
    score: int = Field(..., ge=0, le=100)
    skill_match_score: int = Field(default=50, ge=0, le=100)
    budget_fit_score: int = Field(default=50, ge=0, le=100)
    clarity_score: int = Field(default=50, ge=0, le=100)
    reasoning: str
    risks: list[str] = Field(default_factory=list)


# ── Concurrency control ────────────────────────────────────────────────────────


_SEMAPHORE: asyncio.Semaphore | None = None


def _get_semaphore(max_concurrent: int = 5) -> asyncio.Semaphore:
    """Return a module-level semaphore for limiting concurrent LLM calls."""
    global _SEMAPHORE  # noqa: PLW0603
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(max_concurrent)
    return _SEMAPHORE


# ── Pipeline Report ────────────────────────────────────────────────────────────


@dataclass
class FilteringReport:
    """Summary of a filtering pipeline run."""

    total_input: int = 0
    """Number of opportunities received."""
    high_count: int = 0
    """Number classified as HIGH."""
    potential_count: int = 0
    """Number classified as POTENTIAL."""
    low_count: int = 0
    """Number classified as LOW."""
    disqualified_count: int = 0
    """Number disqualified by rule-based filters."""
    errors: int = 0
    """Number of opportunities that errored during scoring."""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When filtering started."""
    completed_at: datetime | None = None
    """When filtering completed."""

    @property
    def elapsed_seconds(self) -> float | None:
        """Return elapsed seconds, or *None* if not completed."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()

    @property
    def qualified_count(self) -> int:
        """Number of opportunities that passed filtering (HIGH + POTENTIAL)."""
        return self.high_count + self.potential_count


# ── Filtering Pipeline ─────────────────────────────────────────────────────────


class FilteringPipeline:
    """Scores and filters discovered opportunities.

    Combines **rule-based** scoring (via :class:`ProfileMatcher`) with
    **LLM-based** classification (via :class:`LLMClient` using the
    :data:`~freelance_lead_gen.config.prompts.QUALIFICATION_PROMPT`) to
    produce a final qualification decision for each opportunity.

    Parameters
    ----------
    profile : TargetProfile or None
        Target profile for matching.  Defaults to
        :meth:`TargetProfile.default`.
    llm_client : LLMClient or None
        Client for LLM classification.  Created with defaults if not provided.
    repository : OpportunityRepository or None
        Repository for persisting scoring results.  Created with defaults
        if not provided.
    thresholds : ScoringThresholds or None
        Score tier boundaries.  Uses defaults if not provided.
    settings : Settings or None
        Application settings.

    """

    def __init__(
        self,
        *,
        profile: TargetProfile | None = None,
        llm_client: LLMClient | None = None,
        repository: OpportunityRepository | None = None,
        thresholds: ScoringThresholds | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings: Settings = settings or get_settings()

        self._profile_matcher: ProfileMatcher = ProfileMatcher(profile=profile)
        self._llm: LLMClient = llm_client or LLMClient(settings=self._settings)
        self._repository: OpportunityRepository = repository or OpportunityRepository()
        self._thresholds: ScoringThresholds = thresholds or ScoringThresholds()

        self._stats: dict[str, int] = {
            "runs": 0,
            "total_scored": 0,
            "total_high": 0,
            "total_potential": 0,
            "total_low": 0,
            "total_disqualified": 0,
            "total_errors": 0,
        }

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        """Return a copy of lifetime statistics."""
        return dict(self._stats)

    @property
    def thresholds(self) -> ScoringThresholds:
        """Current scoring thresholds."""
        return self._thresholds

    # ── Public API ───────────────────────────────────────────────────────

    async def run(
        self,
        opportunities: list[LeadOpportunity],
        *,
        use_llm: bool = True,
        persist: bool = True,
    ) -> tuple[list[LeadOpportunity], FilteringReport]:
        """Run the full filtering pipeline on a batch of opportunities.

        Each opportunity goes through:

        1. **Rule-based disqualification** — excluded keywords, missing data.
        2. **Profile matching** — multi-dimensional score via
           :class:`ProfileMatcher`.
        3. **LLM classification** (if *use_llm*) — semantic evaluation.
        4. **Tier assignment** — HIGH / POTENTIAL / LOW based on thresholds.

        Parameters
        ----------
        opportunities : list of LeadOpportunity
            Discovered opportunities to score and filter.
        use_llm : bool
            Whether to use LLM-based classification in addition to
            rule-based scoring (default *True*).
        persist : bool
            Whether to persist scoring results to the database (default
            *True*).

        Returns
        -------
        tuple of (list of LeadOpportunity, FilteringReport)
            The first element is the list of *qualified* opportunities
            (HIGH + POTENTIAL tiers), with their scores and reasoning
            populated.  The second is a detailed run report.

        """
        report = FilteringReport()
        report.total_input = len(opportunities)

        logger.info(
            "filtering.run_starting",
            count=len(opportunities),
            use_llm=use_llm,
        )

        qualified: list[LeadOpportunity] = []
        # Track non-disqualified opportunities and their LLM tasks together.
        llm_targets: list[tuple[LeadOpportunity, int]] = []  # (opp, rule_score)

        for opp in opportunities:
            # ── Step 1: Rule-based disqualification ──────────────────────
            score_result = self._profile_matcher.score_opportunity(opp)

            if score_result["disqualified"]:
                opp.status = LeadStatus.REJECTED
                opp.score = 0
                opp.notes = score_result["disqualification_reason"]
                report.disqualified_count += 1
                if persist:
                    await self._persist_result(opp)
                logger.info(
                    "filtering.disqualified",
                    opportunity_id=opp.id,
                    reason=score_result["disqualification_reason"],
                )
                continue

            # ── Step 2: Assign rule-based scores ─────────────────────────
            rule_score = score_result["overall_score"]
            opp.score = rule_score

            if use_llm:
                llm_targets.append((opp, rule_score))
            else:
                tier = self._assign_tier(rule_score)
                opp.status = LeadStatus.QUALIFIED if tier != "LOW" else LeadStatus.REJECTED
                opp.notes = f"Rule-based score: {rule_score} ({tier})"
                if opp.status == LeadStatus.QUALIFIED:
                    qualified.append(opp)
                if persist:
                    await self._persist_result(opp)

        # ── Await all LLM classifications ────────────────────────────────
        if use_llm and llm_targets:
            sem = _get_semaphore(max_concurrent=5)
            async with sem:
                llm_results = await asyncio.gather(
                    *[self._classify_with_llm(opp) for opp, _ in llm_targets],
                    return_exceptions=True,
                )

            for (opp, rule_score), result_or_error in zip(llm_targets, llm_results, strict=False):
                if isinstance(result_or_error, Exception):
                    logger.error(
                        "filtering.llm_classify_error",
                        opportunity_id=opp.id,
                        error=str(result_or_error),
                    )
                    report.errors += 1
                    tier = self._assign_tier(rule_score)
                elif result_or_error is not None:
                    llm_result: _LLMClassification = result_or_error
                    blended = self._blend_scores(
                        rule_score=rule_score,
                        llm_result=llm_result,
                    )
                    opp.score = blended["score"]
                    opp.notes = (
                        f"LLM: {llm_result.reasoning}"
                    )
                    if llm_result.risks:
                        opp.notes += f" | Risks: {'; '.join(llm_result.risks)}"
                    tier = self._assign_tier(blended["score"])
                else:
                    tier = self._assign_tier(rule_score)

                if tier == "LOW":
                    opp.status = LeadStatus.REJECTED
                    report.low_count += 1
                else:
                    opp.status = LeadStatus.QUALIFIED
                    qualified.append(opp)

                if persist:
                    await self._persist_result(opp)

        # ── Tally report ─────────────────────────────────────────────────
        for opp in opportunities:
            if opp.status == LeadStatus.REJECTED:
                tier = self._assign_tier(opp.score or 0) if opp.score else "LOW"
                if opp.score == 0 or tier == "LOW":
                    report.low_count += 1
            elif opp.status == LeadStatus.QUALIFIED:
                tier = self._assign_tier(opp.score or 50)
                if tier == "HIGH":
                    report.high_count += 1
                elif tier == "POTENTIAL":
                    report.potential_count += 1
                else:
                    report.low_count += 1

        report.completed_at = datetime.now(UTC)

        # Update lifetime stats.
        self._stats["runs"] += 1
        self._stats["total_scored"] += report.total_input
        self._stats["total_high"] += report.high_count
        self._stats["total_potential"] += report.potential_count
        self._stats["total_low"] += report.low_count
        self._stats["total_disqualified"] += report.disqualified_count
        self._stats["total_errors"] += report.errors

        logger.info(
            "filtering.run_completed",
            input=report.total_input,
            high=report.high_count,
            potential=report.potential_count,
            low=report.low_count,
            disqualified=report.disqualified_count,
            errors=report.errors,
            elapsed_seconds=report.elapsed_seconds,
        )

        return qualified, report

    async def score_opportunity(
        self,
        opportunity: LeadOpportunity,
        profile: TargetProfile | None = None,
    ) -> LeadScoringResult:
        """Score a single opportunity and return a structured result.

        This is a convenience method for scoring individual opportunities
        outside a batch pipeline run.

        Parameters
        ----------
        opportunity : LeadOpportunity
            The opportunity to score.
        profile : TargetProfile or None
            Optional override profile.  Uses the pipeline's profile if not
            provided.

        Returns
        -------
        LeadScoringResult
            The structured scoring result.

        """
        matcher = ProfileMatcher(profile=profile) if profile else self._profile_matcher
        scores = matcher.score_opportunity(opportunity)

        # Get LLM classification.
        try:
            llm_class = await self._classify_with_llm(opportunity)
        except Exception as exc:
            logger.warning(
                "filtering.llm_fallback",
                opportunity_id=opportunity.id,
                error=str(exc),
            )
            llm_class = None

        if llm_class is not None:
            blended = self._blend_scores(
                rule_score=scores["overall_score"],
                llm_result=llm_class,
            )
        else:
            blended = {"score": scores["overall_score"], "skill_match_score": scores["skill_match_score"]}

        return LeadScoringResult(
            qualified=scores["overall_score"] >= self._thresholds.potential,
            score=blended["score"],
            skill_match_score=blended.get("skill_match_score", scores["skill_match_score"]),
            budget_fit_score=scores["budget_fit_score"],
            clarity_score=0,  # Clarity not computed in rule-based pass.
            reasoning=scores.get("diagnostics", {}).get("matched_skills", "No detail"),
            risks=llm_class.risks if llm_class else [],
        )

    def set_thresholds(self, thresholds: ScoringThresholds) -> None:
        """Update the scoring thresholds.

        Parameters
        ----------
        thresholds : ScoringThresholds
            New threshold configuration.

        """
        self._thresholds = thresholds
        logger.info("filtering.thresholds_updated", thresholds=thresholds.model_dump())

    # ── Internal methods ─────────────────────────────────────────────────

    async def _classify_with_llm(
        self,
        opportunity: LeadOpportunity,
    ) -> _LLMClassification | None:
        """Classify a single opportunity using the LLM.

        Returns ``None`` if the LLM call fails entirely.
        """
        try:
            user_content = _build_classification_input(opportunity)
            result = await self._llm.structured_classify(
                system_prompt=QUALIFICATION_PROMPT,
                user_content=user_content,
                response_model=_LLMClassification,
                temperature=0.3,
            )
            return _LLMClassification(**result)
        except Exception as exc:
            logger.exception(
                "filtering.llm_classify_failed",
                opportunity_id=opportunity.id,
                error=str(exc),
            )
            return None

    def _assign_tier(self, score: int) -> str:
        """Assign an opportunity to HIGH, POTENTIAL, or LOW."""
        if score >= self._thresholds.high:
            return "HIGH"
        if score >= self._thresholds.potential:
            return "POTENTIAL"
        return "LOW"

    def _blend_scores(
        self,
        rule_score: int,
        llm_result: _LLMClassification,
    ) -> dict[str, Any]:
        """Blend rule-based scores with LLM classification results.

        Uses a 40/60 split: LLM result gets more weight for the final score
        since it has semantic understanding, but the rule score provides a
        grounded baseline.
        """
        llm_score = llm_result.score
        blended_score = round(rule_score * 0.4 + llm_score * 0.6)
        blended_score = max(0, min(100, blended_score))

        return {
            "score": blended_score,
            "skill_match_score": llm_result.skill_match_score,
            "reasoning": llm_result.reasoning,
        }

    async def _persist_result(self, opportunity: LeadOpportunity) -> None:
        """Persist scoring results to the database."""
        try:
            await self._repository.update(opportunity)
        except Exception as exc:
            logger.warning(
                "filtering.persist_failed",
                opportunity_id=opportunity.id,
                error=str(exc),
            )


# ── Input builder ──────────────────────────────────────────────────────────────


def _build_classification_input(opportunity: LeadOpportunity) -> str:
    """Build the user-content string for LLM classification.

    Parameters
    ----------
    opportunity : LeadOpportunity
        The opportunity to describe.

    Returns
    -------
    str
        Formatted input for the LLM.

    """
    parts = [
        f"Title: {opportunity.title}",
        f"Platform: {opportunity.platform}",
    ]
    if opportunity.company:
        parts.append(f"Company: {opportunity.company}")
    if opportunity.budget_min is not None or opportunity.budget_max is not None:
        budget_parts = []
        if opportunity.budget_min is not None:
            budget_parts.append(f"${opportunity.budget_min:.0f}")
        if opportunity.budget_max is not None:
            budget_parts.append(f"${opportunity.budget_max:.0f}")
        parts.append(f"Budget: {'-'.join(budget_parts)} {opportunity.currency}")
    if opportunity.skills:
        parts.append(f"Skills: {', '.join(opportunity.skills)}")
    if opportunity.location:
        parts.append(f"Location: {opportunity.location}")
    parts.append(f"---\n{opportunity.description}")

    return "\n".join(parts)
