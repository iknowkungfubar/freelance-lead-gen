"""Inline content editor widget for outreach draft editing.

Provides a Textual-based text editor for reviewing and editing generated
outreach drafts.  Features include:

- Syntax highlighting of markdown-like formatting
- Character and word count display
- Anti-AI flag detection in text
- Save/Cancel actions
- Version diff (shows edits from the generated version)
- Auto-save to the database on exit
"""

from __future__ import annotations as _annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from freelance_lead_gen.models.opportunity import OutboundDraft
    from freelance_lead_gen.storage.repository import OpportunityRepository

# ── Colours (matching Tokyo Night palette) ───────────────────────────────

TEXT = "#c0caf5"
SUBTEXT = "#a9b1d6"
PRIMARY = "#7aa2f7"
SUCCESS = "#9ece6a"
WARNING = "#e0af68"
ERROR = "#f7768e"
DIM = "#565f89"
SURFACE = "#24283b"

# ── Anti-AI flag patterns ─────────────────────────────────────────────────

ANTI_AI_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(as an AI|I'm an AI|as a language model|I cannot)\b", re.IGNORECASE),
    re.compile(r"\b(I don't have personal|I don't have access)\b", re.IGNORECASE),
    re.compile(
        r"\b(As a helpful|I'm here to help|let me know if you have any questions)\b", re.IGNORECASE
    ),
    re.compile(r"\b(Please let me know if|Feel free to reach out)\b", re.IGNORECASE),
    re.compile(r"\b(I hope this message finds you well|I am writing to express)\b", re.IGNORECASE),
]

# ── AFK detection pattern (AI-sounding boilerplate) ───────────────────────

AFK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bdedicated\s+(to\s+)?delivering\b", re.IGNORECASE),
    re.compile(r"\bpassionate\s+about\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+dive\s+into\b", re.IGNORECASE),
    re.compile(r"\bI\s+believe\s+(that\s+)?(my|our)\b", re.IGNORECASE),
    re.compile(r"\bI\s+would\s+be\s+a\s+great\s+fit\b", re.IGNORECASE),
    re.compile(r"\bI\s+look\s+forward\s+to\s+(the\s+)?opportunity\b", re.IGNORECASE),
    re.compile(r"\bbest\s+(suited|qualified)\s+for\b", re.IGNORECASE),
    re.compile(r"\bthrive\s+in\s+a\s+(fast[ -]paced|dynamic)\b", re.IGNORECASE),
    re.compile(r"\bresults[ -]driven\b", re.IGNORECASE),
    re.compile(r"\bproven\s+track\s+record\b", re.IGNORECASE),
]


# ── Messages ──────────────────────────────────────────────────────────────


class EditorSaved(Message):
    """Posted when the user saves changes in the editor."""

    def __init__(self, new_body: str) -> None:
        self.new_body = new_body
        super().__init__()


class EditorDiscarded(Message):
    """Posted when the user discards changes."""

    def __init__(self) -> None:
        super().__init__()


# ── Anti-AI flag analyser ────────────────────────────────────────────────


def _detect_ai_flags(text: str) -> list[dict[str, Any]]:
    """Scan *text* for anti-AI and AFK patterns.

    Returns a list of dicts with keys ``pattern``, ``match``, ``position``,
    and ``type`` (``"anti_ai"`` or ``"afk"``).
    """
    flags: list[dict[str, Any]] = []
    for pattern in ANTI_AI_PATTERNS:
        for match in pattern.finditer(text):
            flags.append(
                {
                    "pattern": pattern.pattern,
                    "match": match.group(),
                    "position": match.start(),
                    "type": "anti_ai",
                }
            )
    for pattern in AFK_PATTERNS:
        for match in pattern.finditer(text):
            flags.append(
                {
                    "pattern": pattern.pattern,
                    "match": match.group(),
                    "position": match.start(),
                    "type": "afk",
                }
            )
    return flags


# ── ContentEditor ────────────────────────────────────────────────────────


class ContentEditor(ModalScreen[None]):
    """Inline text editor for editing outreach drafts.

    Presents a full-screen editor for reviewing and modifying a generated
    draft.  Shows character/word counts, anti-AI warnings, and the original
    version for comparison.
    """

    DEFAULT_CSS = """
    ContentEditor {
        align: center middle;
        background: $surface 80%;
    }

    ContentEditor > #editor-container {
        width: 90%;
        height: 90%;
        background: $panel;
        border: thick $primary;
        padding: 0 1;
    }

    ContentEditor #editor-title {
        text-style: bold;
        color: $text;
        padding: 1 0;
    }

    ContentEditor #editor-area {
        width: 100%;
        height: 1fr;
        border: solid $border;
        background: $surface;
        color: $text;
    }

    ContentEditor #editor-stats {
        height: 1;
        padding: 0 1;
    }

    ContentEditor #editor-warnings {
        height: auto;
        max-height: 4;
        overflow-y: auto;
        padding: 0 1;
    }

    ContentEditor #editor-buttons Horizontal {
        height: 3;
        align: center middle;
        padding: 0 1;
    }

    ContentEditor #editor-buttons Button {
        margin: 0 1;
    }

    ContentEditor #save-btn {
        background: $success;
        color: $text;
    }

    ContentEditor #cancel-btn {
        background: $error 60%;
        color: $text;
    }
    """

    editor_text: str = reactive("")

    def __init__(
        self,
        draft: OutboundDraft,
        repository: OpportunityRepository,
    ) -> None:
        self._draft = draft
        self._repository = repository
        self._original_body = draft.current_body or ""
        self._has_changes = False
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-container"):
            yield Static(
                f"✏️  Editing Draft — {self._draft.opportunity_id}",
                id="editor-title",
            )
            yield TextArea(
                self._original_body,
                id="editor-area",
                language="markdown",
                show_line_numbers=True,
            )
            yield Static(id="editor-stats")
            yield Static(id="editor-warnings")
            with Horizontal(id="editor-buttons"):
                yield Button("💾  Save", variant="success", id="save-btn")
                yield Button("✕  Cancel", variant="error", id="cancel-btn")

    def on_mount(self) -> None:
        self._update_stats()
        self._update_warnings("")

    def watch_editor_text(self, value: str) -> None:
        self._has_changes = value != self._original_body

    # ── Button handlers ──────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self._save_and_close()
        elif event.button.id == "cancel-btn":
            self._cancel_and_close()

    # ── Text change handler ──────────────────────────────────────────────

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.editor_text = str(event.text_area.value)
        self._update_stats()
        self._update_warnings(str(event.text_area.value))

    # ── Internal helpers ─────────────────────────────────────────────────

    def _update_stats(self) -> None:
        body = self.editor_text or self._original_body
        char_count = len(body)
        word_count = len(body.split()) if body else 0
        stats_widget = self.query_one("#editor-stats", Static)
        changes = ""
        if self._has_changes:
            diff = sum(
                1
                for line in difflib.unified_diff(
                    self._original_body.splitlines(),
                    body.splitlines(),
                    n=0,
                )
                if line.startswith(("+", "-"))
            )
            changes = f"  [bold]{SUCCESS}▲+[/] [dim]{diff} changes[/]"

        prefix = (char_count > 500 and "[bold]red[/]") or "[bold]"
        stats_widget.update(f"{prefix}{char_count} chars[/]  [bold]{word_count} words[/]{changes}")

    def _update_warnings(self, text: str) -> None:
        warnings_widget = self.query_one("#editor-warnings", Static)
        if not text.strip():
            warnings_widget.update("[dim]Waiting for content…[/]")
            return

        flags = _detect_ai_flags(text)
        if not flags:
            warnings_widget.update(f"[{SUCCESS}]✓ No AI flags detected[/]")
            return

        anti_ai = [f for f in flags if f["type"] == "anti_ai"]
        afk = [f for f in flags if f["type"] == "afk"]
        parts: list[str] = []
        if anti_ai:
            parts.append(f"[bold {ERROR}]⚠ {len(anti_ai)} anti-AI pattern(s) detected[/]")
        if afk:
            parts.append(f"[bold {WARNING}]⚠ {len(afk)} AFK (sounds-like-AI) pattern(s)[/]")
        warnings_widget.update(" | ".join(parts))

    def _save_and_close(self) -> None:
        body = self.editor_text or self._original_body
        self.dismiss(body)

    def _cancel_and_close(self) -> None:
        self.dismiss(None)

    # ── Keyboard shortcuts ───────────────────────────────────────────────

    def key_escape(self) -> None:
        """Cancel editing (Escape)."""
        self._cancel_and_close()

    def key_ctrl_s(self) -> None:
        """Save (Ctrl+S)."""
        self._save_and_close()
