"""Base extraction logic — raw leads, abstract extractor, and generic Playwright extractor.

Defines the :class:`RawLead` dataclass used throughout the pipeline and the
:class:`GenericPlaywrightExtractor` that drives per-platform extraction with
anti-detection measures.
"""

from __future__ import annotations as _annotations

import abc
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from freelance_lead_gen.discovery.browser import ManagedBrowser

logger = structlog.get_logger(__name__)

# ── CAPTCHA / login redirect indicators ────────────────────────────────────────

_CAPTCHA_INDICATORS: list[str] = [
    "captcha",
    "cf-challenge",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "challenge-platform",
    "are you a human",
    "verify you are human",
]

_LOGIN_REDIRECT_INDICATORS: list[str] = [
    "/login",
    "/sign-in",
    "/signin",
    "/auth",
    "login?redirect",
    "account/login",
    "log in",
]

# ── RawLead dataclass ──────────────────────────────────────────────────────────


@dataclass
class RawLead:
    """A raw, unprocessed lead extracted from a freelance platform.

    This is the output of the extraction layer — a lightly-normalised
    representation of a job listing.  It gets classified into a proper
    :class:`~freelance_lead_gen.models.opportunity.LeadOpportunity` by
    a later pipeline step.

    .. note::
        All dates are stored as ISO-format strings for simplicity at
        the extraction level.  Parsing into :class:`datetime` objects
        happens during classification.
    """

    platform: str
    """Source platform name (e.g. ``"upwork"``, ``"linkedin"``)."""

    platform_job_id: str
    """Platform-native identifier for this listing (used for deduplication)."""

    title: str
    """Job or project title."""

    company: str | None = None
    """Hiring company or client name, if visible."""

    description: str = ""
    """Full text of the listing description."""

    url: str | None = None
    """Direct URL to the listing."""

    posted_date: str | None = None
    """ISO-format date string (e.g. ``"2026-06-24"`` or ``"2026-06-24T10:30:00Z"``)."""

    budget_min: float | None = None
    """Minimum budget / hourly rate in USD."""

    budget_max: float | None = None
    """Maximum budget / hourly rate in USD."""

    currency: str = "USD"
    """ISO 4217 currency code."""

    skills: list[str] = field(default_factory=list)
    """List of skill keywords mentioned in the listing."""

    location: str | None = None
    """Location string — remote, city, country, or ``None``."""

    raw_html: str | None = None
    """Raw HTML snippet of the listing card (for debugging / re-parsing)."""

    extracted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    """ISO-format timestamp of extraction."""


# ── Base extractor ─────────────────────────────────────────────────────────────


class Extractor(abc.ABC):
    """Abstract base for all platform extractors.

    Subclasses implement :meth:`extract` which returns a list of
    :class:`RawLead` instances discovered by searching the platform.
    """

    @abc.abstractmethod
    async def extract(
        self,
        platform: str,
        search_query: str,
    ) -> list[RawLead]:
        """Execute extraction for the given *platform* and *search_query*.

        Parameters
        ----------
        platform : str
            Lowercase platform name.
        search_query : str
            Search term (e.g. ``"AI automation"``, ``"RAG pipeline"``).

        Returns
        -------
        list of RawLead
            All leads found during this extraction run.

        """
        ...


# ── Generic Playwright Extractor ──────────────────────────────────────────────


class GenericPlaywrightExtractor(Extractor):
    """Generic extractor that uses a :class:`ManagedBrowser` and per-platform
    CSS selectors to extract leads.

    This is the "heavy" extractor — it drives a real browser with
    anti-detection for platforms that block simple HTTP requests.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance to drive.
    search_url_template : str
        URL template with a ``{query}`` placeholder.
    card_selector : str
        CSS selector for listing cards on the search-results page.
    title_selector : str
        CSS selector (relative to the card) for the listing title.
    url_selector : str
        CSS selector for the link element (extracts ``href``).
    description_selector : str or None
        Selector for the description text.
    company_selector : str or None
        Selector for the company / client name.
    budget_selector : str or None
        Selector for budget or rate information.
    posted_date_selector : str or None
        Selector for the posting date.
    extra_selectors : dict
        Additional selectors keyed by field name.
    max_results : int
        Maximum number of listings to extract per run.
    paginate : bool
        If ``True``, click "next page" and continue extracting.
    next_page_selector : str or None
        CSS selector for the "next page" button / link.

    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        search_url_template: str,
        card_selector: str,
        title_selector: str,
        url_selector: str,
        description_selector: str | None = None,
        company_selector: str | None = None,
        budget_selector: str | None = None,
        posted_date_selector: str | None = None,
        extra_selectors: dict[str, str] | None = None,
        max_results: int = 25,
        paginate: bool = True,
        next_page_selector: str | None = None,
        platform_name: str = "custom",
    ) -> None:
        self._browser = browser
        self._search_url_template = search_url_template
        self._card_selector = card_selector
        self._title_selector = title_selector
        self._url_selector = url_selector
        self._description_selector = description_selector
        self._company_selector = company_selector
        self._budget_selector = budget_selector
        self._posted_date_selector = posted_date_selector
        self._extra_selectors = extra_selectors or {}
        self._max_results = max_results
        self._paginate = paginate
        self._next_page_selector = next_page_selector
        self._platform_name = platform_name

    # ── Extractor interface ─────────────────────────────────────────────

    async def extract(self, platform: str, search_query: str) -> list[RawLead]:
        """Navigate to the search URL and extract all visible listing cards.

        Parameters
        ----------
        platform : str
            Ignored (always uses the configured *platform_name*).
        search_query : str
            URL-encoded search term (encoding is handled automatically).

        Returns
        -------
        list of RawLead

        """
        url = self._search_url_template.replace("{query}", search_query.replace(" ", "+"))
        logger.info("extractor.starting", platform=self._platform_name, query=search_query, url=url)

        try:
            await self._browser.navigate(url, wait_until="networkidle", timeout_ms=60_000)
        except Exception as exc:
            logger.exception(
                "extractor.navigation_failed", platform=self._platform_name, url=url, error=str(exc)
            )
            return []

        # CAPTCHA check.
        if await self._detect_captcha():
            logger.warning("extractor.captcha_detected", platform=self._platform_name, url=url)
            return []

        # Login redirect check.
        if await self._detect_login_redirect(url):
            logger.warning("extractor.login_redirect_detected", platform=self._platform_name)
            return []

        # Wait for cards to appear.
        try:
            await self._browser.wait_for_selector(self._card_selector, timeout_ms=15_000)
        except Exception:
            logger.warning(
                "extractor.no_cards_found",
                platform=self._platform_name,
                selector=self._card_selector,
            )
            return []

        # Anti-detection: random scroll to trigger lazy loading.
        await self._human_scroll()

        leads: list[RawLead] = []
        seen_urls: set[str] = set()
        page_count = 0

        while page_count < 10 and len(leads) < self._max_results:
            page_count += 1
            page_leads = await self._extract_page_leads(seen_urls)
            leads.extend(page_leads)

            logger.info(
                "extractor.page_extracted",
                platform=self._platform_name,
                page=page_count,
                leads_on_page=len(page_leads),
                total=len(leads),
            )

            if self._paginate and len(leads) < self._max_results:
                has_next = await self._go_next_page()
                if not has_next:
                    break
            else:
                break

        logger.info(
            "extractor.finished",
            platform=self._platform_name,
            query=search_query,
            total_leads=len(leads),
        )
        return leads

    # ── Per-page extraction ─────────────────────────────────────────────

    async def _extract_page_leads(self, seen_urls: set[str]) -> list[RawLead]:
        """Extract all leads from the current page's listing cards."""
        leads: list[RawLead] = []

        try:
            cards = await self._browser.page.query_selector_all(self._card_selector)
        except Exception:
            return []

        for card in cards:
            try:
                lead = await self._extract_card(card)
                if lead is None:
                    continue

                # Dedup by URL within this extraction run.
                dedup_key = lead.url or lead.title
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)

                leads.append(lead)
            except Exception as exc:
                logger.debug("extractor.card_parse_error", error=str(exc))
                continue

        return leads

    async def _extract_card(self, card: Any) -> RawLead | None:
        """Extract a single :class:`RawLead` from a Playwright element handle."""
        title = await self._get_card_text(card, self._title_selector) or ""
        if not title.strip():
            return None

        url = await self._get_card_href(card, self._url_selector)
        description = (
            await self._get_card_text(card, self._description_selector)
            if self._description_selector
            else ""
        )
        company = (
            await self._get_card_text(card, self._company_selector)
            if self._company_selector
            else None
        )
        budget_text = (
            await self._get_card_text(card, self._budget_selector)
            if self._budget_selector
            else None
        )
        posted_text = (
            await self._get_card_text(card, self._posted_date_selector)
            if self._posted_date_selector
            else None
        )

        budget_min, budget_max = self._parse_budget(budget_text) if budget_text else (None, None)

        # Extra selectors.
        extra: dict[str, str] = {}
        for field_name, sel in self._extra_selectors.items():
            val = await self._get_card_text(card, sel)
            if val:
                extra[field_name] = val

        # Extract a platform-native job ID from the URL or a data attribute.
        platform_job_id = self._extract_job_id(url or "", card)

        return RawLead(
            platform=self._platform_name,
            platform_job_id=platform_job_id,
            title=title.strip()[:500],
            company=company.strip() if company else None,
            description=description.strip() if description else "",
            url=url,
            posted_date=posted_text.strip() if posted_text else None,
            budget_min=budget_min,
            budget_max=budget_max,
            extra=extra,  # type: ignore[arg-type]
        )

    # ── Element helpers ─────────────────────────────────────────────────

    async def _get_card_text(self, card: Any, selector: str) -> str:
        """Get text content from a child element of *card*."""
        try:
            el = await card.query_selector(selector)
            if el:
                return (await el.inner_text()).strip()
        except Exception:
            pass
        return ""

    async def _get_card_href(self, card: Any, selector: str) -> str | None:
        """Get ``href`` from a child link element."""
        try:
            el = await card.query_selector(selector)
            if el:
                return await el.get_attribute("href")
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_budget(text: str) -> tuple[float | None, float | None]:
        """Parse budget text like ``"$30-$50/hr"`` or ``"Budget: $500"`` into min/max."""
        import re

        if not text:
            return None, None

        # Match dollar amounts: $20-$50, $30-$60, $500, etc.
        amounts = re.findall(r"\$?(\d+(?:,\d{3})*(?:\.\d+)?)", text.replace(",", ""))
        amounts = [float(a) for a in amounts]

        if not amounts:
            return None, None

        if len(amounts) >= 2:
            return amounts[0], amounts[1]
        return amounts[0], None

    @staticmethod
    def _extract_job_id(url: str, card: Any) -> str:
        """Extract a platform-native job ID from the card or URL."""
        import hashlib

        # Try getting from a data attribute.
        try:
            job_id = card.get_attribute("data-job-id") or card.get_attribute("data-id")
            if job_id:
                return job_id
        except Exception:
            pass

        # Fall back to a hash of the URL for dedup purposes.
        if url:
            return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:12]

        return hashlib.md5(str(time.time()).encode(), usedforsecurity=False).hexdigest()[:12]

    # ── Navigation helpers ──────────────────────────────────────────────

    async def _go_next_page(self) -> bool:
        """Click the "next page" button if available.

        Returns
        -------
        bool
            *True* if navigation occurred, *False* if no next page.

        """
        if not self._next_page_selector:
            return False

        try:
            visible = await self._browser.is_element_visible(self._next_page_selector)
            if not visible:
                return False

            # Anti-detection: scroll to the pagination element.
            await self._browser.scroll_into_view(self._next_page_selector)
            await self._random_delay(0.5, 1.5)

            await self._browser.click(self._next_page_selector)
            await self._browser.wait_for_navigation(timeout_ms=30_000)
            await self._random_delay(1.0, 3.0)
            return True
        except Exception as exc:
            logger.debug("extractor.next_page_failed", error=str(exc))
            return False

    # ── Anti-detection helpers ──────────────────────────────────────────

    async def _human_scroll(self) -> None:
        """Simulate a human scrolling down the page to trigger lazy content."""
        scroll_count = random.randint(2, 5)
        for _ in range(scroll_count):
            await self._browser.scroll("down", amount=random.randint(300, 800))
            await self._random_delay(0.5, 2.0)

    async def _random_mouse_movement(self) -> None:
        """Move the mouse to a random position on the page."""
        try:
            vp = self._browser.page.viewport_size
            if vp:
                x = random.randint(50, vp["width"] - 50)
                y = random.randint(50, vp["height"] - 50)
                await self._browser.page.mouse.move(x, y, steps=random.randint(5, 12))
        except Exception:
            pass

    async def _random_delay(self, min_s: float = 0.3, max_s: float = 3.0) -> None:
        """Sleep for a random interval."""
        import asyncio

        await asyncio.sleep(random.uniform(min_s, max_s))

    # ── Error detection ─────────────────────────────────────────────────

    async def _detect_captcha(self) -> bool:
        """Check the current page for CAPTCHA challenges.

        Returns
        -------
        bool
            *True* if a CAPTCHA is detected.

        """
        try:
            content = await self._browser.page.content()
            content_lower = content.lower()

            for indicator in _CAPTCHA_INDICATORS:
                if indicator in content_lower:
                    logger.warning("extractor.captcha_indicator_found", indicator=indicator)
                    return True

            # Check page URL for challenge patterns.
            current_url = self._browser.page.url.lower()
            for indicator in _CAPTCHA_INDICATORS:
                if indicator in current_url:
                    logger.warning("extractor.captcha_in_url", indicator=indicator)
                    return True
        except Exception:
            pass

        return False

    async def _detect_login_redirect(self, original_url: str) -> bool:
        """Check if the browser was redirected to a login page.

        Returns
        -------
        bool
            *True* if a login redirect is detected.

        """
        try:
            current_url = self._browser.page.url.lower()

            # Strip query params for comparison.
            clean_original = original_url.split("?", maxsplit=1)[0].rstrip("/")
            clean_current = current_url.split("?")[0].rstrip("/")

            if clean_original != clean_current:
                for indicator in _LOGIN_REDIRECT_INDICATORS:
                    if indicator in current_url:
                        logger.warning(
                            "extractor.login_redirect",
                            original=original_url,
                            current=current_url,
                        )
                        return True
        except Exception:
            pass

        return False
