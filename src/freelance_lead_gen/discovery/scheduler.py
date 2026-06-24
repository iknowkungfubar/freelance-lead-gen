"""Discovery scheduler — orchestrated cron-based discovery runs with rate limiting.

Uses APScheduler to manage per-platform discovery schedules, enforces a daily
cap on total opportunities, and distributes interactions across the day to
avoid burst patterns that trigger rate limiting.
"""

from __future__ import annotations as _annotations

import asyncio
import random
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_MAX_DAILY_OPPORTUNITIES: int = 50
"""Hard cap on total opportunities processed per day across all platforms."""

_DISCOVERY_WINDOW_HOURS: int = 12
"""Spread discovery activity across this many hours per day."""

_BASE_JITTER_SECONDS: int = 300
"""Random jitter (0–300 s) applied to the first scheduled run to desynchronise."""

_DEFAULT_INTERVAL_MINUTES: int = 60
"""Default interval between discovery rounds (60 minutes)."""


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class PlatformSchedule:
    """Schedule configuration for a single platform."""

    platform_name: str
    """Lowercase platform name."""

    interval_minutes: int = _DEFAULT_INTERVAL_MINUTES
    """How often to run discovery for this platform."""

    max_per_round: int = 10
    """Maximum leads to extract per round for this platform."""

    enabled: bool = True
    """If ``False``, the platform is skipped during discovery cycles."""

    last_run: datetime | None = None
    """When this platform was last processed."""

    total_found: int = 0
    """Running count of leads found for this platform in the current day."""

    consecutive_failures: int = 0
    """Consecutive discovery failures for this platform."""

    max_consecutive_failures: int = 5
    """Auto-disable after this many consecutive failures."""


@dataclass
class SchedulerStats:
    """Aggregate statistics for the discovery scheduler."""

    total_runs: int = 0
    """Total discovery cycles executed."""

    total_leads: int = 0
    """Total leads found across all cycles."""

    total_new: int = 0
    """Total new (previously unseen) leads."""

    total_failures: int = 0
    """Total failed discovery rounds."""

    started_at: datetime | None = None
    """When the scheduler was started."""

    platform_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    """Per-platform stats keyed by platform name."""


# ── Scheduler ──────────────────────────────────────────────────────────────────


class DiscoveryScheduler:
    """Scheduled discovery runner that coordinates per-platform extraction
    with daily cap enforcement and distributed timing.

    The scheduler uses APScheduler's ``AsyncIOScheduler`` to run discovery
    rounds at configurable intervals.  It spreads platform interactions across
    a 12-hour window to avoid burst patterns that trigger rate limiting.

    Parameters
    ----------
    discovery_fn : Callable or None
        Async callable that performs a full discovery cycle.  Signature:
        ``async def fn(platforms: list[str]) -> dict[str, dict[str, int]]``.
        If ``None``, no discovery is executed (for testing).
    daily_cap : int
        Maximum opportunities to process per day (default 50).
    window_hours : int
        Hours over which to spread discovery (default 12).

    """

    def __init__(
        self,
        discovery_fn: Callable[[list[str]], Any] | None = None,
        *,
        daily_cap: int = _MAX_DAILY_OPPORTUNITIES,
        window_hours: int = _DISCOVERY_WINDOW_HOURS,
    ) -> None:
        self._discovery_fn = discovery_fn

        # Core settings.
        self._daily_cap = daily_cap
        self._window_hours = window_hours

        # Scheduler state.
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler()
        self._platforms: dict[str, PlatformSchedule] = {}
        self._stats = SchedulerStats()
        self._running: bool = False
        self._daily_reset_time: datetime | None = None
        self._cycle_lock = asyncio.Lock()

        # Graceful shutdown signal handling.
        self._shutdown_event = asyncio.Event()
        self._shutdown_task: asyncio.Task | None = None

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the scheduler is currently active."""
        return self._running

    @property
    def stats(self) -> SchedulerStats:
        """Current aggregate statistics."""
        return self._stats

    def get_platform_schedule(self, platform: str) -> PlatformSchedule | None:
        """Return the schedule config for *platform*, or ``None`` if not registered."""
        return self._platforms.get(platform)

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of scheduler status for monitoring / display.

        Returns
        -------
        dict
            Includes: running, platforms, stats, daily_cap, etc.

        """
        return {
            "running": self._running,
            "daily_cap": self._daily_cap,
            "window_hours": self._window_hours,
            "started_at": self._stats.started_at.isoformat() if self._stats.started_at else None,
            "total_runs": self._stats.total_runs,
            "total_leads": self._stats.total_leads,
            "total_new": self._stats.total_new,
            "total_failures": self._stats.total_failures,
            "platforms": {
                name: {
                    "enabled": ps.enabled,
                    "interval_minutes": ps.interval_minutes,
                    "last_run": ps.last_run.isoformat() if ps.last_run else None,
                    "total_found": ps.total_found,
                    "consecutive_failures": ps.consecutive_failures,
                }
                for name, ps in self._platforms.items()
            },
        }

    @property
    def health_status(self) -> dict[str, Any]:
        """Return health metrics snapshot for monitoring / heartbeats.

        Returns
        -------
        dict
            Includes: running, total_cycles, total_leads, total_errors,
            per_platform breakdown, consecutive_failures, auto_disabled
            platforms, and last_cycle_at timestamp.

        """
        return {
            "running": self._running,
            "total_cycles": self._stats.total_runs,
            "total_leads": self._stats.total_leads,
            "total_errors": self._stats.total_failures,
            "per_platform": {
                name: {
                    "enabled": ps.enabled,
                    "last_run": ps.last_run.isoformat() if ps.last_run else None,
                    "total_found": ps.total_found,
                    "consecutive_failures": ps.consecutive_failures,
                }
                for name, ps in self._platforms.items()
            },
            "consecutive_failures": {
                name: ps.consecutive_failures
                for name, ps in self._platforms.items()
            },
            "auto_disabled": [
                name
                for name, ps in self._platforms.items()
                if not ps.enabled
            ],
            "last_cycle_at": max(
                (ps.last_run for ps in self._platforms.values() if ps.last_run),
                default=None,
            ),
        }

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler.

        Registers all enabled platforms as APScheduler jobs with staggered
        start times and begins the main discovery loop.
        """
        if self._running:
            logger.warning("scheduler.already_running")
            return

        self._running = True
        self._stats.started_at = datetime.now(UTC)
        self._daily_reset_time = datetime.now(UTC)

        # Register a platform if none exist.
        if not self._platforms:
            logger.info("scheduler.no_platforms_registered")

        # Register each platform as a recurring APScheduler job.
        for name, ps in self._platforms.items():
            if ps.enabled:
                self._register_aps_job(name, ps)

        # Start APScheduler.
        self._scheduler.start()

        # Register signal handlers for graceful shutdown.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._signal_handler(sig))
            except (NotImplementedError, ValueError):
                pass  # Windows or restricted environment

        logger.info(
            "scheduler.started",
            platform_count=len(self._platforms),
            daily_cap=self._daily_cap,
            window_hours=self._window_hours,
        )

    async def stop(self, *, grace_period_seconds: int = 30) -> None:
        """Gracefully stop the scheduler.

        Parameters
        ----------
        grace_period_seconds : int
            Max seconds to wait for in-flight discovery cycles to complete.

        """
        if not self._running:
            return

        logger.info("scheduler.stopping", grace_period=grace_period_seconds)

        # Shut down APScheduler (waits for running jobs up to grace_period).
        self._scheduler.shutdown(wait=True)

        # If a shutdown task was created by a signal handler, wait for it
        # only if we are not *inside* that task (avoid circular await).
        if (
            self._shutdown_task is not None
            and not self._shutdown_task.done()
            and self._shutdown_task is not asyncio.current_task()
        ):
            try:
                await self._shutdown_task
            except Exception as exc:
                logger.warning("scheduler.stop_task_failed", error=str(exc))

        self._running = False
        self._shutdown_event.set()

        logger.info(
            "scheduler.stopped",
            total_runs=self._stats.total_runs,
            total_leads=self._stats.total_leads,
        )

    # ── Platform management ─────────────────────────────────────────────

    def add_platform(
        self,
        platform_name: str,
        *,
        interval_minutes: int = _DEFAULT_INTERVAL_MINUTES,
        max_per_round: int = 10,
    ) -> None:
        """Register or update a platform schedule.

        Parameters
        ----------
        platform_name : str
            Lowercase platform name.
        interval_minutes : int
            Minutes between discovery rounds for this platform.
        max_per_round : int
            Maximum leads to collect per round.

        """
        schedule = PlatformSchedule(
            platform_name=platform_name,
            interval_minutes=interval_minutes,
            max_per_round=max_per_round,
        )
        self._platforms[platform_name] = schedule

        # If the scheduler is already running, register the APS job.
        if self._running and schedule.enabled:
            self._register_aps_job(platform_name, schedule)

        logger.info(
            "scheduler.platform_added",
            platform=platform_name,
            interval=interval_minutes,
        )

    def remove_platform(self, platform_name: str) -> bool:
        """Remove a platform from the schedule.

        Parameters
        ----------
        platform_name : str
            Lowercase platform name.

        Returns
        -------
        bool
            *True* if the platform was found and removed.

        """
        if platform_name not in self._platforms:
            logger.warning("scheduler.platform_not_found", platform=platform_name)
            return False

        # Remove APS job if it exists.
        job_id = self._job_id(platform_name)
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

        del self._platforms[platform_name]

        logger.info("scheduler.platform_removed", platform=platform_name)
        return True

    def disable_platform(self, platform_name: str) -> None:
        """Temporarily disable a platform so it is skipped in cycles.

        Parameters
        ----------
        platform_name : str
            Lowercase platform name.

        """
        schedule = self._platforms.get(platform_name)
        if schedule:
            schedule.enabled = False
            job_id = self._job_id(platform_name)
            if self._scheduler.get_job(job_id):
                self._scheduler.remove_job(job_id)
            logger.info("scheduler.platform_disabled", platform=platform_name)

    def enable_platform(self, platform_name: str) -> None:
        """Re-enable a previously disabled platform.

        Parameters
        ----------
        platform_name : str
            Lowercase platform name.

        """
        schedule = self._platforms.get(platform_name)
        if schedule:
            schedule.enabled = True
            schedule.consecutive_failures = 0
            if self._running:
                self._register_aps_job(platform_name, schedule)
            logger.info("scheduler.platform_enabled", platform=platform_name)

    # ── Discovery execution ─────────────────────────────────────────────

    async def _run_discovery_cycle(self, platform_name: str) -> None:
        """Execute one discovery cycle for *platform_name*.

        This is the APScheduler job callback.  It enforces the daily cap,
        runs the discovery function, and updates stats.
        """
        # Enforce daily cap at the scheduler level.
        if self._daily_cap_reached():
            logger.info(
                "scheduler.daily_cap_reached",
                cap=self._daily_cap,
                found=self._stats.total_leads,
            )
            return

        if self._cycle_lock.locked():
            logger.warning("scheduler.cycle_skipped_lock")
            return

        schedule = self._platforms.get(platform_name)
        if not schedule or not schedule.enabled:
            return

        logger.info("scheduler.cycle_starting", platform=platform_name)

        async with self._cycle_lock:
            # Double-check cap after acquiring lock.
            if self._daily_cap_reached():
                return

            try:
                # Calculate remaining capacity for this platform.
                remaining = min(
                    schedule.max_per_round,
                    self._daily_cap - self._stats.total_leads,
                )

                if remaining <= 0:
                    logger.info("scheduler.cycle_capacity_exhausted", platform=platform_name)
                    return

                # Run the discovery function.
                if self._discovery_fn is not None:
                    result = await self._discovery_fn([platform_name])
                else:
                    result = {}

                # Update stats.
                platform_result = result.get(platform_name, {})
                leads_found = platform_result.get("found", 0)
                leads_new = platform_result.get("new", 0)

                self._stats.total_runs += 1
                self._stats.total_leads += leads_found
                self._stats.total_new += leads_new

                schedule.last_run = datetime.now(UTC)
                schedule.total_found += leads_found
                schedule.consecutive_failures = 0

                self._stats.total_failures += platform_result.get("failed", 0)

                # Update per-platform stats.
                ps = self._stats.platform_stats.setdefault(platform_name, {
                    "runs": 0,
                    "leads": 0,
                    "new": 0,
                    "failures": 0,
                })
                ps["runs"] += 1
                ps["leads"] += leads_found
                ps["new"] += leads_new
                if platform_result.get("failed", 0):
                    ps["failures"] += platform_result.get("failed", 0)

                logger.info(
                    "scheduler.cycle_completed",
                    platform=platform_name,
                    found=leads_found,
                    new=leads_new,
                    total_leads=self._stats.total_leads,
                    daily_cap=self._daily_cap,
                )

            except Exception as exc:
                schedule.consecutive_failures += 1
                self._stats.total_failures += 1

                logger.exception(
                    "scheduler.cycle_failed",
                    platform=platform_name,
                    error=str(exc),
                    consecutive=schedule.consecutive_failures,
                )

                # Auto-disable if consecutive failures exceed threshold.
                if schedule.consecutive_failures >= schedule.max_consecutive_failures:
                    logger.warning(
                        "scheduler.auto_disabling_platform",
                        platform=platform_name,
                        failures=schedule.consecutive_failures,
                    )
                    self.disable_platform(platform_name)

            # Heartbeat — log health snapshot after every cycle attempt.
            logger.info("scheduler.cycle_complete", **self.health_status)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _register_aps_job(self, platform_name: str, schedule: PlatformSchedule) -> None:
        """Register an APScheduler job for *platform_name*.

        Adds jitter to the start time so platforms don't all fire at once.
        """
        job_id = self._job_id(platform_name)
        trigger = IntervalTrigger(
            minutes=schedule.interval_minutes,
            jitter=60,  # ±60 s jitter per interval
        )
        # Stagger the first run by a random offset.
        start_delay = random.randint(0, _BASE_JITTER_SECONDS)

        self._scheduler.add_job(
            self._run_discovery_cycle,
            trigger=trigger,
            args=[platform_name],
            id=job_id,
            name=platform_name,
            replace_existing=True,
            next_run_time=(
                datetime.now(UTC) + timedelta(seconds=start_delay)
            ),
        )

        logger.debug(
            "scheduler.job_registered",
            platform=platform_name,
            interval=schedule.interval_minutes,
            start_delay=start_delay,
        )

    def _job_id(self, platform_name: str) -> str:
        """Return a deterministic APScheduler job ID for *platform_name*."""
        return f"discovery_{platform_name}"

    def _daily_cap_reached(self) -> bool:
        """Check if the total leads found today exceeds the daily cap.

        Resets the counter if a new UTC day has started.
        """
        now = datetime.now(UTC)

        # Reset counters if we've crossed a UTC day boundary.
        if self._daily_reset_time is not None:
            last_reset_day = self._daily_reset_time.date()
            today = now.date()
            if last_reset_day != today:
                logger.info(
                    "scheduler.daily_reset",
                    previous_day=str(last_reset_day),
                    total_leads_reset=self._stats.total_leads,
                )
                self._stats.total_leads = 0
                self._stats.total_new = 0
                self._daily_reset_time = now

                # Reset per-platform counters.
                for ps in self._platforms.values():
                    ps.total_found = 0

                return False

        return self._stats.total_leads >= self._daily_cap

    def _signal_handler(self, sig: signal.Signals) -> Callable[[], None]:
        """Build a signal handler for graceful shutdown.

        Saves the shutdown task so :meth:`stop` can await it, and attaches
        a done callback that logs any exceptions raised during shutdown.
        """

        def _log_stop_error(task: asyncio.Task[None]) -> None:
            try:
                exc = task.exception()
                if exc is not None:
                    logger.error("scheduler.signal_stop_failed", error=str(exc))
            except asyncio.CancelledError:
                pass

        def _handle() -> None:
            logger.info("scheduler.signal_received", signal=sig.name)
            task = asyncio.ensure_future(self.stop())
            task.add_done_callback(_log_stop_error)
            self._shutdown_task = task

        return _handle

    # ── Context manager ─────────────────────────────────────────────────

    @asynccontextmanager
    async def run(self):
        """Async context manager that starts the scheduler and cleans up on exit.

        Usage::

            async with scheduler.run():
                # scheduler is running in the background
                await asyncio.sleep(3600)
        """
        try:
            await self.start()
            yield self
        finally:
            await self.stop()

    # ── Factory method ──────────────────────────────────────────────────

    @classmethod
    def with_defaults(
        cls,
        discovery_fn: Callable[[list[str]], Any] | None = None,
        *,
        daily_cap: int = _MAX_DAILY_OPPORTUNITIES,
        platforms: list[tuple[str, int, int]] | None = None,
    ) -> DiscoveryScheduler:
        """Create a :class:`DiscoveryScheduler` with default platform schedules.

        Parameters
        ----------
        discovery_fn : callable or None
            Discovery function to call per cycle.
        daily_cap : int
            Daily opportunity cap.
        platforms : list of (name, interval_minutes, max_per_round) or None
            Platform schedules.  Defaults to all supported platforms.

        Returns
        -------
        DiscoveryScheduler

        """
        sched = cls(discovery_fn=discovery_fn, daily_cap=daily_cap)

        if platforms is None:
            platforms = [
                ("upwork", 60, 10),
                ("linkedin", 120, 8),
                ("freelancer", 90, 10),
                ("remote_ok", 180, 15),
                ("yc_work", 360, 10),
            ]

        for name, interval, max_per in platforms:
            sched.add_platform(name, interval_minutes=interval, max_per_round=max_per)

        return sched
