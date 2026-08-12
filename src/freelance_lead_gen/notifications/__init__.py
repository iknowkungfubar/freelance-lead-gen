"""Notifications — push summaries of pipeline activity to chat platforms.

Currently supports Telegram via the Bot API.  All notifiers degrade to a
no-op when not configured so the rest of the pipeline never depends on a
notification provider being reachable.
"""

from __future__ import annotations

from freelance_lead_gen.notifications.telegram import TelegramNotifier

__all__ = ["TelegramNotifier"]
