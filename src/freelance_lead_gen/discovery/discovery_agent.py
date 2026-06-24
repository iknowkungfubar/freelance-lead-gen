"""Discovery agent — orchestrates the full discovery pipeline.

The :class:`DiscoveryAgent` ties together the browser, platform extractors,
and repository to run complete discovery cycles: iterating enabled platforms,
dereplicating results, persisting new opportunities, and collecting
statistics.
"""

from __future__ import annotations as _annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from freelance_lead_gen.config.settings import Settings, get_settings
from freelance_lead_gen.discovery.browser import ManagedBrowser
from freelance_lead_gen.discovery.platforms import PLATFORM_EXTRACTORS
from freelance_lead_gen.discovery.scheduler import DiscoveryScheduler
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus
from freelance_lead_gen.storage.repository import OpportunityRepository

if TYPE_CHECKING:
    from freelance_lead_gen.discovery.extractor import RawLead
    from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor

logger = structlog.get_logger(__name__)

# ── Retry constants ────────────────────────────────────────────────────────────

_MAX_RETRIES: int = 3
"""Number of retry attempts per platform when extraction fails."""

_RETRY_BACKOFF_BASE: float = 2.0
"""Exponential backoff base (seconds) between retries."""

_RETRY_BACKOFF_JITTER: float = 1.0
"""Random jitter (seconds) added to backoff."""


# ── Cycle report dataclass ─────────────────────────────────────────────────────


@dataclass
class DiscoveryCycleReport:
    """Summary of a single discovery cycle run.

    Returned by :meth:`DiscoveryAgent.run_discovery_cycle`.
    """

    total_searched: int = 0
    """Number of platform searches executed."""

    total_found: int = 0
    """Number of raw leads extracted across all platforms."""

    total_new: int = 0
    """Number of new (previously unseen) opportunities persisted."""

    total_errors: int = 0
    """Number of platform errors encountered during the cycle."""

    platforms_attempted: int = 0
    """Number of platforms that were attempted."""

    platforms_succeeded: int = 0
    """Number of platforms that completed without error."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """When this cycle started."""

    completed_at: datetime | None = None
    """When this cycle completed."""

    per_platform: dict[str, dict[str, int]] = field(default_factory=dict)
    """Per-platform breakdown of results."""

    errors: list[dict[str, str]] = field(default_factory=list)
    """Error details (platform, message) for any failed platforms."""

    @property
    def elapsed_seconds(self) -> float | None:
        """Return elapsed seconds for this cycle, or *None* if not complete."""
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


# ── Discovery Agent ───────────────────────────────────────────────────────────


class DiscoveryAgent:
    """Orchestrator for the full discovery pipeline.

    The agent manages the lifecycle of a discovery run:

    1. Iterates enabled platforms (in random order to diversify patterns).
    2. For each platform, runs the configured extractor.
    3. Deduplicates results against the database via the repository.
    4. Persists new :class:`LeadOpportunity` records.
    5. Collects cycle statistics.

    Parameters
    ----------
    settings : Settings or None
        Application settings.  Loaded from the environment if not provided.
    managed_browser : ManagedBrowser or None
        A browser instance for Playwright-based platforms.  Created with
        defaults if not provided.
    platform_extractors : dict of str -> BasePlatformExtractor or None
        Mapping of platform names to extractor instances.  When ``None``,
        the agent builds extractors from the registry.
    repository : OpportunityRepository or None
        Repository for persistence.  Created with defaults if not provided.
    search_queries : list of str or None
        Search terms to use.  Defaults to those from settings.

    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        managed_browser: ManagedBrowser | None = None,
        platform_extractors: dict[str, BasePlatformExtractor] | None = None,
        repository: OpportunityRepository | None = None,
        search_queries: list[str] | None = None,
    ) -> None:
        self._settings = settings or get_settings()

        self._browser = managed_browser
        self._repository = repository or OpportunityRepository()

        # Resolve search queries.
        self._search_queries = (
            search_queries
            if search_queries is not None
            else self._settings.discovery.queries
        )

        # Resolve platform extractors.
        self._platform_extractors: dict[str, BasePlatformExtractor] = {}
        if platform_extractors is not None:
            self._platform_extractors = platform_extractors

        # Track enabled platforms from settings.
        self._enabled_platforms: list[str] = self._settings.platforms.enabled_list

        self._stats: dict[str, Any] = {
            "cycles": 0,
            "total_found": 0,
            "total_new": 0,
            "total_errors": 0,
            "started_at": None,
        }

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Lifetime statistics across all cycles."""
        return dict(self._stats)

    @property
    def enabled_platforms(self) -> list[str]:
        """List of currently enabled platform names."""
        return list(self._enabled_platforms)

    # ── Public API ──────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Prepare the agent for discovery runs.

        Ensures the browser is started and all platform extractors are
        initialised.  Call once before :meth:`run_discovery_cycle`.
        """
        logger.info("discovery_agent.initialising")

        # Start the browser if it hasn't been started and we need it.
        if self._browser is None:
            self._browser = ManagedBrowser(
                headless=self._settings.browser.headless,
                user_data_dir=self._settings.browser.user_data_dir,
            )

        if not self._browser.is_running:
            await self._browser.start()

        # Resolve extractors for any enabled platforms that don't have one.
        for platform_name in self._enabled_platforms:
            if platform_name not in self._platform_extractors:
                extractor_cls = PLATFORM_EXTRACTORS.get(platform_name)
                if extractor_cls is None:
                    logger.warning(
                        "discovery_agent.no_extractor",
                        platform=platform_name,
                    )
                    continue

                # Create the extractor.  For browser-based extractors, pass the
                # browser.  For HTTP-based extractors (RemoteOK, YCWork), they
                # accept browser only for fallback.
                try:
                    extractor = extractor_cls(
                        browser=self._browser,
                        credentials={},
                        settings=self._settings,
                    )
                    self._platform_extractors[platform_name] = extractor
                except Exception as exc:
                    logger.exception(
                        "discovery_agent.extractor_init_failed",
                        platform=platform_name,
                        error=str(exc),
                    )

        logger.info(
            "discovery_agent.initialised",
            platforms=list(self._platform_extractors.keys()),
            queries=self._search_queries,
        )

    async def shutdown(self) -> None:
        """Clean up resources — stop the browser if we started it."""
        logger.info("discovery_agent.shutting_down")

        if self._browser is not None and self._browser.is_running:
            await self._browser.stop()

    async def run_discovery_cycle(
        self,
        *,
        platforms: list[str] | None = None,
        specific_queries: list[str] | None = None,
    ) -> DiscoveryCycleReport:
        """Execute one complete discovery cycle.

        Iterates the enabled (or specified) platforms, runs their extractors,
        deduplicates results, persists new opportunities, and returns a report.

        Parameters
        ----------
        platforms : list of str or None
            Subset of platforms to run.  When ``None``, runs all enabled
            platforms from settings.
        specific_queries : list of str or None
            Search queries for this cycle.  When ``None``, uses the
            configured default queries.

        Returns
        -------
        DiscoveryCycleReport
            Structured report with per-platform breakdowns.

        """
        report = DiscoveryCycleReport()
        report.started_at = datetime.now(UTC)

        targets = platforms or self._enabled_platforms

        # Shuffle the platform order to diversify traffic patterns.
        shuffled = list(targets)
        random.shuffle(shuffled)

        queries = specific_queries or self._search_queries

        logger.info(
            "discovery_agent.cycle_starting",
            platforms=shuffled,
            queries=queries,
        )

        for platform_name in shuffled:
            extractor = self._platform_extractors.get(platform_name)
            if extractor is None:
                logger.warning(
                    "discovery_agent.skipping_platform_no_extractor",
                    platform=platform_name,
                )
                continue

            report.platforms_attempted += 1

            try:
                platform_result = await self._run_platform_extraction(
                    platform_name=platform_name,
                    extractor=extractor,
                    queries=queries,
                )

                report.total_found += platform_result["found"]
                report.total_new += platform_result["new"]
                report.platforms_succeeded += 1

                report.per_platform[platform_name] = {
                    "found": platform_result["found"],
                    "new": platform_result["new"],
                    "failed": platform_result.get("failed", 0),
                    "searched": platform_result.get("searched", 0),
                }

                logger.info(
                    "discovery_agent.platform_completed",
                    platform=platform_name,
                    found=platform_result["found"],
                    new=platform_result["new"],
                )

            except Exception as exc:
                report.total_errors += 1
                report.errors.append({
                    "platform": platform_name,
                    "error": str(exc),
                })

                report.per_platform[platform_name] = {
                    "found": 0,
                    "new": 0,
                    "failed": 1,
                    "error": str(exc),
                }

                logger.error(
                    "discovery_agent.platform_failed",
                    platform=platform_name,
                    error=str(exc),
                    exc_info=True,
                )
                # Continue to next platform — graceful degradation.

        # Update lifetime stats.
        self._stats["cycles"] += 1
        self._stats["total_found"] += report.total_found
        self._stats["total_new"] += report.total_new
        self._stats["total_errors"] += report.total_errors
        if self._stats["started_at"] is None:
            self._stats["started_at"] = report.started_at

        report.completed_at = datetime.now(UTC)

        logger.info(
            "discovery_agent.cycle_completed",
            attempted=report.platforms_attempted,
            succeeded=report.platforms_succeeded,
            found=report.total_found,
            new=report.total_new,
            errors=report.total_errors,
            elapsed_seconds=report.elapsed_seconds,
        )

        return report

    # ── Platform extraction (with retry) ────────────────────────────────

    async def _run_platform_extraction(
        self,
        platform_name: str,
        extractor: BasePlatformExtractor,
        queries: list[str],
    ) -> dict[str, int]:
        """Run extraction for a single platform across all search queries.

        Implements retry with exponential backoff.

        Returns
        -------
        dict with keys: ``found``, ``new``, ``searched``, ``failed``.

        """
        total_found = 0
        total_new = 0
        searched = 0
        errors = 0

        for query in queries:
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    raw_leads = await extractor.extract_listings_raw(query=query)
                    searched += 1

                    # Deduplicate and persist.
                    new_count = await self._persist_raw_leads(
                        platform_name=platform_name,
                        raw_leads=raw_leads,
                    )

                    total_found += len(raw_leads)
                    total_new += new_count

                    logger.debug(
                        "discovery_agent.query_completed",
                        platform=platform_name,
                        query=query,
                        found=len(raw_leads),
                        new=new_count,
                    )
                    break  # Success — move to next query.

                except Exception as exc:
                    errors += 1
                    if attempt < _MAX_RETRIES:
                        backoff = (
                            _RETRY_BACKOFF_BASE ** attempt
                            + random.uniform(0, _RETRY_BACKOFF_JITTER)
                        )
                        logger.warning(
                            "discovery_agent.retry",
                            platform=platform_name,
                            query=query,
                            attempt=attempt,
                            max_retries=_MAX_RETRIES,
                            backoff_seconds=round(backoff, 1),
                            error=str(exc),
                        )
                        await asyncio.sleep(backoff)
                    else:
                        logger.exception(
                            "discovery_agent.retry_exhausted",
                            platform=platform_name,
                            query=query,
                            error=str(exc),
                        )

            # Inter-query delay to avoid bursts.
            if query != queries[-1]:
                await asyncio.sleep(random.uniform(2.0, 5.0))

        return {
            "found": total_found,
            "new": total_new,
            "searched": searched,
            "failed": errors,
        }

    # ── Persistence and deduplication ───────────────────────────────────

    async def _persist_raw_leads(
        self,
        platform_name: str,
        raw_leads: list[RawLead],
    ) -> int:
        """Convert raw leads to opportunities and persist new ones.

        Uses :meth:`OpportunityRepository.upsert` which handles
        deduplication by ``(platform, platform_job_id)``.

        Parameters
        ----------
        platform_name : str
            The source platform.
        raw_leads : list of RawLead
            Raw leads to persist.

        Returns
        -------
        int
            Number of *new* (previously unseen) opportunities created.

        """
        new_count = 0

        for raw in raw_leads:
            try:
                # Build a LeadOpportunity from the raw lead.
                opportunity = self._raw_lead_to_opportunity(platform_name, raw)

                # Persist via upsert (deduplication by platform + platform_job_id).
                persisted = await self._repository.upsert(opportunity)

                # The upsert returns the DB record.  If the `created_at`
                # field is very recent, it's a new insert (rough heuristic).
                # A more precise approach would be to check the row count
                # from the upsert, but SQLite's ON CONFLICT makes that
                # platform-dependent.  Instead, check if the `score` stayed
                # None (was just inserted) or has a value (was an update).
                # Better: query before/after — but that's expensive at scale.
                # We use a simple heuristic: if status is DISCOVERED and
                # there's no score, it was likely inserted fresh.
                if persisted.status == LeadStatus.DISCOVERED and persisted.score is None:
                    new_count += 1

            except Exception as exc:
                logger.exception(
                    "discovery_agent.persist_failed",
                    platform=platform_name,
                    title=raw.title[:60] if raw.title else "?",
                    error=str(exc),
                )

        return new_count

    # ── RawLead → LeadOpportunity conversion ─────────────────────────────

    def _raw_lead_to_opportunity(
        self,
        platform_name: str,
        raw: RawLead,
    ) -> LeadOpportunity:
        """Convert a :class:`RawLead` to a :class:`LeadOpportunity` domain model.

        Parameters
        ----------
        platform_name : str
            Override platform name (from the extractor, not from raw).
        raw : RawLead
            The extracted raw lead.

        Returns
        -------
        LeadOpportunity

        """
        # Parse date from the raw posted_date string if present.
        posted_date = None
        if raw.posted_date:
            try:
                posted_date = datetime.fromisoformat(raw.posted_date)
            except (ValueError, TypeError):
                posted_date = None

        return LeadOpportunity(
            platform=platform_name,
            platform_job_id=raw.platform_job_id,
            title=raw.title,
            company=raw.company,
            description=raw.description,
            budget_min=raw.budget_min,
            budget_max=raw.budget_max,
            currency=raw.currency,
            skills=raw.skills,
            posted_date=posted_date,
            url=raw.url,
            location=raw.location,
            status=LeadStatus.DISCOVERED,
            raw_data={
                "raw_title": raw.title,
                "raw_company": raw.company,
                "raw_description": raw.description,
                "raw_url": raw.url,
                "extracted_at": raw.extracted_at,
            },
        )

    # ── Scheduler factory ───────────────────────────────────────────────

    def create_scheduler(
        self,
        *,
        daily_cap: int | None = None,
    ) -> DiscoveryScheduler:
        """Create a :class:`DiscoveryScheduler` configured to run cycles via
        this agent.

        Parameters
        ----------
        daily_cap : int or None
            Daily opportunity cap.  Defaults to the settings value (50).

        Returns
        -------
        DiscoveryScheduler
            A pre-configured scheduler linked to this agent.

        """
        cap = daily_cap or self._settings.discovery.max_daily

        scheduler = DiscoveryScheduler(
            discovery_fn=self.run_discovery_cycle,
            daily_cap=cap,
        )

        # Register platforms with platform-appropriate intervals.
        platform_intervals: dict[str, int] = {
            "upwork": 60,
            "linkedin": 120,
            "freelancer": 90,
            "remote_ok": 180,
            "yc_work": 360,
        }

        for platform_name in self._enabled_platforms:
            interval = platform_intervals.get(platform_name, 120)
            scheduler.add_platform(
                platform_name,
                interval_minutes=interval,
            )

        return scheduler
