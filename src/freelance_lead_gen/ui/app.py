"""LeadGen TUI — main application entry point for the terminal interface.

The :class:`LeadGenTUI` application manages screen navigation, background
pipeline execution, real-time status updates, and keyboard bindings.

Key bindings
------------
q / Ctrl+C — Quit the application
j          — Navigate down
k          — Navigate up
space      — Select item
e          — Edit draft
a          — Approve
r          — Reject
"""

from __future__ import annotations as _annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import structlog
from textual import on
from textual.app import App
from textual.binding import Binding

from freelance_lead_gen.storage.database import close_db, init_db
from freelance_lead_gen.storage.repository import (
    DatabaseError,
    OpportunityRepository,
)

if TYPE_CHECKING:
    from freelance_lead_gen.agents.orchestrator import LeadGenOrchestrator
    from freelance_lead_gen.models.opportunity import LeadOpportunity

from .dashboard import (
    DashboardScreen,
    NavigateToReview,
    NavigateToStats,
    RunDiscoveryRequested,
)
from .lead_detail import (
    LeadApproved,
    LeadArchived,
    LeadDetailScreen,
    LeadRejected,
    NavigateBack,
    RegenerateRequested,
)
from .lead_list import (
    LeadListScreen,
    LeadSelected,
    NavigateBackToDashboard,
)
from .review_queue import (
    NavigateBackFromReview,
    NavigateToDetail,
    ReviewAllComplete,
    ReviewQueueScreen,
)

logger = structlog.get_logger(__name__)


# ── LeadGenTUI Application ───────────────────────────────────────────────


class LeadGenTUI(App[None]):
    """Textual terminal UI for the freelance lead generation system.

    The main application orchestrates screen switching, background pipeline
    execution, and database lifecycle.

    Parameters
    ----------
    repository : OpportunityRepository, optional
        Repository for database access.  Created from the default session
        factory if not provided.
    orchestrator : LeadGenOrchestrator, optional
        Pipeline orchestrator.  Created with default agents if not provided.

    """

    TITLE = "📊  Freelance Lead Gen"
    SUB_TITLE = "Automated opportunity discovery & outreach preparation"

    CSS = """
    Screen {
        background: #1a1b26;
    }

    /* ── Colour definitions ─────────────────────────────── */
    $surface: #1a1b26;
    $surface-lighten-1: #24283b;
    $panel: #1a1b26;
    $boost: #24283b;
    $border: #3b4261;
    $text: #c0caf5;
    $text-muted: #565f89;
    $primary: #7aa2f7;
    $secondary: #7dcfff;
    $accent: #bb9af7;
    $success: #9ece6a;
    $warning: #e0af68;
    $error: #f7768e;
    $surface-alt: #1f2335;

    /* ── Global widget styling ──────────────────────────── */
    * {
        scrollbar-color: $primary $surface;
        scrollbar-size-vertical: 1;
    }

    Button {
        background: $surface-lighten-1;
        color: $text;
        border: none;
        padding: 0 2;
        min-height: 1;
    }

    Button:hover {
        background: $primary 30%;
    }

    Button:focus {
        background: $primary 40%;
    }

    Button.-disabled {
        opacity: 0.4;
    }

    Input {
        background: $surface-lighten-1;
        color: $text;
        border: solid $border;
        padding: 0 1;
    }

    Input:focus {
        border: solid $primary;
    }

    ListView {
        background: $surface;
        color: $text;
    }

    ListView > ListItem {
        padding: 0 1;
        background: $surface;
    }

    ListView > ListItem:hover {
        background: $boost;
    }

    ListView > ListItem.--highlight {
        background: $primary 20%;
    }

    ListView > ListItem.--highlight:hover {
        background: $primary 30%;
    }

    RichLog {
        background: $surface;
        color: $text;
    }

    TextArea {
        background: $surface;
        color: $text;
    }

    Header {
        background: $surface-lighten-1;
        color: $text;
    }

    Footer {
        background: $surface-lighten-1;
        color: $text-muted;
    }

    Label {
        color: $text;
    }

    Static {
        color: $text;
    }

    /* ── Borders ────────────────────────────────────────── */
    .-border-solid {
        border: solid $border;
    }

    /* ── Scrollbar styling ──────────────────────────────── */
    Scrollbar {
        background: $surface;
        color: $primary;
    }
    """

    # Binding list — q to quit, the rest are per-screen
    BINDINGS: tuple[Binding, ...] = (
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("d", "toggle_dark", "Dark mode", show=False),
    )

    # ── Application state ─────────────────────────────────────────────────

    _repository: OpportunityRepository
    _orchestrator: LeadGenOrchestrator | None
    _pipeline_running: bool = False

    def __init__(
        self,
        repository: OpportunityRepository | None = None,
        orchestrator: LeadGenOrchestrator | None = None,
    ) -> None:
        super().__init__()
        self._repository = repository or OpportunityRepository()
        self._orchestrator = orchestrator
        self._pipeline_running = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ── Lifecycle hooks ──────────────────────────────────────────────────

    async def on_mount(self) -> None:
        """Application startup — initialise DB, run checks, show dashboard."""
        self.dark = True

        db_status = await self._check_database()

        await self._show_dashboard()

        if self._orchestrator:
            logger.info("tui.started", dashboard_loaded=db_status)

    async def on_unmount(self) -> None:
        """Application shutdown — close resources."""
        logger.info("tui.shutting_down")

        if self._pipeline_running:
            logger.info("tui.cancelling_pipeline")
            self._pipeline_running = False

        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        if self._orchestrator:
            await self._orchestrator.shutdown()

    # ── Database ─────────────────────────────────────────────────────────

    async def _check_database(self) -> bool:
        """Verify the database is accessible and has the expected schema.

        Returns *True* if the database is healthy.
        """
        try:
            stats = await self._repository.get_stats()
        except DatabaseError as exc:
            logger.exception("tui.database_check_failed", error=str(exc))
            return False
        except Exception as exc:
            logger.exception("tui.database_unexpected_error", error=str(exc))
            return False
        else:
            logger.info(
                "tui.database_healthy",
                total_leads=stats.get("total", 0),  # type: ignore[union-attr]
            )
            return True

    # ── Screen navigation ────────────────────────────────────────────────

    async def _show_dashboard(self) -> None:
        """Switch to the dashboard screen."""
        dashboard = DashboardScreen(self._repository)
        await self.push_screen(dashboard)

    async def _show_lead_list(self) -> None:
        """Push the lead list screen."""
        lead_list = LeadListScreen(self._repository)
        await self.push_screen(lead_list)

    async def _show_lead_detail(self, opportunity: LeadOpportunity) -> None:
        """Push the lead detail screen for *opportunity*."""
        detail = LeadDetailScreen(opportunity, self._repository)
        await self.push_screen(detail)

    async def _show_review_queue(self) -> None:
        """Push the review queue screen."""
        queue = ReviewQueueScreen(self._repository)
        await self.push_screen(queue)

    # ── Message handlers (screen → app communication) ───────────────────

    @on(RunDiscoveryRequested)
    async def on_run_discovery(self, _event: RunDiscoveryRequested) -> None:
        """Handle a request to run discovery from the dashboard."""
        await self._run_pipeline()

    @on(NavigateToReview)
    async def on_navigate_review(self, _event: NavigateToReview) -> None:
        """Handle a request to navigate to the review queue."""
        await self._show_review_queue()

    @on(NavigateToStats)
    async def on_navigate_stats(self, _event: NavigateToStats) -> None:
        """Handle a request to navigate to the lead list/stats view."""
        await self._show_lead_list()

    @on(LeadSelected)
    async def on_lead_selected(self, event: LeadSelected) -> None:
        """Handle a lead selection from the lead list."""
        await self._show_lead_detail(event.opportunity)

    @on(NavigateBackToDashboard)
    async def on_back_to_dashboard(self, _event: NavigateBackToDashboard) -> None:
        """Handle a request to navigate back to the dashboard."""
        await self.pop_screen()

    @on(LeadApproved)
    async def on_lead_approved(self, _event: LeadApproved) -> None:
        """Handle a lead being approved from the detail screen."""
        await self._update_status_bar()
        await self.pop_screen()

    @on(LeadRejected)
    async def on_lead_rejected(self, _event: LeadRejected) -> None:
        """Handle a lead being rejected from the detail screen."""
        await self._update_status_bar()

    @on(LeadArchived)
    async def on_lead_archived(self, _event: LeadArchived) -> None:
        """Handle a lead being archived from the detail screen."""
        await self._update_status_bar()
        await self.pop_screen()

    @on(RegenerateRequested)
    async def on_regenerate(self, event: RegenerateRequested) -> None:
        """Handle a request to regenerate a draft."""
        if self._orchestrator:
            self.notify(
                f"Regenerating draft for {event.opportunity_id}…",
                severity="information",
                timeout=3,
            )
            await self._run_rephase(event.opportunity_id)

    @on(NavigateBack)
    async def on_navigate_back(self, _event: NavigateBack) -> None:
        """Handle a request to navigate back from the detail screen."""
        await self.pop_screen()

    @on(NavigateToDetail)
    async def on_navigate_detail(self, event: NavigateToDetail) -> None:
        """Handle a request to view lead detail from the review queue."""
        await self._show_lead_detail(event.opportunity)

    @on(ReviewAllComplete)
    async def on_review_complete(self, event: ReviewAllComplete) -> None:
        """Handle the review queue completing all items."""
        self.notify(
            f"✅ Review complete: {event.approved} approved, {event.rejected} rejected",
            severity="information",
            timeout=5,
        )

    @on(NavigateBackFromReview)
    async def on_back_from_review(self, _event: NavigateBackFromReview) -> None:
        """Handle a request to navigate back from the review queue."""
        await self.pop_screen()

    # ── Status bar updates ──────────────────────────────────────────────

    async def _update_status_bar(self) -> None:
        """Refresh dashboard stats after state changes.

        Called after approve/reject/archive actions so the next dashboard
        refresh shows current numbers.  The dashboard's own auto-refresh
        timer will pick up these changes within 30 seconds.
        """
        async with suppress(Exception):
            await self._repository.get_stats()

    # ── Pipeline execution ──────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Run the full pipeline in the background."""
        if self._pipeline_running:
            self.notify("Pipeline already running", severity="warning", timeout=2)
            return

        if self._orchestrator is None:
            self.notify(
                "No orchestrator configured — pipeline not available",
                severity="error",
                timeout=3,
            )
            return

        self._pipeline_running = True
        task = asyncio.create_task(self._pipeline_worker())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        self.notify(
            "🔍  Pipeline started — discovering and qualifying opportunities…",
            severity="information",
            timeout=5,
        )

    async def _pipeline_worker(self) -> None:
        """Background worker that executes the full pipeline."""
        assert self._orchestrator is not None

        try:
            report = await self._orchestrator.run_full_pipeline()

            if report.success:
                discovered = report.total_discovered
                qualified = report.total_qualified
                drafted = report.total_drafted
                self.notify(
                    f"✅ Pipeline complete: {discovered} discovered, "
                    f"{qualified} qualified, {drafted} drafted",
                    severity="information",
                    timeout=8,
                )
            else:
                errors = report.total_errors
                self.notify(
                    f"⚠️ Pipeline completed with {errors} error(s). See logs for details.",
                    severity="warning",
                    timeout=8,
                )

        except Exception as exc:
            logger.exception("pipeline_worker.failed", error=str(exc))
            self.notify(
                f"❌ Pipeline failed: {exc}",
                severity="error",
                timeout=5,
            )
        finally:
            self._pipeline_running = False
            await self._update_status_bar()

    async def _run_rephase(self, opportunity_id: str) -> None:
        """Regenerate the draft for a single opportunity."""
        if not self._orchestrator:
            self.notify("No orchestrator available", severity="error", timeout=2)
            return

        try:
            opportunity = await self._repository.get_by_id(opportunity_id)
            if opportunity is None:
                self.notify("Opportunity not found", severity="error", timeout=2)
                return

            report = await self._orchestrator.run_full_pipeline(
                opportunities=[opportunity],
                run_discovery=False,
                run_filtering=False,
                run_personalization=True,
                run_verification=True,
                run_hitl=True,
            )

            if report.total_drafted:
                self.notify(
                    "✅ Draft regenerated",
                    severity="information",
                    timeout=4,
                )
            else:
                self.notify(
                    "⚠️ Draft regeneration completed but no draft was produced",
                    severity="warning",
                    timeout=4,
                )

            await self._update_status_bar()

        except DatabaseError as exc:
            self.notify(f"Database error: {exc}", severity="error", timeout=3)
        except Exception as exc:
            self.notify(f"Regeneration failed: {exc}", severity="error", timeout=3)

    # ── Key actions ─────────────────────────────────────────────────────

    def action_quit(self) -> None:
        """Quit the application."""
        logger.info("tui.quitting")
        self.exit()


# ── Main entry point ─────────────────────────────────────────────────────


async def run_tui(
    repository: OpportunityRepository | None = None,
    orchestrator: LeadGenOrchestrator | None = None,
) -> None:
    """Start the LeadGen TUI application.

    Parameters
    ----------
    repository : OpportunityRepository, optional
        Repository instance.  Created from defaults if not provided.
    orchestrator : LeadGenOrchestrator, optional
        Pipeline orchestrator.  Created with defaults if not provided.

    """
    try:
        await init_db()
    except Exception as exc:
        logger.exception("tui.db_init_failed", error=str(exc))
        print(f"ERROR: Database initialisation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    app = LeadGenTUI(repository=repository, orchestrator=orchestrator)
    try:
        await app.run_async()
    finally:
        await close_db()
