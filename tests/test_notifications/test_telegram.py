"""Tests for the Telegram notification sender."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from freelance_lead_gen.config.settings import get_settings
from freelance_lead_gen.notifications.telegram import TelegramNotifier


@pytest.fixture
def telegram_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Telegram settings at a fake bot/chat for notifier tests."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()


@pytest.fixture
def no_telegram_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure Telegram is unconfigured by overriding .env values with empty strings."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    get_settings.cache_clear()


def test_configured_property(telegram_settings: None) -> None:
    notifier = TelegramNotifier()
    assert notifier.configured is True


def test_not_configured(no_telegram_settings: None) -> None:
    notifier = TelegramNotifier()
    assert notifier.configured is False


@pytest.mark.asyncio
async def test_skips_when_not_configured(no_telegram_settings: None) -> None:
    notifier = TelegramNotifier()
    ok = await notifier.send("hello")
    assert ok is False
    assert notifier._client is None


@pytest.mark.asyncio
async def test_skips_empty_text(telegram_settings: None) -> None:
    notifier = TelegramNotifier()
    ok = await notifier.send("")
    assert ok is False


@pytest.mark.asyncio
async def test_send_success(telegram_settings: None) -> None:
    mock_post = AsyncMock()
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "ok"
    mock_client = AsyncMock()
    mock_client.post = mock_post

    with patch(
        "freelance_lead_gen.notifications.telegram.httpx.AsyncClient", return_value=mock_client
    ):
        notifier = TelegramNotifier()
        ok = await notifier.send("Pipeline selesai: 3 drafted")

    assert ok is True
    mock_post.assert_awaited_once()
    url = mock_post.call_args.args[0]
    assert url == "https://api.telegram.org/bot123:FAKE/sendMessage"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["chat_id"] == "42"
    assert payload["text"] == "Pipeline selesai: 3 drafted"
    assert payload["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_send_api_error_returns_false(telegram_settings: None) -> None:
    mock_post = AsyncMock()
    mock_post.return_value.status_code = 400
    mock_post.return_value.text = "Bad Request"
    mock_client = AsyncMock()
    mock_client.post = mock_post

    with patch(
        "freelance_lead_gen.notifications.telegram.httpx.AsyncClient", return_value=mock_client
    ):
        notifier = TelegramNotifier()
        ok = await notifier.send("test")

    assert ok is False


@pytest.mark.asyncio
async def test_send_network_error_returns_false(telegram_settings: None) -> None:
    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise TimeoutError("connection timed out")

    mock_client = AsyncMock()
    mock_client.post = _boom

    with patch(
        "freelance_lead_gen.notifications.telegram.httpx.AsyncClient", return_value=mock_client
    ):
        notifier = TelegramNotifier()
        ok = await notifier.send("test")

    assert ok is False


@pytest.mark.asyncio
async def test_aclose_releases_client(telegram_settings: None) -> None:
    mock_client = AsyncMock()
    with patch(
        "freelance_lead_gen.notifications.telegram.httpx.AsyncClient", return_value=mock_client
    ):
        notifier = TelegramNotifier()
        notifier._client = mock_client
        await notifier.aclose()

    mock_client.aclose.assert_awaited_once()
    assert notifier._client is None
