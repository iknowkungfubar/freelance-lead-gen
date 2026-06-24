"""Review queue screen — batch review workflow for drafted opportunities.

Provides a focused review interface for opportunities that need human
approval (DRAFTED status).  Features:

- Lists all leads awaiting human review
- Quick actions: approve all visible, reject selected, bulk edit
- Progress indicator: X of Y reviewed
- Stats panel: average score, platform distribution
- Focus mode: one-at-a-time review flow using the detail screen
"""

from __future__ import annotations as _annotations

from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Header, Label, ListItem, ListView, Static

from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus
from freelance_lead_gen.storage.repository import (
    DatabaseError,
    OpportunityRepository,
)
from freelance_lead_gen.ui.widgets import (
    DIM,
    ERROR,
    PRIMARY,
    SUBTEXT,
    SUCCESS,
    TEXT,
    WARNING,
    format_budget,
    format_timestamp,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

# ── Messages ─────────────────────────────────────────────────────────────


class ReviewAllComplete(Message):
    """Posted when the review session completes."""

    def __init__(
        self,
        approved: int,
        rejected: int,
        total: int,
    ) -> None:
        self.approved = approved
        self.rejected = rejected
        self.total = total
        super().__init__()


class NavigateToDetail(Message):
    """Posted when the user wants to focus-review a single lead."""

    def __init__(self, opportunity: LeadOpportunity) -> None:
        self.opportunity = opportunity
        super().__init__()


class NavigateBackFromReview(Message):
    """Posted when the user requests to go back to the dashboard."""

    def __init__(self) -> None:
        super().__init__()


# ── ReviewListItem ───────────────────────────────────────────────────────


class ReviewListItem(ListItem):
    """A single lead in the review queue.

    Shows: title, platform, company, score, budget, posted date,
    and reviewed/approved status.
    """

    def __init__(self, opportunity: LeadOpportunity, idx: int) -> None:
        self.opportunity = opportunity
        self._idx = idx
        super().__init__()

    def render(self) -> Text:
        opp = self.opportunity
        checked = "☑" if opp.status == LeadStatus.REVIEWED else "☐"
        platform_name = opp.platform.title()
        budget = format_budget(opp.budget_min, opp.budget_max, opp.currency)
        company = opp.company or "──"
        posted = format_timestamp(opp.posted_date)
        score_str = f"{opp.score:>3}" if opp.score is not None else " —"

        return Text.assemble(
            (f" {checked} ", Style(color=SUCCESS if opp.status == LeadStatus.REVIEWED else DIM)),
            (f"#{self._idx:<3}", Style(color=DIM)),
            (f"{opp.title[:55]:<55}", Style(color=TEXT, bold=True)),
            (f"  {platform_name:<10}", Style(color=PRIMARY)),
            (f"{company:<18}", Style(color=SUBTEXT)),
            (f"{budget:<16}", Style(color=SUBTEXT)),
            (
                f" {score_str}",
                Style(color=SUCCESS if (opp.score or 0) >= 60 else WARNING, bold=True),
            ),
            (f"  {posted:<8}", Style(color=DIM)),
        )


# ── ReviewQueueScreen ────────────────────────────────────────────────────


class ReviewQueueScreen(Screen[None]):
    """Batch review workflow for drafted opportunities.

    Displays all DRAFTED-status leads and provides bulk operations:
    approve all, reject individual, and focus-mode for in-depth review.
    """

    DEFAULT_CSS = """
    ReviewQueueScreen {
        background: $surface;
    }

    ReviewQueueScreen #review-container {
        height: 100%;
    }

    ReviewQueueScreen #review-header {
        height: 3;
        padding: 0 1;
    }

    ReviewQueueScreen #review-header #review-title {
        text-style: bold;
        color: $text;
    }

    ReviewQueueScreen #review-header #review-actions-hint {
        color: $text-muted;
    }

    ReviewQueueScreen #stats-row {
        height: 5;
        padding: 0 1;
    }

    ReviewQueueScreen #stats-row > Static {
        margin: 0 1 0 0;
    }

    ReviewQueueScreen #progress-row {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    ReviewQueueScreen #review-list-container {
        height: 1fr;
        border: solid $border;
        margin: 0 1;
    }

    ReviewQueueScreen #review-list-container:focus-within {
        border: solid $primary;
    }

    ReviewQueueScreen #review-list {
        height: 100%;
    }

    ReviewQueueScreen #empty-state {
        height: 1fr;
        align: center middle;
        color: $text-muted;
        text-style: italic;
    }

    ReviewQueueScreen #action-row {
        height: 5;
        padding: 0 1;
        align: center middle;
    }

    ReviewQueueScreen #action-row Button {
        margin: 0 1;
        min-width: 16;
    }

    ReviewQueueScreen #btn-approve-all {
        background: $success 40%;
        color: $text;
    }

    ReviewQueueScreen #btn-approve-selected {
        background: $success 30%;
        color: $text;
    }

    ReviewQueueScreen #btn-reject-selected {
        background: $error 40%;
        color: $text;
    }

    ReviewQueueScreen #btn-focus-mode {
        background: $primary 40%;
        color: $text;
    }

    ReviewQueueScreen #btn-refresh {
        background: $panel;
        color: $text-muted;
    }

    ReviewQueueScreen #btn-back {
        background: $panel;
        color: $text-muted;
    }

    ReviewQueueScreen #status-message {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    ReviewQueueScreen #error-banner {
        height: auto;
        max-height: 3;
        background: $error 20%;
        color: $error;
        padding: 0 1;
        display: none;
    }

    ReviewQueueScreen #platform-dist {
        height: 3;
        padding: 0 1;
    }
    """

    pending_leads: list[LeadOpportunity] = reactive([])
    total_pending: int = reactive(0)
    total_reviewed: int = reactive(0)

    def __init__(self, repository: OpportunityRepository) -> None:
        self._repository = repository
        self._pending = []
        self._reviewed_count = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="review-container"):
            with Horizontal(id="review-header"):
                yield Label("📋  Review Queue", id="review-title")
                yield Label(
                    "j/k: navigate  space: open  a: approve all  q: back", id="review-actions-hint"
                )
            with Horizontal(id="stats-row"):
                yield Static("", id="stat-pending")
                yield Static("", id="stat-reviewed")
                yield Static("", id="stat-avg-score")
            yield Label("", id="progress-row")
            with Container(id="review-list-container"):
                yield ListView(id="review-list")
            with Horizontal(id="platform-dist"):
                yield Static("", id="platform-dist-text")
            with Horizontal(id="action-row"):
                yield Button("✓  Approve All", id="btn-approve-all")
                yield Button("✓  Approve Selected", id="btn-approve-selected")
                yield Button("✕  Reject Selected", id="btn-reject-selected")
                yield Button("🔍  Focus Mode", id="btn-focus-mode")
                yield Button("🔄  Refresh", id="btn-refresh")
                yield Button("←  Back", id="btn-back")
            yield Label("", id="status-message")
            yield Static(id="error-banner", classes="error-banner")

    def on_mount(self) -> None:
        self.run_worker(self._load_pending(), exclusive=True)

    async def on_screen_resume(self) -> None:
        self.run_worker(self._load_pending(), exclusive=True)

    # ── Data loading ────────────────────────────────────────────────────

    async def _load_pending(self) -> None:
        """Load all DRAFTED-status opportunities awaiting review."""
        self._hide_error()
        self.query_one("#status-message", Label).update("🔄  Loading…")

        try:
            self._pending = await self._repository.search(
                status=LeadStatus.DRAFTED,
                limit=500,
            )
            self.pending_leads = self._pending
            self.total_pending = len(self._pending)
            self.total_reviewed = 0

            self._render_list()
            self._update_stats()
            self._update_progress()

            self.query_one("#status-message", Label).update(
                f"✓  {len(self._pending)} items pending review"
            )

        except DatabaseError as exc:
            self._show_error(f"Database error: {exc}")
        except Exception as exc:
            self._show_error(f"Failed to load: {exc}")

    # ── Rendering ───────────────────────────────────────────────────────

    def _render_list(self) -> None:
        list_view = self.query_one("#review-list", ListView)
        list_view.clear()

        if not self._pending:
            list_view.mount(
                ListItem(Static("  ✅  No items pending review", classes="empty-state"))
            )
            return

        for i, opp in enumerate(self._pending, start=1):
            list_view.mount(ReviewListItem(opp, i))

        if list_view.children:
            list_view.index = 0

    def _update_stats(self) -> None:
        """Update the stats display row."""
        if not self._pending:
            self.query_one("#stat-pending", Static).update(
                f"[bold {SUCCESS}]✓ All clear![/] No leads pending review."
            )
            self.query_one("#stat-reviewed", Static).update("")
            self.query_one("#stat-avg-score", Static).update("")
            self.query_one("#platform-dist-text", Static).update("")
            return

        scores = [opp.score for opp in self._pending if opp.score is not None]
        avg_score = sum(scores) / len(scores) if scores else 0

        self.query_one("#stat-pending", Static).update(
            f"[{SUBTEXT}]Pending:[/] [bold]{len(self._pending)}[/]"
        )
        self.query_one("#stat-reviewed", Static).update(
            f"[{SUBTEXT}]Reviewed:[/] [bold {SUCCESS}]{self._reviewed_count}[/]"
        )
        self.query_one("#stat-avg-score", Static).update(
            f"[{SUBTEXT}]Avg Score:[/] [bold]{avg_score:.0f}[/]/100"
        )

        # Platform distribution
        platform_counts: dict[str, int] = {}
        for opp in self._pending:
            p = opp.platform
            platform_counts[p] = platform_counts.get(p, 0) + 1

        parts = [
            f"[{DIM}]{p.title()}:[/] {c}"
            for p, c in sorted(platform_counts.items(), key=lambda x: x[1], reverse=True)
        ]
        self.query_one("#platform-dist-text", Static).update(
            f"[bold {SUBTEXT}]Platforms:[/]  " + "  ".join(parts)
        )

    def _update_progress(self) -> None:
        total = len(self._pending)
        reviewed = self._reviewed_count
        pct = (reviewed / total * 100) if total else 0
        bar_filled = "━" * min(40, int(pct // 2.5))
        bar_empty = "━" * (40 - len(bar_filled))

        progress_text = (
            f"[{DIM}]Progress:[/] [{SUCCESS}]{bar_filled}[/][{DIM}]{bar_empty}[/]  "
            f"[bold]{reviewed}[/] of [bold]{total}[/] reviewed ({pct:.0f}%)"
        )
        self.query_one("#progress-row", Label).update(progress_text)

    # ── Action handlers ─────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = str(event.button.id)
        if btn_id == "btn-approve-all":
            self.run_worker(self._approve_all(), exclusive=True)
        elif btn_id == "btn-approve-selected":
            self.run_worker(self._approve_selected(), exclusive=True)
        elif btn_id == "btn-reject-selected":
            self.run_worker(self._reject_selected(), exclusive=True)
        elif btn_id == "btn-focus-mode":
            self._open_focus_mode()
        elif btn_id == "btn-refresh":
            self.run_worker(self._load_pending(), exclusive=True)
        elif btn_id == "btn-back":
            self.post_message(NavigateBackFromReview())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open the detail screen for in-depth review."""
        item = event.item
        if isinstance(item, ReviewListItem):
            self.post_message(NavigateToDetail(item.opportunity))

    # ── Bulk actions ────────────────────────────────────────────────────

    async def _approve_all(self) -> None:
        """Approve all pending leads."""
        self._hide_error()
        if not self._pending:
            return

        self.query_one("#status-message", Label).update("🔄  Approving all…")
        count = 0

        for opp in self._pending:
            try:
                # Update the draft if it exists
                drafts = await self._repository.get_drafts_for_opportunity(opp.id)
                for draft in drafts:
                    draft.approve()
                    await self._repository.update_draft(draft)

                await self._repository.update_status(
                    opp.id,
                    LeadStatus.REVIEWED,
                )
                count += 1
            except DatabaseError:
                continue

        self._reviewed_count += count
        self._pending = [opp for opp in self._pending if opp.status != LeadStatus.REVIEWED]
        self.pending_leads = self._pending
        self.total_pending = len(self._pending)

        self._render_list()
        self._update_stats()
        self._update_progress()
        self.query_one("#status-message", Label).update(f"[{SUCCESS}]✓ Approved {count} leads[/]")

        if not self._pending:
            self.post_message(
                ReviewAllComplete(
                    approved=count,
                    rejected=0,
                    total=count,
                )
            )

    async def _approve_selected(self) -> None:
        """Approve the currently selected lead."""
        list_view = self.query_one("#review-list", ListView)
        if list_view.index is None:
            return

        item = list_view.children[list_view.index]
        if not isinstance(item, ReviewListItem):
            return

        opp = item.opportunity
        try:
            drafts = await self._repository.get_drafts_for_opportunity(opp.id)
            for draft in drafts:
                draft.approve()
                await self._repository.update_draft(draft)

            await self._repository.update_status(
                opp.id,
                LeadStatus.REVIEWED,
            )
            self._reviewed_count += 1
            self._pending = [o for o in self._pending if o.id != opp.id]
            self.pending_leads = self._pending

            self._render_list()
            self._update_stats()
            self._update_progress()
            self.query_one("#status-message", Label).update(
                f"[{SUCCESS}]✓ Approved: {opp.title[:50]}[/]"
            )

        except DatabaseError as exc:
            self._show_error(f"Failed to approve: {exc}")

    async def _reject_selected(self) -> None:
        """Reject the currently selected lead."""
        list_view = self.query_one("#review-list", ListView)
        if list_view.index is None:
            return

        item = list_view.children[list_view.index]
        if not isinstance(item, ReviewListItem):
            return

        opp = item.opportunity
        try:
            await self._repository.update_status(
                opp.id,
                LeadStatus.REJECTED,
            )
            self._reviewed_count += 1
            self._pending = [o for o in self._pending if o.id != opp.id]
            self.pending_leads = self._pending

            self._render_list()
            self._update_stats()
            self._update_progress()
            self.query_one("#status-message", Label).update(
                f"[bold {ERROR}]✕ Rejected: {opp.title[:50]}[/]"
            )

        except DatabaseError as exc:
            self._show_error(f"Failed to reject: {exc}")

    # ── Focus mode ─────────────────────────────────────────────────────

    def _open_focus_mode(self) -> None:
        """Open the first pending lead in the detail screen.

        The detail screen provides one-at-a-time review with approve/reject/edit
        actions, then navigates back to the queue.
        """
        if not self._pending:
            self.query_one("#status-message", Label).update("[dim]No pending items to review[/]")
            return

        first = self._pending[0]
        self.post_message(NavigateToDetail(first))

    # ── Error handling ──────────────────────────────────────────────────

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.update(f"[bold]⚠ {message}[/]")
        banner.styles.display = "block"

    def _hide_error(self) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.styles.display = "none"

    # ── Keyboard navigation ─────────────────────────────────────────────

    async def key_j(self) -> None:
        """Navigate down (j)."""
        list_view = self.query_one("#review-list", ListView)
        list_view.action_cursor_down()

    async def key_k(self) -> None:
        """Navigate up (k)."""
        list_view = self.query_one("#review-list", ListView)
        list_view.action_cursor_up()

    async def key_space(self) -> None:
        """Open selected item in focus mode (space)."""
        self._open_focus_mode()

    def key_a(self) -> None:
        """Approve all (a)."""
        self.run_worker(self._approve_all(), exclusive=True)

    def key_r(self) -> None:
        """Reject selected (r)."""
        self.run_worker(self._reject_selected(), exclusive=True)

    def key_escape(self) -> None:
        """Back to dashboard."""
        self.post_message(NavigateBackFromReview())
