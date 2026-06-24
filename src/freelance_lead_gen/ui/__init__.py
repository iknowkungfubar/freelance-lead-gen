"""Terminal UI package for the freelance lead generation system.

Provides a full-featured **Textual** terminal interface with:

- :class:`DashboardScreen` — aggregate stats, platform breakdowns, recent activity
- :class:`LeadListScreen` — scrollable, filterable, sortable opportunity list
- :class:`LeadDetailScreen` — split-pane review (posting + draft) with HITL actions
- :class:`ReviewQueueScreen` — batch review workflow for drafted opportunities
- :class:`ContentEditor` — modal inline text editor for outreach drafts
- :class:`LeadGenTUI` — main application entry point with key bindings

The UI enforces the **human-in-the-loop** gateway: the agent discovers,
scores, and drafts, but **never** submits autonomously. Every approval is
an explicit human action.
"""

from __future__ import annotations as _annotations

from freelance_lead_gen.ui.app import LeadGenTUI, run_tui
from freelance_lead_gen.ui.content_editor import ContentEditor
from freelance_lead_gen.ui.dashboard import DashboardScreen
from freelance_lead_gen.ui.lead_detail import LeadDetailScreen
from freelance_lead_gen.ui.lead_list import LeadListScreen
from freelance_lead_gen.ui.review_queue import ReviewQueueScreen

__all__ = [
    "ContentEditor",
    "DashboardScreen",
    "LeadDetailScreen",
    "LeadGenTUI",
    "LeadListScreen",
    "ReviewQueueScreen",
    "run_tui",
]
