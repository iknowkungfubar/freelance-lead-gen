"""Telegram notification sender backed by the Bot API.

Sends plain-text messages to a configured chat via ``sendMessage``.  The
sender is fire-and-forget from the pipeline's perspective: every failure is
logged and swallowed so a notification outage never disrupts screening.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from freelance_lead_gen.config.settings import Settings, get_settings

logger = structlog.get_logger(__name__)

_API_BASE: str = "https://api.telegram.org/bot{token}"


class TelegramNotifier:
    """Send messages to a Telegram chat through a bot token.

    Parameters
    ----------
    settings : Settings or None
        Application settings.  The Telegram section is read for the bot
        token, chat ID, and report toggle.
    timeout_seconds : float
        Per-request timeout.  Notifications are best-effort, so a short
        timeout keeps them from ever blocking the pipeline for long.

    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._settings: Settings = settings or get_settings()
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Whether a token and chat ID are present."""
        return self._settings.telegram.configured

    async def send(self, text: str, *, disable_notification: bool = False) -> bool:
        """Send *text* to the configured chat.

        Returns *True* on success, *False* if not configured or the API
        call failed.  Never raises.
        """
        if not self.configured:
            logger.debug("telegram.not_configured_skipping")
            return False
        if not text:
            return False

        token = self._settings.telegram.bot_token
        chat_id = self._settings.telegram.chat_id
        url = f"{_API_BASE.format(token=token)}/sendMessage"

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }

        try:
            client = self._client
            if client is None:
                client = httpx.AsyncClient(timeout=self._timeout_seconds)
                self._client = client
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.warning(
                    "telegram.send_failed",
                    status_code=response.status_code,
                    body=response.text[:300],
                )
                return False
            logger.info(
                "telegram.sent",
                chat_id=chat_id,
                chars=len(text),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("telegram.send_error", error=str(exc))
            return False

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if one was opened."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
