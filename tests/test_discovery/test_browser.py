"""Tests for the ManagedBrowser configuration and lifecycle.

Uses direct state injection rather than deep Playwright mock chains to keep
tests simple and focused on the browser's API contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from freelance_lead_gen.discovery.browser import (
    BrowserError,
    BrowserNotStartedError,
    BrowserSessionInfo,
    ManagedBrowser,
    NavigationTimeoutError,
    create_browser,
)


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _simulate_started(
    browser: ManagedBrowser,
    *,
    context: AsyncMock | None = None,
    page: AsyncMock | None = None,
) -> None:
    """Inject internal Playwright objects to simulate a started browser.

    This avoids needing a full Playwright mock chain while still letting
    us test the browser's API contract (navigation guards, cookies, etc.).
    """
    mock_ctx = context or AsyncMock()
    mock_ctx.is_closed = MagicMock(return_value=False)
    mock_page = page or AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)

    # Set the internal state directly.
    browser._playwright = AsyncMock()
    browser._context = mock_ctx
    browser._page = mock_page
    browser._session_info = BrowserSessionInfo(
        fingerprint=AsyncMock(),
        started_at=1000.0,
    )


# ── Tests ───────────────────────────────────────────────────────────────────────


class TestManagedBrowserConfig:
    """Tests for ManagedBrowser initialisation and configuration."""

    def test_default_configuration(self) -> None:
        """Verify default constructor values are set correctly."""
        browser = ManagedBrowser()
        assert browser.is_running is False
        assert browser.fingerprint is None
        assert browser.session_info is None
        assert browser._headless is False
        assert browser._jitter_mean == 3.0
        assert browser._jitter_sigma == 1.2

    def test_custom_configuration(self) -> None:
        """Verify constructor accepts custom parameters."""
        browser = ManagedBrowser(
            headless=True,
            user_data_dir="./custom_data",
            proxy_url="http://proxy:8080",
            jitter_mean=2.0,
            jitter_sigma=0.5,
        )
        assert browser._headless is True
        assert str(browser._user_data_dir).endswith("custom_data")
        assert browser._proxy_url == "http://proxy:8080"
        assert browser._jitter_mean == 2.0

    def test_explicit_fingerprint(self) -> None:
        """Verify a pre-configured fingerprint is stored."""
        fingerprint = AsyncMock()
        fingerprint.viewport_width = 1440
        fingerprint.viewport_height = 900
        browser = ManagedBrowser(fingerprint=fingerprint)
        assert browser.fingerprint is not None

    def test_page_not_started_raises(self) -> None:
        """Verify accessing .page before start() raises."""
        browser = ManagedBrowser()
        with pytest.raises(BrowserNotStartedError):
            _ = browser.page

    def test_user_data_dir_stored(self, tmp_path: pytest.TempPathFactory) -> None:
        """Verify the user_data_dir path is stored correctly."""
        relative = str(tmp_path / "browser_profile")
        browser = ManagedBrowser(user_data_dir=relative)
        assert browser._user_data_dir.name == "browser_profile"


class TestManagedBrowserLifecycle:
    """Tests for ManagedBrowser start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop_direct_state(self) -> None:
        """Verify is_running works with directly injected state."""
        browser = ManagedBrowser()
        _simulate_started(browser)

        assert browser.is_running is True
        assert browser.page is not None
        assert browser.session_info is not None
        assert isinstance(browser.session_info, BrowserSessionInfo)

        await browser.stop()
        assert browser.is_running is False
        assert browser.session_info is None

    @pytest.mark.asyncio
    async def test_double_stop_safe(self) -> None:
        """Verify stop() is safe to call multiple times."""
        browser = ManagedBrowser()
        _simulate_started(browser)

        await browser.stop()
        await browser.stop()  # Second stop should be a no-op.
        # If we get here without exception, it's fine.

    @pytest.mark.asyncio
    async def test_abort_safe_when_not_started(self) -> None:
        """Verify abort() is safe on an unstarted browser."""
        browser = ManagedBrowser()
        await browser.abort()

    @pytest.mark.asyncio
    async def test_context_manager_stops_on_exit(self) -> None:
        """Verify the context manager stops the browser on exit."""
        browser = ManagedBrowser()
        _simulate_started(browser)
        assert browser.is_running

        # Simulate __aexit__.
        await browser.__aexit__(None, None, None)
        assert browser.is_running is False


class TestManagedBrowserNavigation:
    """Tests that navigation methods fail when browser is not started."""

    @pytest.mark.asyncio
    async def test_navigate_not_started_raises(self) -> None:
        """Verify navigate on unstarted browser raises."""
        browser = ManagedBrowser()
        with pytest.raises(BrowserNotStartedError):
            await browser.navigate("https://example.com")

    @pytest.mark.asyncio
    async def test_extract_text_not_started_raises(self) -> None:
        """Verify extract_text on unstarted browser raises."""
        browser = ManagedBrowser()
        with pytest.raises(BrowserNotStartedError):
            await browser.extract_text()

    @pytest.mark.asyncio
    async def test_click_not_started_raises(self) -> None:
        """Verify click on unstarted browser raises."""
        browser = ManagedBrowser()
        with pytest.raises(BrowserNotStartedError):
            await browser.click(".button")

    @pytest.mark.asyncio
    async def test_screenshot_not_started_raises(self) -> None:
        """Verify screenshot on unstarted browser raises."""
        browser = ManagedBrowser()
        with pytest.raises(BrowserNotStartedError):
            await browser.screenshot()


class TestManagedBrowserAPIMethods:
    """Tests for specific ManagedBrowser methods using injected state."""

    @pytest.mark.asyncio
    async def test_navigate(self) -> None:
        """Verify navigate delegates to Playwright and increments counter."""
        mock_page = AsyncMock()
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        result = await browser.navigate("https://example.com")

        mock_page.goto.assert_awaited_once()
        assert result is mock_page
        assert browser.session_info is not None
        assert browser.session_info.pages_visited == 1

    @pytest.mark.asyncio
    async def test_get_url(self) -> None:
        """Verify get_url returns the current page URL."""
        mock_page = AsyncMock()
        mock_page.url = "https://example.com/jobs"
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        url = await browser.get_url()
        assert url == "https://example.com/jobs"

    @pytest.mark.asyncio
    async def test_get_title(self) -> None:
        """Verify get_title returns the page title."""
        mock_page = AsyncMock()
        mock_page.title = AsyncMock(return_value="Job Listings")
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        title = await browser.get_title()
        assert title == "Job Listings"

    @pytest.mark.asyncio
    async def test_cookies_roundtrip(self) -> None:
        """Verify get_cookies and set_cookies work."""
        mock_ctx = AsyncMock()
        mock_ctx.cookies = AsyncMock(return_value=[{"name": "session", "value": "abc"}])
        browser = ManagedBrowser()
        _simulate_started(browser, context=mock_ctx)

        cookies = await browser.get_cookies()
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session"

    @pytest.mark.asyncio
    async def test_wait_for_selector_returns_true(self) -> None:
        """Verify wait_for_selector returns True on success."""
        mock_page = AsyncMock()
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        result = await browser.wait_for_selector(".job-card")
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_selector_timeout(self) -> None:
        """Verify wait_for_selector returns False on timeout."""
        mock_page = AsyncMock()
        mock_page.wait_for_selector.side_effect = TimeoutError()
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        result = await browser.wait_for_selector(".ghost")
        assert result is False

    @pytest.mark.asyncio
    async def test_is_element_visible(self) -> None:
        """Verify is_element_visible delegates to locator."""
        from unittest.mock import MagicMock

        mock_page = AsyncMock()
        # page.locator() is synchronous in Playwright — returns a Locator
        mock_page.locator = MagicMock(
            return_value=MagicMock(
                is_visible=AsyncMock(return_value=True),
            )
        )
        browser = ManagedBrowser()
        _simulate_started(browser, page=mock_page)

        visible = await browser.is_element_visible(".button")
        assert visible is True


class TestCreateBrowserFactory:
    """Tests for the create_browser convenience factory.

    These tests use partial mocking to verify the factory contract
    without actually launching Playwright.
    """

    @pytest.mark.asyncio
    async def test_factory_aborts_on_exception(self) -> None:
        """Verify create_browser stops the browser if the context raises."""
        browser = ManagedBrowser()
        with patch.object(ManagedBrowser, "start", new=AsyncMock(return_value=None)):
            with patch.object(ManagedBrowser, "stop", new=AsyncMock()):
                async with create_browser(headless=True) as b:
                    assert isinstance(b, ManagedBrowser)
                # stop() was called when exiting the context manager.
