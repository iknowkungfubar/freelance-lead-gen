"""Base platform extractor — abstract interface for all platform scrapers.

Every platform extractor (Upwork, LinkedIn, Freelancer, etc.) inherits from
:class:`BasePlatformExtractor`, which defines the common contract: login,
search, parse results, and pagination.  Platform-specific subclasses fill in
the selectors, URL templates, and rate-limiting configuration.
"""

from __future__ import annotations as _annotations

import abc
import asyncio
import contextlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from freelance_lead_gen.config.settings import Settings, get_settings

if TYPE_CHECKING:
    from freelance_lead_gen.discovery.browser import ManagedBrowser
    from freelance_lead_gen.discovery.extractor import RawLead

logger = structlog.get_logger(__name__)


# ── Rate-limit config dataclass ────────────────────────────────────────────────


@dataclass
class RateLimitConfig:
    """Rate-limiting parameters for a platform.

    Controls how aggressively the system interacts with the platform to
    avoid being blocked.
    """

    min_delay: float = 3.0
    """Minimum delay (seconds) between any two interactions."""

    max_delay: float = 10.0
    """Maximum delay (seconds) between interactions."""

    jitter_factor: float = 0.3
    """Random jitter multiplier applied to delays — adds ±factor×delay fuzz."""

    requests_per_minute: int = 10
    """Soft cap on requests per minute (used by the scheduler)."""

    max_pages_per_session: int = 10
    """Maximum pages to scrape in a single browser session."""

    cooldown_after_session: float = 60.0
    """Cooldown (seconds) after a full session before a new one starts."""


# ── Base platform extractor ────────────────────────────────────────────────────


class BasePlatformExtractor(abc.ABC):
    """Abstract base for platform-specific extractors.

    Subclasses must implement:

    * :meth:`login` — authenticate on the platform.
    * :meth:`search` — navigate to search / browse results for a query.
    * :meth:`parse_results` — extract :class:`RawLead` instances from the
      current page.
    * :meth:`next_page` — navigate to the next page of results (or return
      ``False``).

    Each subclass also defines its own :attr:`platform_name`, rate-limiting
    config, and search URL pattern.
    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        credentials: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._browser = browser
        self._rate_limit = rate_limit or RateLimitConfig()
        self._credentials = credentials or {}
        self._settings = settings or get_settings()

        # Internal state.
        self._authenticated: bool = False
        self._current_page: int = 0
        self._session_start: float | None = None

    # ── Abstract interface ──────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def platform_name(self) -> str:
        """Lowercase platform name (matching :class:`~freelance_lead_gen.models.platform.Platform`)."""
        ...

    @property
    @abc.abstractmethod
    def search_url_template(self) -> str:
        """URL template with a ``{query}`` placeholder for search terms."""
        ...

    @abc.abstractmethod
    async def login(self) -> bool:
        """Authenticate on the platform.

        Returns
        -------
        bool
            *True* if the login was successful.

        """
        ...

    @abc.abstractmethod
    async def search(self, query: str) -> None:
        """Navigate to search results for *query*."""
        ...

    @abc.abstractmethod
    async def parse_results(self) -> list[RawLead]:
        """Parse listing cards from the current page into :class:`RawLead` instances.

        Returns
        -------
        list of RawLead

        """
        ...

    @abc.abstractmethod
    async def next_page(self) -> bool:
        """Advance to the next page of results.

        Returns
        -------
        bool
            *True* if the next page was loaded, *False* if at the last page.

        """
        ...

    # ── Concrete helpers ────────────────────────────────────────────────

    async def extract_listings(self) -> list[dict[str, Any]]:
        """Run a full extraction: ensures login, then iterates paginated results.

        Returns
        -------
        list of dict
            Raw listing dictionaries.  Override :meth:`parse_results` to
            control the output format.

        .. note::
            This is a convenience that calls the abstract methods in order.
            Subclasses with more complex flows should override this directly.

        """
        return [
            {
                "platform": lead.platform,
                "platform_job_id": lead.platform_job_id,
                "title": lead.title,
                "company": lead.company,
                "description": lead.description,
                "url": lead.url,
                "posted_date": lead.posted_date,
                "budget_min": lead.budget_min,
                "budget_max": lead.budget_max,
                "currency": lead.currency,
                "skills": lead.skills,
                "location": lead.location,
            }
            for lead in (await self.extract_listings_raw())
        ]

    async def extract_listings_raw(self, query: str = "") -> list[RawLead]:
        """Run a full extraction, returning :class:`RawLead` objects.

        Calls :meth:`ensure_authenticated`, calls :meth:`search` with the
        given *query*, collects results from all pages up to the platform's
        page limit, and returns deduplicated leads.

        Parameters
        ----------
        query : str
            Search term to pass to :meth:`search`.  Defaults to ``""``
            (browse all listings).

        """
        logger.info(
            "platform.extract_started",
            platform=self.platform_name,
            query=query or "all",
        )

        if not await self.ensure_authenticated():
            logger.error("platform.auth_failed", platform=self.platform_name)
            return []

        await self._rate_limit_delay()
        await self.search(query)

        leads: list[RawLead] = []
        seen: set[str] = set()
        self._current_page = 0
        self._session_start = datetime.now(UTC).timestamp()

        while self._current_page < self._rate_limit.max_pages_per_session:
            self._current_page += 1

            await self._rate_limit_delay()

            # Anti-detection: random scroll before parsing.
            await self._random_scroll()

            page_leads = await self.parse_results()

            for lead in page_leads:
                key = lead.url or lead.platform_job_id
                if key and key not in seen:
                    seen.add(key)
                    leads.append(lead)

            logger.info(
                "platform.page_parsed",
                platform=self.platform_name,
                page=self._current_page,
                leads_on_page=len(page_leads),
                total_leads=len(leads),
            )

            if not await self.next_page():
                break

            # Anti-detection: random delay between page loads.
            await self._rate_limit_delay()

        logger.info(
            "platform.extract_finished",
            platform=self.platform_name,
            total_leads=len(leads),
            pages=self._current_page,
        )
        return leads

    async def ensure_authenticated(self) -> bool:
        """Check if already authenticated, and log in if not.

        Subclasses can override this to add session-cookie restoration.

        Returns
        -------
        bool
            *True* if authenticated (or auth not required).

        """
        if self._authenticated:
            return True

        try:
            self._authenticated = await self.login()
        except Exception as exc:
            logger.exception(
                "platform.login_exception",
                platform=self.platform_name,
                error=str(exc),
            )
            self._authenticated = False

        return self._authenticated

    async def refresh_session(self, force: bool = False) -> bool:
        """Check the current page for session expiry and reauthenticate if needed.

        Parameters
        ----------
        force : bool
            If *True*, always reauthenticate regardless of session state.

        Returns
        -------
        bool
            *True* if the session is valid after the check.

        """
        if force or self._session_expired():
            logger.info("platform.session_refresh", platform=self.platform_name)
            return await self.ensure_authenticated()
        return self._authenticated

    def _session_expired(self) -> bool:
        """Heuristic: check if enough time has passed that the session may have expired.

        Subclasses can override with page-specific checks.
        """
        if self._session_start is None:
            return True
        elapsed = datetime.now(UTC).timestamp() - self._session_start
        # Assume sessions are valid for ~30 minutes.
        return elapsed > 1800

    # ── Anti-detection helpers ──────────────────────────────────────────

    async def _rate_limit_delay(self) -> None:
        """Wait for the configured rate-limiting delay with random jitter."""
        delay = random.uniform(self._rate_limit.min_delay, self._rate_limit.max_delay)
        jitter = delay * self._rate_limit.jitter_factor * random.uniform(-1.0, 1.0)
        total = max(0.5, delay + jitter)
        await asyncio.sleep(total)

    async def _random_scroll(self) -> None:
        """Scrolling pattern that mimics human browsing behaviour."""
        scrolls = random.randint(2, 5)
        for _ in range(scrolls):
            amount = random.randint(200, 700)
            direction = random.choice(["down", "down", "down", "up"])
            with contextlib.suppress(Exception):
                await self._browser.scroll(direction, amount=amount)
            await asyncio.sleep(random.uniform(0.3, 1.5))

    async def _random_mouse_move(self) -> None:
        """Semi-random mouse movement to reduce bot scoring."""
        try:
            vp = self._browser.page.viewport_size
            if vp:
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await self._browser.page.mouse.move(x, y, steps=random.randint(4, 10))
        except Exception:
            pass

    # ─── Login helper ───────────────────────────────────────────────────

    async def _type_like_human(
        self,
        selector: str,
        text: str,
    ) -> None:
        """Type text into a field with human-like speed variation."""
        if not text:
            return

        await self._random_delay(0.5, 1.5)
        await self._browser.type_text(
            selector,
            text,
            delay_range=(0.04, 0.18),
            clear_first=True,
        )
        await self._random_delay(0.3, 1.0)

    async def _random_delay(self, min_s: float = 0.5, max_s: float = 3.0) -> None:
        """Sleep for a random interval."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    # ── CAPTCHA / redirect detection ────────────────────────────────────

    async def _detect_login_redirect(self, original_url: str) -> bool:
        """Detect if the browser was redirected to a login page.

        Parameters
        ----------
        original_url : str
            The URL that was originally requested.

        Returns
        -------
        bool
            *True* if redirected to a login / auth page.

        """
        from freelance_lead_gen.discovery.extractor import _LOGIN_REDIRECT_INDICATORS

        try:
            current_url = self._browser.page.url.lower()
            clean_original = original_url.split("?", maxsplit=1)[0].rstrip("/")
            clean_current = current_url.split("?")[0].rstrip("/")

            if clean_original == clean_current:
                return False

            for indicator in _LOGIN_REDIRECT_INDICATORS:
                if indicator in current_url:
                    logger.warning(
                        "platform.login_redirect",
                        platform=self.platform_name,
                        original=original_url,
                        current=current_url,
                    )
                    return True
        except Exception:
            pass

        return False

    async def _detect_captcha(self) -> bool:
        """Detect CAPTCHA challenges on the current page.

        Returns
        -------
        bool
            *True* if a CAPTCHA is present.

        """
        from freelance_lead_gen.discovery.extractor import _CAPTCHA_INDICATORS

        try:
            content = await self._browser.page.content()
            content_lower = content.lower()

            for indicator in _CAPTCHA_INDICATORS:
                if indicator in content_lower:
                    logger.warning(
                        "platform.captcha_detected",
                        platform=self.platform_name,
                        indicator=indicator,
                    )
                    return True

            current_url = self._browser.page.url.lower()
            for indicator in _CAPTCHA_INDICATORS:
                if indicator in current_url:
                    return True
        except Exception:
            pass

        return False
