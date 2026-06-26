"""LinkedIn platform extractor.

LinkedIn has severe behavioural rate-limiting and aggressively flags automated
interaction.  This extractor implements:

- Session-cookie restoration (preferred) with credential fallback.
- Extra jitter and human-like interaction patterns.
- LinkedIn Jobs search with "Easy Apply" detection.
- Parsing of job listing cards for title, company, location, and metadata.

.. warning::
    LinkedIn's bot detection is among the strongest of any platform.
    Always use a persistent user-data directory with a manually authenticated
    session for best results.  Credential-based login is fragile and may
    trigger phone verification.
"""

from __future__ import annotations as _annotations

import contextlib
import random
from typing import TYPE_CHECKING, Any

import structlog

from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor, RateLimitConfig

if TYPE_CHECKING:
    from freelance_lead_gen.config.settings import Settings

logger = structlog.get_logger(__name__)

# ── Selectors ──────────────────────────────────────────────────────────────────
# These reflect LinkedIn's current DOM (subject to change).

_LINKEDIN_EMAIL_SELECTOR: str = "input#session_key, input[name='session_key']"
_LINKEDIN_PASSWORD_SELECTOR: str = "input#session_password, input[name='session_password']"
_LINKEDIN_LOGIN_BUTTON: str = "button[type='submit'], button.btn__primary--large"
_LINKEDIN_JOB_CARD_SELECTOR: str = (
    "div.job-search-card, li.jobs-search-results__list-item, "
    "div[data-job-id], article.job-card-container, "
    "div.job-card-container"
)
_LINKEDIN_TITLE_SELECTOR: str = (
    "a.job-card-list__title, h3.base-search-card__title, "
    "a.job-card-container__link, a.job-card-container__title, "
    "span[data-anonymize='job-title'], a[data-anonymize='job-title']"
)
_LINKEDIN_COMPANY_SELECTOR: str = (
    "a.job-card-container__company-name, h4.base-search-card__subtitle, "
    "span[data-anonymize='company-name']"
)
_LINKEDIN_LOCATION_SELECTOR: str = (
    "span.job-card-container__metadata-item, span.job-search-card__location, "
    "li.job-card-container__metadata-item"
)
_LINKEDIN_DESCRIPTION_SELECTOR: str = (
    "div.job-card-container__description, p.job-search-card__description, "
    "span.job-card-container__description-text"
)
_LINKEDIN_POSTED_DATE_SELECTOR: str = (
    "time.job-card-container__listed-state, time.job-search-card__listdate, "
    "span.job-card-container__listed-state"
)
_LINKEDIN_EASY_APPLY_SELECTOR: str = "span:has-text('Easy Apply'), button:has-text('Easy Apply')"
_LINKEDIN_NEXT_BUTTON: str = "button[aria-label='Next'], button.jobs-search-pagination__next-button"
_LINKEDIN_SEARCH_URL_TEMPLATE: str = (
    "https://www.linkedin.com/jobs/search/?"
    "keywords={query}&"
    "location=&"
    "distance=100&"
    "f_E=2&"  # 2 = Contract, 3 = Freelance, 4 = Full-time
    "sortBy=DD"  # DD = Date posted
)
_LINKEDIN_LOGIN_URL: str = "https://www.linkedin.com/login"


# ── Extractor ──────────────────────────────────────────────────────────────────


class LinkedInExtractor(BasePlatformExtractor):
    """Extractor for LinkedIn Jobs / Consulting opportunities.

    Sessions are persisted via the browser's user-data directory.  For best
    results, perform a manual login once, then rely on cookie restoration for
    subsequent runs.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance.
    rate_limit : RateLimitConfig or None
        Extra-conservative defaults (8–18 s delays) to avoid LinkedIn's
        behavioural rate limiting.
    email : str or None
        Optional email for credential-based login.
    password : str or None
        Optional password for credential-based login.

    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        email: str | None = None,
        password: str | None = None,
        credentials: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit
            or RateLimitConfig(
                min_delay=8.0,
                max_delay=18.0,
                jitter_factor=0.5,
                requests_per_minute=4,
                max_pages_per_session=5,
                cooldown_after_session=120.0,
            ),
            credentials=credentials,
            settings=settings,
        )
        self._email = (
            email or (credentials or {}).get("email") or (credentials or {}).get("username")
        )
        self._password = password or (credentials or {}).get("password")

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "linkedin"

    @property
    def search_url_template(self) -> str:
        return _LINKEDIN_SEARCH_URL_TEMPLATE

    async def login(self) -> bool:
        """Authenticate on LinkedIn.

        Tries cookie-based session first.  If that fails, falls back to
        credential-based login.

        Returns
        -------
        bool
            *True* if authenticated.

        """
        # Check if session cookies already work.
        if await self._is_authenticated():
            logger.info("linkedin.already_authenticated")
            self._authenticated = True
            return True

        # Credential login as fallback.
        if not self._email or not self._password:
            logger.error("linkedin.login_no_credentials")
            return False

        logger.info("linkedin.login_starting")

        try:
            await self._browser.retry_navigation(_LINKEDIN_LOGIN_URL, retries=2, timeout_ms=60_000)
            await self._random_delay(2.0, 5.0)

            # CAPTCHA check.
            if await self._detect_captcha():
                logger.error("linkedin.captcha_before_login")
                return False

            # Type email with human-like delays.
            await self._type_like_human(_LINKEDIN_EMAIL_SELECTOR, self._email)
            await self._random_delay(1.0, 3.0)

            # Type password.
            await self._type_like_human(_LINKEDIN_PASSWORD_SELECTOR, self._password)
            await self._random_delay(2.0, 4.0)

            # Click login.
            await self._browser.click(_LINKEDIN_LOGIN_BUTTON)
            await self._random_delay(3.0, 8.0)

            # Check for security challenges.
            if await self._detect_security_challenge():
                logger.error("linkedin.security_challenge_after_login")
                return False

            if await self._detect_captcha():
                logger.error("linkedin.captcha_after_login")
                return False

            # Verify success.
            current_url = self._browser.page.url.lower()
            if "checkpoint" in current_url or "challenge" in current_url:
                logger.error("linkedin.account_checkpoint")
                return False
            if "/login" in current_url or "login-submit" in current_url:
                logger.error("linkedin.login_failed_still_on_login")
                return False

            logger.info("linkedin.login_successful")
            self._authenticated = True
            return True

        except Exception as exc:
            logger.exception("linkedin.login_exception", error=str(exc))
            return False

    async def search(self, query: str) -> None:
        """Navigate to LinkedIn Jobs search results for *query*.

        Parameters
        ----------
        query : str
            Search keywords.

        """
        url = self.search_url_template.format(query=query.replace(" ", "%20"))
        logger.info("linkedin.searching", query=query)

        try:
            await self._browser.retry_navigation(url, retries=2, timeout_ms=60_000)
            await self._random_delay(3.0, 6.0)

            # LinkedIn lazy-loads results — scroll gently.
            await self._slow_scroll()

            # Wait for job cards to render.
            await self._browser.wait_for_selector(
                _LINKEDIN_JOB_CARD_SELECTOR,
                timeout_ms=20_000,
            )

        except Exception as exc:
            logger.exception("linkedin.search_navigation_error", query=query, error=str(exc))
            raise

    async def parse_results(self) -> list[RawLead]:
        """Parse job listing cards from the current search results page.

        Returns
        -------
        list of RawLead

        """
        leads: list[RawLead] = []

        try:
            cards = await self._browser.page.query_selector_all(_LINKEDIN_JOB_CARD_SELECTOR)
        except Exception as exc:
            logger.warning("linkedin.parse_no_cards", error=str(exc))
            return []

        for card in cards:
            try:
                lead = await self._parse_card(card)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("linkedin.card_parse_error", error=str(exc))
                continue

        return leads

    async def next_page(self) -> bool:
        """Advance to the next page of LinkedIn job results.

        Returns
        -------
        bool
            *True* if the next page was loaded.

        """
        try:
            next_btn = await self._browser.page.query_selector(_LINKEDIN_NEXT_BUTTON)
            if not next_btn:
                return False

            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled is not None:
                return False

            await next_btn.scroll_into_view_if_needed()
            await self._random_delay(1.0, 2.5)

            await next_btn.click()
            await self._browser.wait_for_navigation(timeout_ms=30_000)
            await self._random_delay(4.0, 8.0)

            return True
        except Exception as exc:
            logger.debug("linkedin.next_page_error", error=str(exc))
            return False

    # ── Card parsing ────────────────────────────────────────────────────

    async def _parse_card(self, card: Any) -> RawLead | None:
        """Parse a single LinkedIn job card into a :class:`RawLead`."""
        title = await self._get_el_text(card, _LINKEDIN_TITLE_SELECTOR)
        if not title:
            return None

        url = await self._get_el_href(card, _LINKEDIN_TITLE_SELECTOR)
        company = await self._get_el_text(card, _LINKEDIN_COMPANY_SELECTOR)
        location = await self._get_el_text(card, _LINKEDIN_LOCATION_SELECTOR)
        description = await self._get_el_text(card, _LINKEDIN_DESCRIPTION_SELECTOR)
        posted_text = await self._get_el_text(card, _LINKEDIN_POSTED_DATE_SELECTOR)

        await self._detect_easy_apply(card)

        # Extract a LinkedIn job ID from the URL or data attribute.
        job_id = self._extract_linkedin_job_id(url or title, card)

        budget_min, budget_max = None, None
        # LinkedIn usually doesn't show budget in search results;
        # it requires opening the individual listing.

        return RawLead(
            platform="linkedin",
            platform_job_id=job_id,
            title=title.strip()[:500],
            company=company.strip() if company else None,
            description=(description or "").strip(),
            url=url,
            posted_date=posted_text.strip() if posted_text else None,
            budget_min=budget_min,
            budget_max=budget_max,
            location=location.strip() if location else None,
        )

    async def _detect_easy_apply(self, card: Any) -> bool:
        """Check if a job card has an "Easy Apply" label."""
        try:
            el = await card.query_selector(_LINKEDIN_EASY_APPLY_SELECTOR)
            return el is not None
        except Exception:
            return False

    # ── Anti-detection ──────────────────────────────────────────────────

    async def _slow_scroll(self) -> None:
        """Extra-slow scroll pattern for LinkedIn's behavioural monitoring."""
        scrolls = random.randint(2, 4)
        for _ in range(scrolls):
            with contextlib.suppress(Exception):
                await self._browser.scroll("down", amount=random.randint(200, 500))
            await self._random_delay(1.0, 3.0)

    # ── Detection helpers ───────────────────────────────────────────────

    async def _is_authenticated(self) -> bool:
        """Check if the session is authenticated by visiting the feed."""
        try:
            await self._browser.navigate(
                "https://www.linkedin.com/feed/",
                timeout_ms=20_000,
                wait_until="domcontentloaded",
            )
            await self._random_delay(1.0, 2.0)

            current = self._browser.page.url.lower()
            if "/login" in current or "checkpoint" in current:
                return False

            # Look for the nav bar (authenticated).
            return await self._browser.is_element_visible("header.global-nav, nav.global-nav__nav")
        except Exception:
            return False

    async def _detect_security_challenge(self) -> bool:
        """Check for LinkedIn security challenges (phone verification, etc.)."""
        try:
            page_text = await self._browser.page.content()
            text_lower = page_text.lower()

            challenge_indicators = [
                "let's do a quick security check",
                "verify your identity",
                "security verification",
                "phone verification",
                "enter the code we sent",
                "challenge",
            ]
            for indicator in challenge_indicators:
                if indicator in text_lower:
                    return True

            current = self._browser.page.url.lower()
            if "challenge" in current or "checkpoint" in current:
                return True
        except Exception:
            pass

        return False

    # ── Element helpers ─────────────────────────────────────────────────

    @staticmethod
    async def _get_el_text(card: Any, selector: str) -> str:
        """Get inner text from a child of *card*."""
        try:
            el = await card.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    @staticmethod
    async def _get_el_href(card: Any, selector: str) -> str | None:
        """Get ``href`` from a child anchor, making relative URLs absolute."""
        try:
            el = await card.query_selector(selector)
            if el:
                href = await el.get_attribute("href")
                if href and href.startswith("/"):
                    href = f"https://www.linkedin.com{href.split('?')[0]}"
                return href
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_linkedin_job_id(url: str, card: Any) -> str:
        """Extract the LinkedIn job ID from a URL or card data attribute."""
        import re

        # Data attribute on the card.
        try:
            job_id = card.get_attribute("data-job-id")
            if job_id:
                return job_id
        except Exception:
            pass

        # From URL: /jobs/view/12345678/
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)

        # From URL param: ?currentJobId=12345678
        match = re.search(r"[?&]currentJobId=(\d+)", url)
        if match:
            return match.group(1)

        # Fallback.
        import hashlib

        return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:12]
