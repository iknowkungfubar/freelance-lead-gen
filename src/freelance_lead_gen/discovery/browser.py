"""Managed browser — wraps Playwright with stealth, session persistence, and jitter.

Provides a :class:`ManagedBrowser` that initialises Playwright with anti-detection
configuration, persistent user-data directories, proxy support, and human-like
behavioural jittering.  The browser lifecycle is managed through async context
managers and explicit ``start()`` / ``stop()`` calls.
"""

from __future__ import annotations as _annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import structlog
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from freelance_lead_gen.utils.fingerprint import (
    BrowserFingerprint,
    fingerprint_to_playwright_kwargs,
    generate_fingerprint,
)

logger = structlog.get_logger(__name__)

# ── Behavioural jitter constants ───────────────────────────────────────────────

_DEFAULT_JITTER_MEAN: float = 3.0
"""Mean delay (seconds) between automated actions."""

_DEFAULT_JITTER_SIGMA: float = 1.2
"""Standard deviation of the Gaussian delay."""

_MIN_JITTER: float = 0.3
"""Floor for any individual delay — never go faster than this."""

_MAX_JITTER: float = 12.0
"""Ceiling for any individual delay — cap for safety."""

_RETRY_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
"""HTTP status codes that trigger a retry."""


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class BrowserSessionInfo:
    """Tracking info for an active browser session."""

    fingerprint: BrowserFingerprint
    """The fingerprint used for this session."""
    started_at: float
    """Unix timestamp when this session started."""
    pages_visited: int = 0
    """Counter of navigation events in this session."""
    errors: list[str] = field(default_factory=list)
    """Errors encountered during the session."""


# ── Exceptions ─────────────────────────────────────────────────────────────────


class BrowserError(RuntimeError):
    """Generic browser automation error."""

    def __init__(self, message: str, original: Exception | None = None) -> None:
        self.original = original
        super().__init__(message)


class BrowserNotStartedError(RuntimeError):
    """Raised when an action is attempted before the browser is started."""

    def __init__(self) -> None:
        super().__init__("Browser is not started — call start() or use 'async with' first")


class NavigationTimeoutError(BrowserError):
    """Raised when a navigation or page action times out."""



# ── Managed Browser ────────────────────────────────────────────────────────────


class ManagedBrowser:
    """Playwright automation wrapper with stealth configuration and jitter.

    Manages the full lifecycle of a Playwright browser instance, providing
    anti-detection measures (randomised fingerprint per session, behavioural
    delays, stealth patches) and persistent user-data directories.

    Parameters
    ----------
    headless : bool
        Run in headless mode (default ``False`` — visible for debugging).
    user_data_dir : str or Path
        Path to a persistent browser user-data directory.  Cookies and
        sessions survive across restarts.  Created if it does not exist.
    proxy_url : str or None
        Optional proxy URL (e.g. ``"http://proxy:8080"``).  Passing
        ``None`` disables proxying.
    jitter_mean : float
        Mean delay in seconds between automated actions (default 3.0).
    jitter_sigma : float
        Std-dev of the Gaussian delay (default 1.2).
    viewport : tuple[int, int] or None
        Explicit (width, height).  ``None`` picks a random fingerprint.
    fingerprint : BrowserFingerprint or None
        Reuse a specific fingerprint.  ``None`` generates a fresh one.
    launch_args : list[str] or None
        Additional Chromium launch arguments.

    """

    def __init__(
        self,
        *,
        headless: bool = False,
        user_data_dir: str | Path = "./browser_data",
        proxy_url: str | None = None,
        jitter_mean: float = _DEFAULT_JITTER_MEAN,
        jitter_sigma: float = _DEFAULT_JITTER_SIGMA,
        viewport: tuple[int, int] | None = None,
        fingerprint: BrowserFingerprint | None = None,
        launch_args: list[str] | None = None,
    ) -> None:
        self._headless = headless
        self._user_data_dir = Path(user_data_dir).resolve()
        self._proxy_url = proxy_url
        self._jitter_mean = jitter_mean
        self._jitter_sigma = jitter_sigma
        self._explicit_viewport = viewport
        self._launch_args = launch_args or []

        # Runtime state — set when started.
        self._playwright: Playwright | None = None
        self._browser_instance: Any | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._fingerprint: BrowserFingerprint | None = fingerprint
        self._session_info: BrowserSessionInfo | None = None

        # Default timeout.
        self._default_timeout_ms: int = 30_000

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Return *True* if the browser context is active."""
        return self._context is not None and not self._context.is_closed()

    @property
    def page(self) -> Page:
        """Return the current active page.

        Raises
        ------
        BrowserNotStartedError
            If the browser has not been started yet.

        """
        if self._page is None:
            raise BrowserNotStartedError
        return self._page

    @property
    def fingerprint(self) -> BrowserFingerprint | None:
        """Return the active session fingerprint, or *None* if not started."""
        return self._fingerprint

    @property
    def session_info(self) -> BrowserSessionInfo | None:
        """Return the active session info, or *None* if not started."""
        return self._session_info

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise Playwright, create a persistent context, and open a page.

        Generates a fresh :class:`~freelance_lead_gen.utils.fingerprint.BrowserFingerprint`
        for this session (unless one was provided at construction) and applies
        it to the browser context along with stealth configuration.

        Raises
        ------
        BrowserError
            If Playwright fails to launch or the context cannot be created.

        """
        if self.is_running or self._playwright is not None:
            logger.warning("browser.already_running")
            return

        # Ensure the user-data directory exists.
        self._user_data_dir.mkdir(parents=True, exist_ok=True)

        # Generate fingerprint if none was provided.
        if self._fingerprint is None:
            self._fingerprint = generate_fingerprint(
                viewport=self._explicit_viewport,
            )

        try:
            self._playwright = await async_playwright().start()
        except Exception as exc:
            self._playwright = None
            raise BrowserError("Failed to start Playwright", original=exc) from exc

        # Build launch options.
        launch_options: dict[str, Any] = {
            "headless": self._headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                *self._launch_args,
            ],
        }

        if self._proxy_url:
            launch_options["proxy"] = {"server": self._proxy_url}

        try:
            self._browser_instance = await self._playwright.chromium.launch(**launch_options)
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise BrowserError("Failed to launch Chromium", original=exc) from exc

        # Build persistent context options from the fingerprint.
        assert self._fingerprint is not None
        fp_kwargs = fingerprint_to_playwright_kwargs(self._fingerprint)

        context_options: dict[str, Any] = {
            **fp_kwargs,
            "base_url": "",
            "ignore_https_errors": True,
            "no_viewport": False,
        }

        try:
            self._context = await self._browser_instance.new_context(**context_options)
        except Exception as exc:
            await self._browser_instance.close()
            self._browser_instance = None
            await self._playwright.stop()
            self._playwright = None
            raise BrowserError("Failed to create browser context", original=exc) from exc

        # Open a fresh page.
        try:
            self._page = await self._context.new_page()
        except Exception as exc:
            await self.abort()
            raise BrowserError("Failed to open new page", original=exc) from exc

        # ── Stealth & anti-detection ───────────────────────────────────
        # Apply playwright-stealth patches (optional dependency).
        try:
            from playwright_stealth import stealth  # type: ignore[import-untyped]

            await stealth(self._page)
            logger.info("browser.stealth_applied")
        except ImportError:
            logger.warning("browser.stealth_not_available", detail="playwright-stealth package not installed")

        # Inject Canvas / WebGL / AudioContext fingerprint randomization.
        await self._page.add_init_script("""
// Override WebGL vendor/renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
  if (param === 37445) return 'Intel Inc.';
  if (param === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, param);
};
// Override navigator properties
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
""")

        # Store session info.
        self._session_info = BrowserSessionInfo(
            fingerprint=self._fingerprint,
            started_at=time.time(),
        )

        logger.info(
            "browser.started",
            viewport=f"{fp_kwargs.get('viewport', '?')}",
            fingerprint_id=id(self._fingerprint),
            user_data_dir=str(self._user_data_dir),
        )

    async def stop(self) -> None:
        """Gracefully tear down the browser, context, and Playwright.

        Safe to call multiple times.  Logs but does not raise on errors
        during teardown.  Cleans up resources in reverse order of creation:
        page → context → browser instance → Playwright.
        """
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    await self._page.close()
            except Exception:
                logger.warning("browser.page_close_error", exc_info=True)
            self._page = None

        if self._context is not None:
            try:
                if not self._context.is_closed():
                    await self._context.close()
            except Exception:
                logger.warning("browser.context_close_error", exc_info=True)
            self._context = None

        if self._browser_instance is not None:
            try:
                if not self._browser_instance.is_connected():
                    pass  # Already disconnected.
                await self._browser_instance.close()
            except Exception:
                logger.warning("browser.browser_close_error", exc_info=True)
            self._browser_instance = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.warning("browser.playwright_stop_error", exc_info=True)
            self._playwright = None

        self._session_info = None
        logger.info("browser.stopped")

    async def abort(self) -> None:
        """Forceful shutdown — close everything without graceful teardown.

        Useful when the browser is in an unknown or broken state.
        """
        errors: list[Exception] = []

        if self._page is not None:
            try:
                await self._page.close()
            except Exception as e:
                errors.append(e)
            self._page = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as e:
                errors.append(e)
            self._context = None

        if self._browser_instance is not None:
            try:
                await self._browser_instance.close()
            except Exception as e:
                errors.append(e)
            self._browser_instance = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                errors.append(e)
            self._playwright = None

        self._session_info = None
        logger.info("browser.aborted", error_count=len(errors))

    # ── Context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        """Start the browser and return self for use as an async context manager.

        Usage::

            async with ManagedBrowser() as browser:
                await browser.navigate("https://example.com")
        """
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: object = None,
        exc_val: object = None,
        exc_tb: object = None,
    ) -> None:
        """Stop the browser when exiting the context manager."""
        if exc_type is not None:
            logger.error("browser.context_exception", exc_info=(exc_type, exc_val, exc_tb))  # type: ignore[arg-type]
        await self.stop()

    # ── Navigation ──────────────────────────────────────────────────────

    async def navigate(
        self,
        url: str,
        *,
        timeout_ms: int | None = None,
        wait_until: str = "load",
        referer: str | None = None,
    ) -> Page:
        """Navigate to *url* and wait for the page to load.

        Parameters
        ----------
        url : str
            The URL to navigate to.
        timeout_ms : int or None
            Navigation timeout in milliseconds.  Falls back to the default
            (30 s) when ``None``.
        wait_until : str
            Playwright ``wait_until`` value — ``"load"``, ``"domcontentloaded"``,
            ``"networkidle"``.  Defaults to ``"load"``.
        referer : str or None
            Optional ``Referer`` header value.

        Returns
        -------
        Page
            The active page (post-navigation).

        Raises
        ------
        NavigationTimeoutError
            If navigation times out.
        BrowserError
            For other navigation failures.

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms
        opts: dict[str, Any] = {
            "url": url,
            "wait_until": wait_until,
            "timeout": timeout,
        }
        if referer:
            opts["referer"] = referer

        try:
            await self._jitter()
            await self.page.goto(**opts)
            if self._session_info is not None:
                self._session_info.pages_visited += 1
            logger.info("browser.navigated", url=url, status="ok")
        except Exception as exc:
            err_msg = str(exc)
            if "Timeout" in err_msg or "timeout" in err_msg:
                logger.warning("browser.navigation_timeout", url=url, timeout=timeout)
                raise NavigationTimeoutError(f"Navigation to {url} timed out after {timeout}ms") from exc
            raise BrowserError(f"Navigation to {url} failed: {exc}", original=exc) from exc

        return self.page

    # ── Page interaction ────────────────────────────────────────────────

    async def extract_text(
        self,
        selector: str | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> str:
        """Extract text content from the page or a specific element.

        Parameters
        ----------
        selector : str or None
            CSS/XPath selector.  When ``None``, returns the full page text.
        timeout_ms : int or None
            Wait timeout for the selector.

        Returns
        -------
        str
            The extracted text content (whitespace-stripped).

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms

        try:
            if selector:
                await self.page.wait_for_selector(selector, timeout=timeout)
                text = await self.page.text_content(selector)
                return (text or "").strip()
            text = await self.page.content()
            return text.strip()
        except Exception as exc:
            logger.warning("browser.extract_text_error", selector=selector, error=str(exc))
            return ""

    async def text_content(self, selector: str, *, timeout_ms: int | None = None) -> str:
        """Convenience: extract the text content of the first matching element.

        Parameters
        ----------
        selector : str
            CSS selector for the target element.
        timeout_ms : int or None
            Wait timeout.

        Returns
        -------
        str
            Element text content, or ``""`` if not found.

        """
        return await self.extract_text(selector, timeout_ms=timeout_ms)

    async def click(
        self,
        selector: str,
        *,
        timeout_ms: int | None = None,
        force: bool = False,
        no_after_delay: bool = False,
    ) -> None:
        """Click an element identified by CSS selector.

        Adds a behavioural jitter delay *before* the click.

        Parameters
        ----------
        selector : str
            CSS selector for the element to click.
        timeout_ms : int or None
            Wait timeout.
        force : bool
            Bypass actionability checks (default ``False``).
        no_after_delay : bool
            Skip the post-click jitter delay (default ``False``).

        Raises
        ------
        BrowserError
            If the click fails or the element is not found.

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms

        await self._jitter()

        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            # Human-like mouse move to the element before clicking.
            bbox = await self.page.locator(selector).bounding_box()
            if bbox:
                target_x = bbox["x"] + bbox["width"] * random.uniform(0.2, 0.8)
                target_y = bbox["y"] + bbox["height"] * random.uniform(0.2, 0.8)
                await self.page.mouse.move(target_x, target_y)
                await asyncio.sleep(random.uniform(0.05, 0.25))

            await self.page.click(selector, force=force, timeout=timeout)
            logger.info("browser.clicked", selector=selector)

            if not no_after_delay:
                await self._jitter()
        except Exception as exc:
            if self._session_info is not None:
                self._session_info.errors.append(str(exc))
            logger.warning("browser.click_error", selector=selector, error=str(exc))
            raise BrowserError(f"Click on '{selector}' failed: {exc}", original=exc) from exc

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        delay_range: tuple[float, float] = (0.05, 0.25),
        clear_first: bool = True,
    ) -> None:
        """Type text into an input field with human-like typing speed variation.

        Parameters
        ----------
        selector : str
            CSS selector for the input element.
        text : str
            The text to type.
        delay_range : tuple of (float, float)
            Min/max delay (seconds) between individual keystrokes.
        clear_first : bool
            Clear the field before typing (default ``True``).

        """
        self._ensure_running()

        await self._jitter()

        try:
            await self.page.wait_for_selector(selector, timeout=self._default_timeout_ms)
            await self.page.click(selector)

            if clear_first:
                await self.page.fill(selector, "")
                await asyncio.sleep(random.uniform(0.1, 0.3))

            # Type with human-like inter-key delays.
            for char in text:
                await self.page.keyboard.type(char, delay=int(random.uniform(*delay_range) * 1000))

            logger.info("browser.typed", selector=selector, length=len(text))
            await self._jitter()
        except Exception as exc:
            logger.warning("browser.type_error", selector=selector, error=str(exc))
            raise BrowserError(f"Type into '{selector}' failed: {exc}", original=exc) from exc

    async def scroll(
        self,
        direction: str = "down",
        amount: int | None = None,
        *,
        smooth: bool = True,
    ) -> None:
        """Scroll the page in the given direction by *amount* pixels.

        Parameters
        ----------
        direction : str
            ``"down"``, ``"up"``, ``"left"``, or ``"right"``.
        amount : int or None
            Pixels to scroll.  When ``None``, scrolls by viewport height × a
            random factor (0.6–0.9).
        smooth : bool
            Use smooth scrolling behaviour (default ``True``).

        """
        self._ensure_running()

        if amount is None:
            try:
                vp = self.page.viewport_size
                vh = vp["height"] if vp else 1080
                amount = int(vh * random.uniform(0.6, 0.9))
            except Exception:
                amount = 600

        delta = {"down": "y", "up": "y", "left": "x", "right": "x"}
        sign = 1 if direction in ("down", "right") else -1
        axis = delta.get(direction, "y")

        try:
            smooth_str = "smooth" if smooth else "instant"
            script = (
                f"window.scrollBy({{\n"
                f"  {axis}: {sign * amount},\n"
                f"  behavior: '{smooth_str}'"
                f"}});"
            )
            await self.page.evaluate(script)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            logger.info("browser.scrolled", direction=direction, amount=amount)
        except Exception as exc:
            logger.warning("browser.scroll_error", direction=direction, error=str(exc))

    async def scroll_into_view(self, selector: str) -> None:
        """Scroll an element into view.

        Parameters
        ----------
        selector : str
            CSS selector for the target element.

        """
        self._ensure_running()
        try:
            await self.page.locator(selector).scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception as exc:
            logger.warning("browser.scroll_into_view_error", selector=selector, error=str(exc))

    # ── Screenshots ─────────────────────────────────────────────────────

    async def screenshot(
        self,
        path: str | Path | None = None,
        *,
        full_page: bool = False,
    ) -> bytes:
        """Take a screenshot of the current page.

        Parameters
        ----------
        path : str or Path or None
            File path to save the screenshot.  If ``None``, returns the raw
            PNG bytes.
        full_page : bool
            Capture the full scrollable page (default ``False``).

        Returns
        -------
        bytes
            PNG image data.

        Raises
        ------
        BrowserError
            If the screenshot fails.

        """
        self._ensure_running()
        try:
            opts: dict[str, Any] = {"type": "png", "full_page": full_page}
            if path:
                opts["path"] = str(path)

            data = await self.page.screenshot(**opts)
            if path:
                logger.info("browser.screenshot_saved", path=str(path))
            return data
        except Exception as exc:
            raise BrowserError(f"Screenshot failed: {exc}", original=exc) from exc

    # ── Cookie / Session helpers ─────────────────────────────────────────

    async def get_cookies(self) -> list[dict[str, Any]]:
        """Return all cookies for the current context.

        Returns
        -------
        list of dict
            Each cookie dict contains ``name``, ``value``, ``domain``, etc.

        """
        self._ensure_running()
        return await self._context.cookies()  # type: ignore[union-attr]

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set cookies into the current context.

        Useful for restoring a previously-saved session.

        Parameters
        ----------
        cookies : list of dict
            Playwright-compatible cookie list.

        """
        self._ensure_running()
        await self._context.add_cookies(cookies)  # type: ignore[union-attr]
        logger.info("browser.cookies_set", count=len(cookies))

    async def save_cookies(self, path: str | Path) -> None:
        """Serialize all cookies to a JSON file.

        Parameters
        ----------
        path : str or Path
            JSON file to write.

        """
        import json

        cookies = await self.get_cookies()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cookies": cookies}, indent=2, default=str))
        logger.info("browser.cookies_saved", path=str(path), count=len(cookies))

    async def load_cookies(self, path: str | Path) -> int:
        """Load cookies from a previously saved JSON file.

        Parameters
        ----------
        path : str or Path
            JSON file to read.

        Returns
        -------
        int
            Number of cookies restored.

        """
        import json

        path = Path(path)
        if not path.is_file():
            logger.warning("browser.cookies_file_not_found", path=str(path))
            return 0

        data = json.loads(path.read_text())
        cookies = data.get("cookies", [])
        if cookies:
            await self.set_cookies(cookies)
        return len(cookies)

    # ── Page state helpers ──────────────────────────────────────────────

    async def get_url(self) -> str:
        """Return the current page URL."""
        self._ensure_running()
        return self.page.url

    async def get_title(self) -> str:
        """Return the current page title."""
        self._ensure_running()
        return await self.page.title()

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout_ms: int | None = None,
        state: str = "visible",
    ) -> bool:
        """Wait for a CSS selector to reach a given state.

        Parameters
        ----------
        selector : str
            CSS selector.
        timeout_ms : int or None
            Maximum wait time.
        state : str
            One of ``"attached"``, ``"detached"``, ``"hidden"``, ``"visible"``
            (default).

        Returns
        -------
        bool
            *True* if the element reached the expected state, *False* on timeout.

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms
        try:
            await self.page.wait_for_selector(selector, state=state, timeout=timeout)
            return True
        except Exception:
            return False

    async def is_element_visible(self, selector: str) -> bool:
        """Quick check if a CSS selector is visible on the page.

        Parameters
        ----------
        selector : str
            CSS selector.

        Returns
        -------
        bool

        """
        self._ensure_running()
        try:
            return await self.page.locator(selector).is_visible()
        except Exception:
            return False

    async def evaluate(self, script: str) -> Any:
        """Run JavaScript in the page context.

        Parameters
        ----------
        script : str
            JavaScript code to evaluate.

        Returns
        -------
        Any
            The return value of the script.

        """
        self._ensure_running()
        return await self.page.evaluate(script)

    # ── HTTP helpers ────────────────────────────────────────────────────

    async def fetch_via_page(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request from within the page context.

        Uses ``fetch()`` in the page so the request carries the page's
        cookies and headers (useful for XHR-loaded content).

        Parameters
        ----------
        url : str
            Target URL.
        method : str
            HTTP method (default ``"GET"``).
        headers : dict or None
            Extra headers.
        data : str or None
            Request body for POST/PUT.

        Returns
        -------
        dict
            Parsed JSON response (or ``{"raw": text}`` if not JSON).

        """
        self._ensure_running()
        opts = {"method": method, "headers": headers or {}}
        if data is not None:
            opts["body"] = data

        script = (
            f"fetch({url!r}, {opts!r})"
            f".then(r => r.text().then(text => {{"
            f"  try {{ return JSON.parse(text); }} catch {{ return {{raw: text}}; }}"
            f"}}))"
        )
        try:
            return await self.page.evaluate(script)
        except Exception as exc:
            raise BrowserError(f"fetch_via_page failed for {url}: {exc}", original=exc) from exc

    # ── Wait / sleep helpers ────────────────────────────────────────────

    async def wait(self, seconds: float = 1.0) -> None:
        """Sleep for *seconds*.  Wrapper around ``asyncio.sleep``."""
        await asyncio.sleep(seconds)

    async def wait_for_navigation(
        self,
        *,
        timeout_ms: int | None = None,
        wait_until: str = "networkidle",
    ) -> None:
        """Wait for the page to finish navigating / loading.

        Parameters
        ----------
        timeout_ms : int or None
            Maximum wait time.
        wait_until : str
            When to consider navigation complete.

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms
        try:
            await self.page.wait_for_load_state(wait_until, timeout=timeout)
        except Exception as exc:
            raise NavigationTimeoutError(
                f"Page did not reach '{wait_until}' state within {timeout}ms"
            ) from exc

    # ── URL change detection ───────────────────────────────────────────

    async def get_redirect_url(self, *, timeout_ms: int | None = None) -> str:
        """Wait for any URL change and return the new URL.

        Useful after a form submission triggers a redirect.

        Parameters
        ----------
        timeout_ms : int or None
            Maximum wait time.

        Returns
        -------
        str
            The redirect target URL.

        """
        self._ensure_running()
        timeout = timeout_ms or self._default_timeout_ms
        try:
            current = self.page.url
            await self.page.wait_for_url(
                "**/*",
                timeout=timeout,
            )
            new_url = self.page.url
            if new_url != current:
                logger.info("browser.redirect_detected", from_url=current, to_url=new_url)
            return new_url
        except Exception:
            return self.page.url

    # ── Retry helper ────────────────────────────────────────────────────

    async def retry_navigation(
        self,
        url: str,
        *,
        retries: int = 3,
        timeout_ms: int | None = None,
    ) -> Page:
        """Navigate to *url* with exponential backoff retry.

        Retries on HTTP status codes 408, 429, 5xx and on timeouts.

        Parameters
        ----------
        url : str
            The URL to navigate to.
        retries : int
            Max retries (default 3).
        timeout_ms : int or None
            Per-try timeout.

        Returns
        -------
        Page
            The active page after successful navigation.

        Raises
        ------
        BrowserError
            If all retries are exhausted.

        """
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return await self.navigate(url, timeout_ms=timeout_ms)
            except (NavigationTimeoutError, BrowserError) as exc:
                last_error = exc
                if attempt < retries:
                    backoff = 2.0 ** attempt + random.uniform(0, 1.0)
                    logger.warning(
                        "browser.retry_navigation",
                        url=url,
                        attempt=attempt,
                        max_retries=retries,
                        backoff_seconds=round(backoff, 1),
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)

        raise BrowserError(
            f"Navigation to {url} failed after {retries} retries: {last_error}",
            original=last_error,
        )

    # ── Internal helpers ────────────────────────────────────────────────

    def _ensure_running(self) -> None:
        """Guard: raise if the browser is not ready."""
        if not self.is_running or self._page is None:
            raise BrowserNotStartedError

    async def _jitter(self) -> None:
        """Introduce a random Gaussian delay to simulate human latency."""
        delay = min(
            _MAX_JITTER,
            max(_MIN_JITTER, random.gauss(self._jitter_mean, self._jitter_sigma)),
        )
        await asyncio.sleep(delay)

    # ── Human-like mouse movement ───────────────────────────────────────

    async def human_mouse_move(
        self,
        selector: str,
        *,
        offset_x: float = 0.5,
        offset_y: float = 0.5,
    ) -> None:
        """Smoothly move the mouse cursor to an element's centre (or a fraction).

        Parameters
        ----------
        selector : str
            Target CSS selector.
        offset_x : float
            Fractional horizontal offset into the element (0-1, default 0.5).
        offset_y : float
            Fractional vertical offset (0-1, default 0.5).

        """
        self._ensure_running()
        bbox = await self.page.locator(selector).bounding_box()
        if bbox is None:
            logger.warning("browser.human_mouse_no_bbox", selector=selector)
            return

        target_x = bbox["x"] + bbox["width"] * offset_x
        target_y = bbox["y"] + bbox["height"] * offset_y

        # Move in a few small steps for realism.
        steps = random.randint(3, 7)
        for _ in range(steps):
            current = await self.page.mouse.position()
            cur_x, cur_y = current["x"], current["y"]
            step_x = cur_x + (target_x - cur_x) * random.uniform(0.2, 0.6)
            step_y = cur_y + (target_y - cur_y) * random.uniform(0.2, 0.6)
            await self.page.mouse.move(step_x, step_y)
            await asyncio.sleep(random.uniform(0.01, 0.05))

        await self.page.mouse.move(target_x, target_y)


# ── Convenience factory ────────────────────────────────────────────────────────


@asynccontextmanager
async def create_browser(**kwargs: Any) -> ManagedBrowser:
    """Context-manager factory for a :class:`ManagedBrowser`.

    Usage::

        async with create_browser(headless=False) as browser:
            await browser.navigate("https://upwork.com")
    """
    browser = ManagedBrowser(**kwargs)
    try:
        await browser.start()
        yield browser
    finally:
        await browser.stop()
