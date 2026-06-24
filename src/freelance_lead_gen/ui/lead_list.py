"""Lead list screen — scrollable, filterable, sortable list of opportunities.

Provides a paginated list view with:

- Colour-coded status badges and score gauges
- Search/filter bar with debounce
- Sortable columns (date, score, platform, status)
- Pagination (50 per page) with page-navigation controls
- Full keyboard navigation (vim keys)
- Responsive layout for terminal resize
"""

from __future__ import annotations as _annotations

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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
    SECONDARY,
    SUBTEXT,
    SUCCESS,
    TEXT,
    WARNING,
    FilterChanged,
    SearchBar,
    format_budget,
    format_timestamp,
)

# ── Pagination ───────────────────────────────────────────────────────────

PAGE_SIZE: int = 50

# ── Sort keys ────────────────────────────────────────────────────────────

SortKey = str
SORT_DATE = "date"
SORT_SCORE = "score"
SORT_PLATFORM = "platform"
SORT_STATUS = "status"

_SORT_OPTIONS: dict[SortKey, str] = {
    SORT_DATE: "Date",
    SORT_SCORE: "Score",
    SORT_PLATFORM: "Platform",
    SORT_STATUS: "Status",
}


# ── Messages ─────────────────────────────────────────────────────────────


class LeadSelected(Message):
    """Posted when the user selects a lead to view details."""

    def __init__(self, opportunity: LeadOpportunity) -> None:
        self.opportunity = opportunity
        super().__init__()


class NavigateBackToDashboard(Message):
    """Posted when the user requests to go back."""

    def __init__(self) -> None:
        super().__init__()


# ── LeadListItem ─────────────────────────────────────────────────────────


class LeadListItem(ListItem):
    """A single lead entry rendered in the list.

    Displays: title, platform, company, budget, score gauge, status badge.
    Colour-coded by status.
    """

    def __init__(self, opportunity: LeadOpportunity) -> None:
        self.opportunity = opportunity
        super().__init__()

    def render(self) -> Text:
        opp = self.opportunity
        status_colour = {
            LeadStatus.DISCOVERED: PRIMARY,
            LeadStatus.QUALIFIED: SECONDARY,
            LeadStatus.DRAFTED: WARNING,
            LeadStatus.REVIEWED: SUCCESS,
            LeadStatus.SUBMITTED: SUCCESS,
            LeadStatus.ARCHIVED: DIM,
            LeadStatus.REJECTED: ERROR,
        }.get(opp.status, DIM)

        # Determine row colour based on status
        prefix = "▸" if opp.status in (LeadStatus.DRAFTED, LeadStatus.QUALIFIED) else " "

        platform_name = opp.platform.title()
        score_str = f"{opp.score:>3}" if opp.score is not None else " —"

        budget = format_budget(opp.budget_min, opp.budget_max, opp.currency)
        company = opp.company or "──"
        posted = format_timestamp(opp.posted_date)

        return Text.assemble(
            (f" {prefix} ", Style(color=DIM)),
            (f"{opp.title[:65]:<65}", Style(color=TEXT, bold=True)),
            (f"  {platform_name:<12}", Style(color=PRIMARY)),
            (f"{company:<20}", Style(color=SUBTEXT)),
            (f"{budget:<18}", Style(color=SUBTEXT)),
            (
                f" {score_str} ",
                Style(color=SUCCESS if (opp.score or 0) >= 60 else WARNING, bold=True),
            ),
            (f"  {opp.status.value[:8]}", Style(color=status_colour, italic=True)),
            (f"  {posted:<8}", Style(color=DIM)),
        )


# ── LeadListScreen ───────────────────────────────────────────────────────


class LeadListScreen(Screen[None]):
    """Scrollable, filterable list of opportunities.

    Supports:
    - Search/filter via search bar
    - Sort by date, score, platform, status
    - Pagination (50/page)
    - Vim-key navigation (j/k)
    """

    DEFAULT_CSS = """
    LeadListScreen {
        background: $surface;
    }

    LeadListScreen #list-container {
        height: 100%;
    }

    LeadListScreen #list-header {
        height: 3;
        padding: 0 1;
    }

    LeadListScreen #list-header > #list-title {
        text-style: bold;
        color: $text;
    }

    LeadListScreen #search-row {
        height: 3;
        padding: 0 1;
    }

    LeadListScreen #search-row > SearchBar {
        width: 1fr;
    }

    LeadListScreen #search-row > #sort-selector {
        width: 20;
        margin-left: 1;
    }

    LeadListScreen #list-view {
        height: 1fr;
        border: solid $border;
        margin: 0 1;
    }

    LeadListScreen #list-view:focus-within {
        border: solid $primary;
    }

    LeadListScreen #pagination-row {
        height: 3;
        padding: 0 1;
        align: center middle;
    }

    LeadListScreen #pagination-row > * {
        margin: 0 1;
    }

    LeadListScreen #pagination-row #page-info {
        width: 18;
        text-align: center;
        color: $text-muted;
    }

    LeadListScreen #pagination-row Button {
        min-width: 10;
    }

    LeadListScreen #empty-state {
        height: 1fr;
        align: center middle;
        color: $text-muted;
        text-style: italic;
    }

    LeadListScreen #status-bar {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    LeadListScreen #error-banner {
        height: auto;
        max-height: 3;
        background: $error 20%;
        color: $error;
        padding: 0 1;
        display: none;
    }

    LeadListScreen #summary-row {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    current_page: int = reactive(1)
    total_count: int = reactive(0)
    sort_key: str = reactive(SORT_DATE)
    search_query: str = reactive("")
    _data: list[LeadOpportunity]

    def __init__(self, repository: OpportunityRepository) -> None:
        self._repository = repository
        self._sort_key = SORT_DATE
        self._data = []
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="list-container"):
            with Horizontal(id="list-header"):
                yield Label("📋  Opportunities", id="list-title")
            with Horizontal(id="search-row"):
                yield SearchBar(placeholder="🔍  Search title, company, skills…")
                # Sort selector rendered as labelled buttons
                with Horizontal(id="sort-selector"):
                    yield Label("Sort:", classes="sort-label")
                    yield Static("Date", id="sort-date", classes="sort-option")
                    yield Static("Score", id="sort-score", classes="sort-option")
                    yield Static("Plat.", id="sort-platform", classes="sort-option")
                    yield Static("Status", id="sort-status", classes="sort-option")
            with VerticalScroll(id="list-view"):
                yield ListView(id="lead-list")
            yield Label("", id="summary-row")
            with Horizontal(id="pagination-row"):
                yield Button("◀  Prev", id="btn-prev", variant="default")
                yield Static("Page 1 of 1", id="page-info")
                yield Button("Next  ▶", id="btn-next", variant="default")
            yield Label("", id="status-bar")
            yield Static(id="error-banner", classes="error-banner")

    def on_mount(self) -> None:
        """Load initial data on mount."""
        self._set_sort_active()
        self.run_worker(self._load_data(), exclusive=True)

    async def on_screen_resume(self) -> None:
        """Refresh when the screen becomes active again."""
        self.run_worker(self._load_data(), exclusive=True)

    # ── Reactive watchers ───────────────────────────────────────────────

    def watch_current_page(self, _page: int) -> None:
        self.run_worker(self._load_data(), exclusive=True)

    def watch_sort_key(self, _key: str) -> None:
        self._set_sort_active()
        self.run_worker(self._load_data(), exclusive=True)

    # ── Filter handling ─────────────────────────────────────────────────

    def on_filter_changed(self, event: FilterChanged) -> None:
        self.search_query = event.query
        self.current_page = 1
        self.run_worker(self._load_data(), exclusive=True)

    # ── Sort handling ───────────────────────────────────────────────────

    def _set_sort_active(self) -> None:
        """Highlight the active sort option."""
        for key in (SORT_DATE, SORT_SCORE, SORT_PLATFORM, SORT_STATUS):
            widget = self.query_one(f"#sort-{key}", Static)
            if key == self.sort_key:
                widget.styles.text_style = "bold"
                widget.styles.color = PRIMARY
            else:
                widget.styles.text_style = "normal"
                widget.styles.color = DIM

    def _on_sort_click(self, key: str) -> None:
        self.sort_key = key
        self.current_page = 1

    def on_static_clicked(self, event: Static.Clicked) -> None:
        target = str(event.widget.id or "")
        if target.startswith("sort-"):
            key = target.replace("sort-", "")
            self._on_sort_click(key)

    # ── Pagination ──────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-prev":
            if self.current_page > 1:
                self.current_page -= 1
        elif event.button.id == "btn-next":
            total_pages = max(1, (self.total_count + PAGE_SIZE - 1) // PAGE_SIZE)
            if self.current_page < total_pages:
                self.current_page += 1
        elif event.button.id == "btn-back":
            self.post_message(NavigateBackToDashboard())

    # ── Data loading ────────────────────────────────────────────────────

    async def _load_data(self) -> None:
        """Fetch opportunities from the repository."""
        self._hide_error()
        self.query_one("#status-bar", Label).update("🔄  Loading…")

        try:
            offset = (self.current_page - 1) * PAGE_SIZE

            if self.search_query.strip():
                opps = await self._repository.search(
                    text_query=self.search_query.strip(),
                    limit=PAGE_SIZE,
                    offset=offset,
                )
                total = len(opps)
            else:
                opps, total = await self._repository.list_paginated(
                    limit=PAGE_SIZE,
                    offset=offset,
                )

            self._data = opps
            self.total_count = total

            self._render_list(opps)
            self._update_pagination()
            self._update_summary()

        except DatabaseError as exc:
            self._show_error(f"Database error: {exc}")
        except Exception as exc:
            self._show_error(f"Failed to load: {exc}")

    def _build_sort_expression(self) -> str:
        """Build a SQL ORDER BY expression from the current sort key."""
        mapping = {
            SORT_DATE: "created_at DESC",
            SORT_SCORE: "score DESC",
            SORT_PLATFORM: "platform ASC",
            SORT_STATUS: "status ASC",
        }
        return mapping.get(self.sort_key, "created_at DESC")

    # ── Rendering ───────────────────────────────────────────────────────

    def _render_list(self, opps: list[LeadOpportunity]) -> None:
        """Populate the list view with lead items."""
        list_view = self.query_one("#lead-list", ListView)
        list_view.clear()

        if not opps:
            list_view.mount(ListItem(Static("  No opportunities found", classes="empty-state")))
            return

        for opp in opps:
            list_view.mount(LeadListItem(opp))

        # Focus first item
        if list_view.children:
            list_view.index = 0

    def _update_pagination(self) -> None:
        total_pages = max(1, (self.total_count + PAGE_SIZE - 1) // PAGE_SIZE)
        self.query_one("#page-info", Static).update(f"Page {self.current_page} of {total_pages}")

        prev_btn = self.query_one("#btn-prev", Button)
        next_btn = self.query_one("#btn-next", Button)
        prev_btn.disabled = self.current_page <= 1
        next_btn.disabled = self.current_page >= total_pages

    def _update_summary(self) -> None:
        """Show a summary row with counts."""
        summary = self.query_one("#summary-row", Label)
        if self.search_query.strip():
            summary.update(
                f"[{DIM}]Filtered: {len(self._data)} of {self.total_count} total "
                f'| Query: "{self.search_query.strip()}"[/]'
            )
        else:
            # Count by status
            status_counts: dict[str, int] = {}
            for opp in self._data:
                s = opp.status.value
                status_counts[s] = status_counts.get(s, 0) + 1
            parts = " ".join(f"[{DIM}]{k}:[/] {v}" for k, v in sorted(status_counts.items()))
            summary.update(f"[{DIM}]Showing {len(self._data)} of {self.total_count} | {parts}[/]")

        self.query_one("#status-bar", Label).update(f"✓  Loaded {len(self._data)} opportunities")

    # ── Error handling ──────────────────────────────────────────────────

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.update(f"[bold]⚠ {message}[/]")
        banner.styles.display = "block"
        self.query_one("#status-bar", Label).update(f"[{ERROR}]⚠ {message}[/]")

    def _hide_error(self) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.styles.display = "none"

    # ── Selection ───────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle lead selection."""
        item = event.item
        if isinstance(item, LeadListItem):
            self.post_message(LeadSelected(item.opportunity))

    # ── Keyboard navigation ─────────────────────────────────────────────

    async def key_j(self) -> None:
        """Navigate down (j)."""
        list_view = self.query_one("#lead-list", ListView)
        list_view.action_cursor_down()

    async def key_k(self) -> None:
        """Navigate up (k)."""
        list_view = self.query_one("#lead-list", ListView)
        list_view.action_cursor_up()

    async def key_space(self) -> None:
        """Select lead (space)."""
        list_view = self.query_one("#lead-list", ListView)
        list_view.action_select()

    def key_g(self) -> None:
        """Go to first page (g)."""
        self.current_page = 1

    def key_G(self) -> None:
        """Go to last page (G)."""
        total_pages = max(1, (self.total_count + PAGE_SIZE - 1) // PAGE_SIZE)
        self.current_page = total_pages

    def key_n(self) -> None:
        """Next page (n)."""
        total_pages = max(1, (self.total_count + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.current_page < total_pages:
            self.current_page += 1

    def key_p(self) -> None:
        """Previous page (p)."""
        if self.current_page > 1:
            self.current_page -= 1

    def key_slash(self) -> None:
        """Focus search bar (/)."""
        self.query_one(SearchBar).focus()

    def key_escape(self) -> None:
        """Blur search / go back."""
        if self.query_one(SearchBar).has_focus:
            self.query_one(SearchBar).action_blur()
        else:
            self.post_message(NavigateBackToDashboard())
