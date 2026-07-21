"""Reusable Textual widgets for the LeadGen TUI.

Provides composable UI components used across all screens:

- :class:`StatusBadge` — colored pipeline-status indicator
- :class:`ScoreGauge` — visual score bar (0-100)
- :class:`PlatformIcon` — platform indicator with colour
- :class:`StatsCard` — metric display widget
- :class:`ActivityFeed` — scrolling log of system events
- :class:`SearchBar` — filter input with debounce
"""

from __future__ import annotations as _annotations

from datetime import UTC, datetime

from rich.style import Style
from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

from freelance_lead_gen.models.opportunity import LeadStatus

# ── Colour palette (Tokyo Night inspired) ─────────────────────────────────

SURFACE = "#24283b"
TEXT = "#c0caf5"
SUBTEXT = "#a9b1d6"
PRIMARY = "#7aa2f7"
SECONDARY = "#7dcfff"
ACCENT = "#bb9af7"
SUCCESS = "#9ece6a"
WARNING = "#e0af68"
ERROR = "#f7768e"
INFO = "#2ac3de"
DIM = "#565f89"

_STATUS_COLORS: dict[LeadStatus, str] = {
    LeadStatus.DISCOVERED: INFO,
    LeadStatus.QUALIFIED: PRIMARY,
    LeadStatus.DRAFTED: WARNING,
    LeadStatus.REVIEWED: SUCCESS,
    LeadStatus.SUBMITTED: SUCCESS,
    LeadStatus.ARCHIVED: DIM,
    LeadStatus.REJECTED: ERROR,
}

_PLATFORM_COLORS: dict[str, str] = {
    "upwork": "#6fda44",
    "linkedin": "#0a66c2",
    "freelancer": "#29b2fe",
    "remote_ok": "#0bac4b",
    "yc_work": "#fb651e",
    "custom": ACCENT,
}

_PLATFORM_ICONS: dict[str, str] = {
    "upwork": "U",
    "linkedin": "in",
    "freelancer": "F",
    "remote_ok": "R",
    "yc_work": "Y",
    "custom": "*",
}


# ── Messages ──────────────────────────────────────────────────────────────


class FilterChanged(Message):
    """Posted when the search/filter query changes after debounce."""

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__()


# ── StatusBadge ───────────────────────────────────────────────────────────


class StatusBadge(Static):
    """A coloured pipeline-status indicator.

    Displays the status name on a coloured background with appropriate
    foreground contrast.  Handles all :class:`LeadStatus` values.
    """

    DEFAULT_CSS = """
    StatusBadge {
        padding: 0 1;
        min-width: 12;
        text-style: bold;
        text-align: center;
    }
    """

    def __init__(self, status: LeadStatus, *, compact: bool = False) -> None:
        self._lead_status = status
        self._compact = compact
        label = status.value[:5] if compact else status.value.title()
        super().__init__(label)

    def on_mount(self) -> None:
        self._apply_colour()

    def _apply_colour(self) -> None:
        colour = _STATUS_COLORS.get(self._lead_status, DIM)
        if self._lead_status in (LeadStatus.DISCOVERED, LeadStatus.QUALIFIED):
            self.styles.background = colour + "25"
            self.styles.color = colour
        elif self._lead_status in (LeadStatus.REVIEWED, LeadStatus.SUBMITTED, LeadStatus.ARCHIVED):
            self.styles.background = colour + "20"
            self.styles.color = colour
        else:
            self.styles.background = colour + "30"
            self.styles.color = colour


# ── ScoreGauge ────────────────────────────────────────────────────────────


class ScoreGauge(Widget):
    """Visual score bar (0-100) displayed as a filled gauge.

    The gauge colour shifts from red (low) through yellow to green (high).
    """

    DEFAULT_CSS = """
    ScoreGauge {
        height: 1;
        min-width: 16;
        margin: 0 1;
    }
    """

    score: reactive[int] = reactive(0)

    def __init__(self, score: int | None = None) -> None:
        super().__init__()
        if score is not None:
            self.score = max(0, min(100, score))

    def watch_score(self, _value: int) -> None:
        self.refresh()

    def render(self) -> Text:
        clamped = max(0, min(100, self.score))
        filled = clamped // 5  # 20 segments
        empty = 20 - filled

        if clamped >= 70:
            colour = SUCCESS
        elif clamped >= 40:
            colour = WARNING
        else:
            colour = ERROR

        bar = "█" * filled + "░" * empty
        label = f"{clamped:>3}"

        styled = Text.assemble(
            (f" {label} ", Style(bold=True, color=TEXT)),
            (bar, Style(color=colour)),
        )
        return styled


# ── PlatformIcon ──────────────────────────────────────────────────────────


class PlatformIcon(Static):
    """Platform indicator with a colour-coded icon and name."""

    DEFAULT_CSS = """
    PlatformIcon {
        padding: 0 1;
        text-style: bold;
    }
    """

    def __init__(self, platform: str) -> None:
        self._platform = platform.lower()
        display = _PLATFORM_ICONS.get(self._platform, self._platform[:3].upper())
        super().__init__(f" {display} ")

    def on_mount(self) -> None:
        colour = _PLATFORM_COLORS.get(self._platform, ACCENT)
        self.styles.background = colour + "30"
        self.styles.color = colour


# ── StatsCard ─────────────────────────────────────────────────────────────


class StatsCard(Widget):
    """Metric display widget showing a label, value, and optional delta.

    Renders a compact card for dashboard-style statistic displays.
    """

    DEFAULT_CSS = """
    StatsCard {
        width: 18;
        height: 3;
        padding: 0 1;
        border: solid $primary;
        border-title-color: $text;
    }
    """

    label: str = reactive("")
    value: str | int = reactive("—")
    delta: str | None = reactive(None)

    def __init__(
        self,
        label: str = "",
        value: str | int = "—",
        delta: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.value = value
        self.delta = delta

    def render(self) -> Text:
        val_str = str(self.value)
        if self.delta:
            delta_colour = SUCCESS if not self.delta.startswith("-") else ERROR
            return Text.assemble(
                (f"{self.label}\n", Style(color=SUBTEXT)),
                (val_str, Style(bold=True, color=TEXT, italic=True)),
                (f" {self.delta}", Style(color=delta_colour)),
            )
        return Text.assemble(
            (f"{self.label}\n", Style(color=SUBTEXT)),
            (val_str, Style(bold=True, color=TEXT, italic=True)),
        )


# ── ActivityFeed ──────────────────────────────────────────────────────────


class ActivityEntry:
    """A single entry in the activity feed.

    Attributes
    ----------
    timestamp : datetime
        When the activity occurred.
    message : str
        The activity description.
    level : str
        One of ``"info"``, ``"success"``, ``"warning"``, ``"error"``.

    """

    def __init__(
        self,
        message: str,
        level: str = "info",
        timestamp: datetime | None = None,
    ) -> None:
        self.timestamp = timestamp or datetime.now(UTC)
        self.message = message
        self.level = level

    def render(self) -> Text:
        time_str = self.timestamp.strftime("%H:%M:%S")
        colours = {
            "info": PRIMARY,
            "success": SUCCESS,
            "warning": WARNING,
            "error": ERROR,
        }
        dot_color = colours.get(self.level, DIM)
        return Text.assemble(
            (f" {time_str} ", Style(color=DIM)),
            ("●", Style(color=dot_color)),
            (f" {self.message}", Style(color=TEXT)),
        )


class ActivityFeed(Static):
    """Scrolling log of system events.

    Stores up to *max_entries* events and auto-scrolls as new ones arrive.
    """

    DEFAULT_CSS = """
    ActivityFeed {
        height: 100%;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    max_entries: int = 100

    def __init__(self, max_entries: int = 100, **kwargs: object) -> None:
        self.max_entries = max_entries
        self._entries: list[ActivityEntry] = []
        super().__init__(**kwargs)

    def add_entry(
        self,
        message: str,
        level: str = "info",
        timestamp: datetime | None = None,
    ) -> None:
        """Append a new entry and trim to *max_entries*."""
        entry = ActivityEntry(message, level=level, timestamp=timestamp)
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)
        self.refresh()
        self.scroll_end(animate=False)

    def clear_entries(self) -> None:
        """Remove all entries from the feed."""
        self._entries.clear()
        self.refresh()

    def render(self) -> Text:
        if not self._entries:
            return Text("  No activity yet", style=Style(color=DIM, italic=True))
        lines = [e.render() for e in self._entries[-self.max_entries :]]
        return Text("\n".join(str(t) for t in lines))


# ── SearchBar ─────────────────────────────────────────────────────────────


class SearchBar(Input):
    """Filter input with debounce for live search-as-you-type.

    Emits a :class:`FilterChanged` message *debounce_ms* milliseconds after
    the user stops typing.
    """

    debounce_ms: int = 300
    _timer: float = 0

    def __init__(self, placeholder: str = "Search…", debounce_ms: int = 300) -> None:
        self.debounce_ms = debounce_ms
        super().__init__(placeholder=placeholder)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._debounce_filter(str(event.value))

    def _debounce_filter(self, query: str) -> None:
        self.set_timer(
            self.debounce_ms / 1000,
            lambda q=query: self._emit_filter(q),
        )

    def _emit_filter(self, query: str) -> None:
        self.post_message(FilterChanged(query))


# ── Budget display helper ─────────────────────────────────────────────────


def format_budget(min_val: float | None, max_val: float | None, currency: str = "USD") -> str:
    """Format a budget range as a human-readable string.

    Parameters
    ----------
    min_val : float or None
        Minimum budget amount.
    max_val : float or None
        Maximum budget amount.
    currency : str
        ISO 4217 currency code (default USD).

    Returns
    -------
    str
        Formatted budget string (e.g. ``"$50-100/hr"``, ``"$5k fixed"``).

    """
    symbol = "$" if currency == "USD" else f"{currency} "
    if min_val is not None and max_val is not None:
        if min_val == max_val:
            return f"{symbol}{min_val:,.0f}"
        return f"{symbol}{min_val:,.0f} – {symbol}{max_val:,.0f}"
    if min_val is not None:
        return f"{symbol}{min_val:,.0f}+"
    if max_val is not None:
        return f"Up to {symbol}{max_val:,.0f}"
    return "Budget N/A"


def format_timestamp(dt: datetime | None) -> str:
    """Format an optional datetime as a short relative or absolute string.

    Parameters
    ----------
    dt : datetime or None
        The timestamp to format.

    Returns
    -------
    str
        Formatted string (e.g. ``"2h ago"`` or ``"2026-06-24"``).

    """
    if dt is None:
        return "—"
    now = datetime.now(UTC)
    diff = now - dt
    if diff.total_seconds() < 60:
        return "just now"
    if diff.total_seconds() < 3600:
        mins = int(diff.total_seconds() // 60)
        return f"{mins}m ago"
    if diff.total_seconds() < 86400:
        hours = int(diff.total_seconds() // 3600)
        return f"{hours}h ago"
    if diff.days < 7:
        return f"{diff.days}d ago"
    return dt.strftime("%Y-%m-%d")
