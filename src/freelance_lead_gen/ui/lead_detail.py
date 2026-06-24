"""Lead detail screen — split-pane view for human-in-the-loop review.

Displays the original job posting alongside the generated outreach draft
for side-by-side comparison and editing.  The HITL gateway ensures the
agent NEVER submits autonomously — every change requires human action.

Layout
------
LEFT pane:    Original job posting (scrollable, read-only)
RIGHT pane:   Generated outreach draft (editable via content editor)

Actions
-------
- Approve   → mark as reviewed, ready for submission
- Edit      → open content editor for inline editing
- Reject    → mark as rejected, with optional note
- Regenerate → request a new draft from the personalization agent
- Archive   → set aside without rejecting
"""

from __future__ import annotations as _annotations

from datetime import UTC, datetime

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Header, Input, Label, RichLog, Static

from freelance_lead_gen.models.opportunity import (
    LeadOpportunity,
    LeadStatus,
    OutboundDraft,
)
from freelance_lead_gen.storage.repository import (
    DatabaseError,
    OpportunityRepository,
)
from freelance_lead_gen.ui.content_editor import ContentEditor
from freelance_lead_gen.ui.widgets import (
    DIM,
    ERROR,
    SUCCESS,
    TEXT,
    format_budget,
    format_timestamp,
)

# ── Messages ─────────────────────────────────────────────────────────────


class LeadApproved(Message):
    """Posted when the user approves a lead."""

    def __init__(self, opportunity_id: str) -> None:
        self.opportunity_id = opportunity_id
        super().__init__()


class LeadRejected(Message):
    """Posted when the user rejects a lead."""

    def __init__(self, opportunity_id: str, reason: str | None = None) -> None:
        self.opportunity_id = opportunity_id
        self.reason = reason
        super().__init__()


class LeadArchived(Message):
    """Posted when the user archives a lead."""

    def __init__(self, opportunity_id: str) -> None:
        self.opportunity_id = opportunity_id
        super().__init__()


class RegenerateRequested(Message):
    """Posted when the user requests draft regeneration."""

    def __init__(self, opportunity_id: str) -> None:
        self.opportunity_id = opportunity_id
        super().__init__()


class NavigateBack(Message):
    """Posted when the user navigates away from detail view."""

    def __init__(self) -> None:
        super().__init__()


# ── LeadDetailScreen ─────────────────────────────────────────────────────


class LeadDetailScreen(Screen[None]):
    """Split-pane review screen for a single opportunity.

    LEFT:  Original job posting (read-only, scrollable).
    RIGHT: Generated outreach draft (read-only preview, editable via modal).
    """

    DEFAULT_CSS = """
    LeadDetailScreen {
        background: $surface;
    }

    LeadDetailScreen #detail-container {
        height: 100%;
    }

    LeadDetailScreen #detail-header {
        height: 3;
        padding: 0 1;
    }

    LeadDetailScreen #detail-header #detail-title {
        text-style: bold;
        color: $text;
        width: 1fr;
    }

    LeadDetailScreen #detail-header #detail-actions-info {
        color: $text-muted;
        width: auto;
    }

    LeadDetailScreen #panes {
        height: 1fr;
        padding: 0 1;
    }

    /* ── Left pane: original posting ─────────────────────── */
    LeadDetailScreen #left-pane {
        width: 1fr;
        min-width: 30;
        border: solid $border;
        margin-right: 1;
    }

    LeadDetailScreen #left-pane #left-header {
        height: 3;
        background: $boost;
        padding: 0 1;
    }

    LeadDetailScreen #left-pane #left-header > Label {
        text-style: bold;
        color: $text;
    }

    LeadDetailScreen #left-pane #job-content {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }

    LeadDetailScreen #left-pane #job-meta {
        height: auto;
        max-height: 6;
        border-top: solid $border;
        padding: 0 1;
    }

    /* ── Right pane: draft ───────────────────────────────── */
    LeadDetailScreen #right-pane {
        width: 1fr;
        min-width: 30;
        border: solid $border;
    }

    LeadDetailScreen #right-pane #right-header {
        height: 3;
        background: $boost;
        padding: 0 1;
    }

    LeadDetailScreen #right-pane #right-header > Label {
        text-style: bold;
        color: $text;
    }

    LeadDetailScreen #right-pane #draft-content {
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }

    LeadDetailScreen #right-pane #draft-meta {
        height: auto;
        max-height: 4;
        border-top: solid $border;
        padding: 0 1;
    }

    /* ── Action buttons ──────────────────────────────────── */
    LeadDetailScreen #action-row {
        height: 5;
        padding: 0 1;
        align: center middle;
    }

    LeadDetailScreen #action-row Button {
        margin: 0 1;
        min-width: 14;
    }

    LeadDetailScreen #btn-approve {
        background: $success 40%;
        color: $text;
    }

    LeadDetailScreen #btn-approve:hover {
        background: $success 60%;
    }

    LeadDetailScreen #btn-edit {
        background: $primary 40%;
        color: $text;
    }

    LeadDetailScreen #btn-edit:hover {
        background: $primary 60%;
    }

    LeadDetailScreen #btn-reject {
        background: $error 40%;
        color: $text;
    }

    LeadDetailScreen #btn-reject:hover {
        background: $error 60%;
    }

    LeadDetailScreen #btn-regenerate {
        background: $warning 30%;
        color: $text;
    }

    LeadDetailScreen #btn-regenerate:hover {
        background: $warning 50%;
    }

    LeadDetailScreen #btn-archive {
        background: $panel;
        color: $text-muted;
    }

    LeadDetailScreen #btn-archive:hover {
        background: $boost;
    }

    LeadDetailScreen #btn-back {
        background: $panel;
        color: $text-muted;
    }

    LeadDetailScreen #reject-reason-row {
        height: 3;
        padding: 0 1;
        display: none;
    }

    LeadDetailScreen #reject-reason-row Input {
        width: 1fr;
    }

    LeadDetailScreen #reject-reason-row Button {
        min-width: 8;
    }

    LeadDetailScreen #status-message {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }

    LeadDetailScreen #error-banner {
        height: auto;
        max-height: 3;
        background: $error 20%;
        color: $error;
        padding: 0 1;
        display: none;
    }
    """

    draft_body: str = reactive("")
    _current_draft: OutboundDraft | None = None

    def __init__(
        self,
        opportunity: LeadOpportunity,
        repository: OpportunityRepository,
    ) -> None:
        self._opportunity = opportunity
        self._repository = repository
        self._current_draft = None
        self._show_reject_reason = False
        super().__init__()

    # ── Screen lifecycle ─────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-container"):
            with Horizontal(id="detail-header"):
                yield Label("", id="detail-title")
                yield Label("q: back  a: approve  e: edit  r: reject", id="detail-actions-info")
            with Horizontal(id="panes"):
                # Left pane — original posting
                with Vertical(id="left-pane"):
                    with Horizontal(id="left-header"):
                        yield Label("📄  Original Job Posting")
                    with VerticalScroll(id="job-content"):
                        yield RichLog(id="posting-text", highlight=True, markup=True)
                    with Vertical(id="job-meta"):
                        yield Static("", id="job-meta-text")
                # Right pane — outreach draft
                with Vertical(id="right-pane"):
                    with Horizontal(id="right-header"):
                        yield Label("✉️  Outreach Draft")
                    with VerticalScroll(id="draft-content"):
                        yield RichLog(id="draft-text", highlight=True, markup=True)
                    with Vertical(id="draft-meta"):
                        yield Static("", id="draft-meta-text")
            # Reject reason input (hidden by default)
            with Horizontal(id="reject-reason-row"):
                yield Input(placeholder="Reason for rejection (optional)…", id="reject-input")
                yield Button("Confirm", id="btn-confirm-reject", variant="error")
            # Action buttons
            with Horizontal(id="action-row"):
                yield Button("✓  Approve", id="btn-approve")
                yield Button("✏️  Edit Draft", id="btn-edit")
                yield Button("✕  Reject", id="btn-reject")
                yield Button("🔄  Regenerate", id="btn-regenerate")
                yield Button("📦  Archive", id="btn-archive")
                yield Button("←  Back", id="btn-back")
            yield Label("", id="status-message")
            yield Static(id="error-banner", classes="error-banner")

    async def on_mount(self) -> None:
        """Load the opportunity data and its draft."""
        self._load_opportunity()
        await self._load_draft()

    # ── Data loading ────────────────────────────────────────────────────

    def _load_opportunity(self) -> None:
        """Populate the header and left pane with opportunity data."""
        opp = self._opportunity
        self.query_one("#detail-title", Label).update(f"  {opp.title}")

        # Left pane — posting text
        posting = self.query_one("#posting-text", RichLog)
        posting.clear()
        posting.write(f"[bold {TEXT}]{opp.description}[/]")

        # Meta info below the posting
        meta = self.query_one("#job-meta-text", Static)
        skills_str = ", ".join(opp.skills) if opp.skills else "None listed"
        meta.update(
            f"[{DIM}]Posted:[/] {format_timestamp(opp.posted_date)}  "
            f"[{DIM}]Budget:[/] {format_budget(opp.budget_min, opp.budget_max, opp.currency)}  "
            f"[{DIM}]Platform:[/] {opp.platform.title()}  "
            f"[{DIM}]Location:[/] {opp.location or 'Remote'}  \n"
            f"[{DIM}]Company:[/] {opp.company or 'N/A'}  "
            f"[{DIM}]Score:[/] [{'green' if (opp.score or 0) >= 60 else 'yellow'}]{opp.score or '—'}[/]  "
            f"[{DIM}]Status:[/] {opp.status.value.title()}  "
            f"[{DIM}]Skills:[/] {skills_str}"
        )

    async def _load_draft(self) -> None:
        """Load the latest draft for this opportunity."""
        try:
            drafts = await self._repository.get_drafts_for_opportunity(self._opportunity.id)
        except DatabaseError as exc:
            self._show_error(f"Failed to load draft: {exc}")
            return

        if drafts:
            self._current_draft = drafts[0]
            self.draft_body = self._current_draft.current_body or ""
        else:
            self._current_draft = None
            self.draft_body = "[dim]No draft generated yet[/]"

        self._render_draft()

    def _render_draft(self) -> None:
        """Render the draft content and metadata."""
        draft_text = self.query_one("#draft-text", RichLog)
        draft_text.clear()

        if self._current_draft:
            body = self._current_draft.current_body or ""
            draft_text.write(f"[{TEXT}]{body}[/]")

            meta = self.query_one("#draft-meta-text", Static)
            v_count = self._current_draft.version_count
            edited = " [dim](human-edited)[/]" if self._current_draft.human_edited else ""
            approved = " [green]✓ Approved[/]" if self._current_draft.approved else ""
            meta.update(
                f"[{DIM}]Version:[/] {self._current_draft.current_version_index + 1}/{v_count}  "
                f"[{DIM}]Words:[/] {len(body.split()) if body else 0}"
                f"{edited}{approved}"
            )
        else:
            draft_text.write("[dim italic]No draft available — generate one in the pipeline.[/]")
            self.query_one("#draft-meta-text", Static).update("")

    # ── Button handlers ─────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = str(event.button.id)
        if btn_id == "btn-approve":
            self.run_worker(self._approve(), exclusive=True)
        elif btn_id == "btn-edit":
            self._open_editor()
        elif btn_id == "btn-reject":
            self._toggle_reject_reason()
        elif btn_id == "btn-confirm-reject":
            self._confirm_reject()
        elif btn_id == "btn-regenerate":
            self.post_message(RegenerateRequested(self._opportunity.id))
        elif btn_id == "btn-archive":
            self.run_worker(self._archive(), exclusive=True)
        elif btn_id == "btn-back":
            self.post_message(NavigateBack())

    # ── Approve ─────────────────────────────────────────────────────────

    async def _approve(self) -> None:
        """Mark the draft as approved and update status."""
        self._hide_error()

        if self._current_draft is None:
            self._show_error("Cannot approve — no draft exists")
            return

        try:
            self._current_draft.approve()

            # Update draft in DB
            await self._repository.update_draft(self._current_draft)

            # Update opportunity status
            updated = await self._repository.update_status(
                self._opportunity.id,
                LeadStatus.REVIEWED,
                notes=f"Approved by human at {datetime.now(UTC).isoformat()}",
            )
            self._opportunity = updated

            self.query_one("#status-message", Label).update(
                f"[{SUCCESS}]✓ Lead approved and marked for submission[/]"
            )
            self._render_draft()
            self.post_message(LeadApproved(self._opportunity.id))

        except DatabaseError as exc:
            self._show_error(f"Failed to approve: {exc}")
        except Exception as exc:
            self._show_error(f"Unexpected error: {exc}")

    # ── Edit ─────────────────────────────────────────────────────────────

    def _open_editor(self) -> None:
        """Open the content editor modal for inline editing."""
        if self._current_draft is None:
            self._show_error("Cannot edit — no draft exists")
            return

        def on_editor_result(result: str | None) -> None:
            if result is not None:
                # User saved changes
                self._current_draft.add_version(result, set_current=True)
                self._current_draft.human_edited = True

                # Persist
                async def _save() -> None:
                    try:
                        await self._repository.update_draft(self._current_draft)
                        self.draft_body = result
                        self._render_draft()
                        self.query_one("#status-message", Label).update(
                            f"[{SUCCESS}]✓ Draft updated[/]"
                        )
                    except DatabaseError as exc:
                        self._show_error(f"Failed to save: {exc}")

                self.run_worker(_save(), exclusive=True)

        self.push_screen(
            ContentEditor(self._current_draft, self._repository),
            on_editor_result,
        )

    # ── Reject ──────────────────────────────────────────────────────────

    def _toggle_reject_reason(self) -> None:
        """Toggle the reject-reason input row visibility."""
        row = self.query_one("#reject-reason-row")
        self._show_reject_reason = not self._show_reject_reason
        row.styles.display = "block" if self._show_reject_reason else "none"
        if self._show_reject_reason:
            self.query_one("#reject-input", Input).focus()
        else:
            self.query_one("#reject-input", Input).value = ""

    def _confirm_reject(self) -> None:
        """Confirm rejection with optional reason."""
        reason_input = self.query_one("#reject-input", Input)
        reason = str(reason_input.value).strip() or None
        self.run_worker(self._reject(reason), exclusive=True)

    async def _reject(self, reason: str | None = None) -> None:
        """Mark the opportunity as rejected."""
        self._hide_error()
        try:
            notes = None
            if reason:
                notes = f"Rejected: {reason}"

            updated = await self._repository.update_status(
                self._opportunity.id,
                LeadStatus.REJECTED,
                notes=notes,
            )
            self._opportunity = updated

            self.query_one("#status-message", Label).update(
                f"[bold {ERROR}]✕ Lead rejected[/]" + (f" ({reason})" if reason else "")
            )
            self._toggle_reject_reason()
            self.post_message(LeadRejected(self._opportunity.id, reason=reason))

        except DatabaseError as exc:
            self._show_error(f"Failed to reject: {exc}")

    # ── Archive ─────────────────────────────────────────────────────────

    async def _archive(self) -> None:
        """Archive the opportunity."""
        self._hide_error()
        try:
            updated = await self._repository.update_status(
                self._opportunity.id,
                LeadStatus.ARCHIVED,
            )
            self._opportunity = updated

            self.query_one("#status-message", Label).update(f"[bold {DIM}]📦 Lead archived[/]")
            self.post_message(LeadArchived(self._opportunity.id))

        except DatabaseError as exc:
            self._show_error(f"Failed to archive: {exc}")

    # ── Error handling ──────────────────────────────────────────────────

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.update(f"[bold]⚠ {message}[/]")
        banner.styles.display = "block"

    def _hide_error(self) -> None:
        banner = self.query_one("#error-banner", Static)
        banner.styles.display = "none"

    # ── Keyboard shortcuts ─────────────────────────────────────────────

    def key_a(self) -> None:
        """Approve (a)."""
        self.run_worker(self._approve(), exclusive=True)

    def key_e(self) -> None:
        """Edit draft (e)."""
        self._open_editor()

    def key_r(self) -> None:
        """Reject (r)."""
        self._toggle_reject_reason()

    def key_g(self) -> None:
        """Regenerate (g)."""
        self.post_message(RegenerateRequested(self._opportunity.id))

    def key_escape(self) -> None:
        """Back / cancel reject."""
        if self._show_reject_reason:
            self._toggle_reject_reason()
        else:
            self.post_message(NavigateBack())
