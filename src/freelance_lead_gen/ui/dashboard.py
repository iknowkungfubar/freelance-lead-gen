"""Dashboard screen — main landing view for the LeadGen TUI.

Displays aggregate statistics, platform breakdowns, recent activity, and
quick-action buttons.  Auto-refreshes every 30 seconds via a recurring
timer.
"""

from __future__ import annotations as _annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Header, Label, Static
from textual_plotext import PlotextPlot

from freelance_lead_gen.storage.repository import (
    DatabaseError,
    OpportunityRepository,
)
from freelance_lead_gen.ui.widgets import (
    DIM,
    ERROR,
    PRIMARY,
    ActivityFeed,
    StatsCard,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer

# ── Refresh interval ─────────────────────────────────────────────────────

DASHBOARD_REFRESH_SECONDS: float = 30.0

# ── Quick Action Button IDs ──────────────────────────────────────────────

BTN_RUN_DISCOVERY = "btn-run-discovery"
BTN_REVIEW_PENDING = "btn-review-pending"
BTN_VIEW_STATS = "btn-view-stats"


# ── Messages ─────────────────────────────────────────────────────────────


class RunDiscoveryRequested(Message):
    """Posted when the user clicks "Run Discovery"."""

    def __init__(self) -> None:
        super().__init__()


class NavigateToReview(Message):
    """Posted when the user clicks "Review Pending"."""

    def __init__(self) -> None:
        super().__init__()


class NavigateToStats(Message):
    """Posted when the user clicks "View Stats"."""

    def __init__(self) -> None:
        super().__init__()


# ── DashboardScreen ──────────────────────────────────────────────────────


class DashboardScreen(Screen[None]):
    """Main landing screen with aggregate dashboards and quick actions.

    Displays:
    - Stats cards for total leads, today's leads, pending review, approved today.
    - Platform-breakdown widgets.
    - Recent activity feed (last 10 actions).
    - Quick-action buttons.
    - Lead-trends plot (last 7 days).
    """

    DEFAULT_CSS = """
    DashboardScreen {
        background: $surface;
    }

    DashboardScreen #dashboard-container {
        height: 100%;
    }

    DashboardScreen #header-row {
        height: 3;
        padding: 0 1;
    }

    DashboardScreen #header-title {
        text-style: bold;
        color: $text;
        padding: 0 1;
    }

    DashboardScreen #header-subtitle {
        color: $text-muted;
        padding: 0 1;
    }

    DashboardScreen #stats-row {
        height: 5;
        padding: 0 1;
    }

    DashboardScreen #stats-row StatsCard {
        margin: 0 1 0 0;
    }

    DashboardScreen #content-row {
        height: 1fr;
        padding: 0 1;
    }

    DashboardScreen #platform-col {
        width: 32;
        min-width: 24;
    }

    DashboardScreen #platform-col > #platform-title {
        text-style: bold;
        color: $text;
        padding: 0 0;
        margin-bottom: 1;
    }

    DashboardScreen .platform-row {
        height: 1;
        margin-bottom: 1;
    }

    DashboardScreen .platform-label {
        width: 14;
        color: $text;
    }

    DashboardScreen .platform-count {
        width: 6;
        text-align: right;
        color: $text-muted;
    }

    DashboardScreen .platform-bar {
        height: 1;
    }

    DashboardScreen #activity-col {
        width: 1fr;
        min-width: 30;
    }

    DashboardScreen #activity-col > #activity-title {
        text-style: bold;
        color: $text;
        padding: 0 0;
        margin-bottom: 1;
    }

    DashboardScreen #activity-col #activity-feed {
        height: 1fr;
        border: solid $border;
    }

    DashboardScreen #plot-col {
        width: 40;
        min-width: 32;
    }

    DashboardScreen #plot-col > #plot-title {
        text-style: bold;
        color: $text;
        padding: 0 0;
        margin-bottom: 1;
    }

    DashboardScreen #plot-col #trend-plot {
        height: 1fr;
        border: solid $border;
    }

    DashboardScreen #actions-row {
        height: 5;
        padding: 0 1;
    }

    DashboardScreen #actions-row Button {
        margin: 0 1 0 0;
        min-width: 20;
    }

    DashboardScreen #actions-row #btn-run-discovery {
        background: $primary 40%;
        color: $text;
    }

    DashboardScreen #actions-row #btn-review-pending {
        background: $warning 40%;
        color: $text;
    }

    DashboardScreen #actions-row #btn-view-stats {
        background: $secondary 30%;
        color: $text;
    }

    DashboardScreen #status-message {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    DashboardScreen #error-banner {
        height: auto;
        max-height: 3;
        background: $error 20%;
        color: $error;
        padding: 0 1;
        display: none;
    }
    """

    stats: dict[str, int] = reactive({})
    platform_counts: dict[str, int] = reactive({})
    trending: list[dict[str, Any]] = reactive([])
    refresh_timer: Timer | None = None

    def __init__(self, repository: OpportunityRepository) -> None:
        self._repository = repository
        self._load_error: str | None = None
        super().__init__()

    # ── Screen lifecycle ─────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="dashboard-container"):
            with Horizontal(id="header-row"):
                yield Label("📊  Lead Dashboard", id="header-title")
                yield Label("", id="header-subtitle")
            with Horizontal(id="stats-row"):
                yield StatsCard("Total Leads", "—", id="stat-total")
                yield StatsCard("Today", "—", id="stat-today")
                yield StatsCard("Pending Review", "—", id="stat-pending")
                yield StatsCard("Approved Today", "—", id="stat-approved")
            with Horizontal(id="content-row"):
                with Vertical(id="platform-col"):
                    yield Label("Platforms", id="platform-title")
                    yield Container(id="platform-list")
                with Vertical(id="activity-col"):
                    yield Label("Recent Activity", id="activity-title")
                    yield ActivityFeed(id="activity-feed")
                with Vertical(id="plot-col"):
                    yield Label("Lead Trends (7d)", id="plot-title")
                    yield PlotextPlot(id="trend-plot")
            with Horizontal(id="actions-row"):
                yield Button("🔍  Run Discovery", id=BTN_RUN_DISCOVERY)
                yield Button("📝  Review Pending", id=BTN_REVIEW_PENDING)
                yield Button("📈  View Stats", id=BTN_VIEW_STATS)
            yield Label("", id="status-message")
            yield Static(id="error-banner", classes="error-banner")

    async def on_screen_resume(self) -> None:
        """Refresh dashboard data when the screen becomes active."""
        await self._load_dashboard()

    def on_mount(self) -> None:
        self._start_auto_refresh()

    # ── Auto-refresh ────────────────────────────────────────────────────

    def _start_auto_refresh(self) -> None:
        """Begin periodic dashboard refresh."""
        self.refresh_timer = self.set_interval(
            DASHBOARD_REFRESH_SECONDS,
            self._load_dashboard,
        )

    # ── Button handlers ──────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == BTN_RUN_DISCOVERY:
            self.post_message(RunDiscoveryRequested())
        elif event.button.id == BTN_REVIEW_PENDING:
            self.post_message(NavigateToReview())
        elif event.button.id == BTN_VIEW_STATS:
            self.post_message(NavigateToStats())

    # ── Data loading ─────────────────────────────────────────────────────

    async def _load_dashboard(self) -> None:
        """Fetch all dashboard data from the repository."""
        self._hide_error()
        self.query_one("#status-message", Label).update("🔄  Refreshing…")

        try:
            stats = await self._repository.get_stats()
            platform_counts = await self._repository.get_platform_counts()
            today_total = await self._today_count()

            self.stats = stats
            self.platform_counts = platform_counts

            # Update header
            now = datetime.now(UTC)
            self.query_one("#header-subtitle", Label).update(
                f"Last updated: {now.strftime('%H:%M:%S')}"
            )

            # Update stats cards
            total = stats.get("total", 0)
            pending = stats.get("drafted", 0)
            reviewed = stats.get("reviewed", 0)

            self._update_stat("stat-total", "Total Leads", str(total))
            self._update_stat("stat-today", "Today", str(today_total))
            self._update_stat(
                "stat-pending",
                "Pending Review",
                str(pending),
                delta="needs review" if pending else "none",
            )
            self._update_stat(
                "stat-approved",
                "Approved Today",
                str(reviewed),
            )

            # Update platform breakdown
            self._render_platforms(platform_counts)

            # Update trend plot
            self._render_trend_plot()

            # Log activity
            activity = self.query_one("#activity-feed", ActivityFeed)
            activity.add_entry(
                f"Dashboard refreshed — {total} total leads, {pending} pending review",
                level="info",
            )

            self.query_one("#status-message", Label).update(
                f"✓  Last refresh: {now.strftime('%H:%M:%S')}"
            )

        except DatabaseError as exc:
            self._show_error(f"Database error: {exc}")
        except Exception as exc:
            self._show_error(f"Failed to load dashboard: {exc}")

    async def _today_count(self) -> int:
        """Count opportunities created today."""
        from datetime import datetime

        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today, _ = await self._repository.search(
            date_from=today_start,
            limit=500,
        )
        return len(today)

    # ── Rendering helpers ────────────────────────────────────────────────

    def _update_stat(
        self,
        stat_id: str,
        label: str,
        value: str,
        delta: str | None = None,
    ) -> None:
        widget = self.query_one(f"#{stat_id}", StatsCard)
        widget.label = label
        widget.value = value
        widget.delta = delta

    def _render_platforms(self, platform_counts: dict[str, int]) -> None:
        """Render the platform-breakdown list."""
        container = self.query_one("#platform-list", Container)
        container.remove_children()

        if not platform_counts:
            container.mount(Static("[dim]No platforms yet[/]"))
            return

        total = sum(platform_counts.values())
        for platform_name, count in sorted(
            platform_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        ):
            pct = (count / total) * 100 if total else 0
            bar_filled = "━" * max(1, int(pct // 5))
            bar_empty = "━" * max(0, 20 - int(pct // 5))

            row = Horizontal(
                Static(f" {platform_name.title()} ", classes="platform-label"),
                Static(str(count), classes="platform-count"),
                Static(
                    f"[{PRIMARY}]{bar_filled}[/][{DIM}]{bar_empty}[/] {pct:.0f}%",
                    classes="platform-bar",
                ),
                classes="platform-row",
            )
            container.mount(row)

    def _render_trend_plot(self) -> None:
        """Render the lead-trends plot using textual-plotext."""
        try:
            plot = self.query_one("#trend-plot", PlotextPlot)
            plt = plot.plt

            # Collect last 7 days of data from the repository
            # For simplicity, we show daily buckets from the trending data.
            dates: list[str] = []
            counts: list[int] = []

            if self.trending:
                for entry in self.trending[-7:]:
                    dates.append(entry.get("date", "?"))
                    counts.append(entry.get("count", 0))
            else:
                # Fallback: show a simple placeholder.
                from datetime import timedelta

                now = datetime.now(UTC)
                for i in range(6, -1, -1):
                    d = now - timedelta(days=i)
                    dates.append(d.strftime("%m/%d"))

            plt.clear_data()
            if counts:
                plt.bar(dates, counts, color=PRIMARY)
                plt.title("Leads per Day (7 days)")
            else:
                plt.title("No trend data yet")
            plt.xlabel("")
            plt.ylabel("")

        except Exception:
            # textual-plotext may not be available or rendering fails — silently ignore.
            pass

    # ── Error handling ───────────────────────────────────────────────────

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.update(f"[bold]⚠ {message}[/]")
        banner.styles.display = "block"
        self.query_one("#status-message", Label).update(f"[{ERROR}]⚠ Load failed[/]")

    def _hide_error(self) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.styles.display = "none"

    # ── Key bindings ─────────────────────────────────────────────────────

    def key_d(self) -> None:
        """Run discovery (d)."""
        self.post_message(RunDiscoveryRequested())

    def key_r(self) -> None:
        """Navigate to review (r)."""
        self.post_message(NavigateToReview())

    def key_s(self) -> None:
        """Navigate to stats (s)."""
        self.post_message(NavigateToStats())

    def key_R(self) -> None:
        """Force refresh (Shift+R)."""
        self.run_worker(self._load_dashboard(), exclusive=True)
