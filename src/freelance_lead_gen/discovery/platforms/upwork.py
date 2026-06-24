"""Upwork platform extractor.

Upwork has strong anti-bot protection (Cloudflare Turnstile, behavioural
scoring, login challenges).  This extractor implements:

- Credential-based login with 2FA challenge detection.
- Session cookie persistence across runs (via the browser user-data dir).
- Search on the "Find Work" page with category filters.
- Parsing of job listing cards for title, description, budget, skills, etc.

.. warning::
    Upwork actively blocks headless browsers and datacentre IP ranges.
    For reliable extraction, use a residential proxy and a persistent
    user-data directory with a prior manual login session.
"""

from __future__ import annotations as _annotations

import random
from typing import Any

import structlog

from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor, RateLimitConfig

logger = structlog.get_logger(__name__)

# ── Selectors ──────────────────────────────────────────────────────────────────

_UPWORK_LOGIN_EMAIL_SELECTOR: str = "input[name='login[email]'], input#login_email_control"
"""CSS selector for the email input on the login page."""

_UPWORK_LOGIN_PASSWORD_SELECTOR: str = "input[name='login[password]'], input#login_password_control"
"""CSS selector for the password input."""

_UPWORK_LOGIN_SUBMIT_SELECTOR: str = "button[type='submit'], #login_control button"
"""CSS selector for the login submit button."""

_UPWORK_2FA_INPUT_SELECTOR: str = "input[name='otp'], input#totp_code, input[data-otp-input]"
"""CSS selector for the 2FA code input (if triggered)."""

_UPWORK_JOB_CARD_SELECTOR: str = (
    "article.up-card-section, article[data-test='JobTile'], "
    "div[data-test='job-tile'], section[data-test='job-tile-section']"
)
"""CSS selector for individual job listing cards on the search results page."""

_UPWORK_TITLE_SELECTOR: str = (
    "h2 a, h3 a, a[data-test='job-tile-title-link'], "
    "h2[data-test='job-tile-title'], h3[data-test='job-tile-title']"
)
"""Selector for the job title within a card."""

_UPWORK_DESCRIPTION_SELECTOR: str = (
    "p[data-test='job-description-text'], "
    "div[data-test='description'], span[data-test='job-description']"
)
"""Selector for the job description snippet."""

_UPWORK_BUDGET_SELECTOR: str = (
    "span[data-test='budget'], span[data-test='price'], "
    "strong[data-test='budget'], li[data-test='job-type']"
)
"""Selector for the budget / rate text."""

_UPWORK_POSTED_DATE_SELECTOR: str = (
    "span[data-test='job-pubished-date'], span[data-test='date'], "
    "small[data-test='job-date']"
)
"""Selector for the posting date."""

_UPWORK_SKILLS_SELECTOR: str = (
    "div[data-test='job-tile-skills'] span, "
    "div[data-test='skills'] span[data-test='skill']"
)
"""Selector for skill tags."""

_UPWORK_NEXT_PAGE_SELECTOR: str = (
    "a[data-test='pagination-next'], a[aria-label='Next'], "
    "button[data-test='pagination-next']"
)
"""Selector for the "next page" pagination button."""

# ── URLs ──────────────────────────────────────────────────────────────────────

_UPWORK_LOGIN_URL: str = "https://www.upwork.com/ab/account-security/login"
_UPWORK_FIND_WORK_URL: str = "https://www.upwork.com/nx/find-work/"
_UPWORK_SEARCH_URL_TEMPLATE: str = (
    "https://www.upwork.com/nx/search/jobs/?q={query}&sort=recency"
)


# ── Extractor ──────────────────────────────────────────────────────────────────


class UpworkExtractor(BasePlatformExtractor):
    """Extractor for Upwork freelance job listings.

    Handles login, 2FA detection, search with query filtering, and
    paginated result parsing.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance.
    rate_limit : RateLimitConfig or None
        Rate-limit parameters.  Upwork defaults to conservative values
        (5–12 s delays) to avoid triggering Cloudflare.
    email : str or None
        Optional email for login.  If omitted, attempts cookie-based auth.
    password : str or None
        Optional password for login.
    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit or RateLimitConfig(
                min_delay=5.0,
                max_delay=12.0,
                jitter_factor=0.4,
                requests_per_minute=6,
                max_pages_per_session=8,
                cooldown_after_session=90.0,
            ),
        )
        self._email = email
        self._password = password

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "upwork"

    @property
    def search_url_template(self) -> str:
        return _UPWORK_SEARCH_URL_TEMPLATE

    async def login(self) -> bool:
        """Authenticate on Upwork.

        The method first checks whether the current session is already valid
        (by visiting the Find Work page).  If not, it performs a credential-based
        login and handles 2FA challenges if they appear.

        Returns
        -------
        bool
            *True* if authentication was successful.
        """
        # Check if already authenticated.
        if await self._is_authenticated():
            logger.info("upwork.already_authenticated")
            self._authenticated = True
            return True

        if not self._email or not self._password:
            logger.error("upwork.login_no_credentials")
            return False

        logger.info("upwork.login_starting")

        try:
            await self._browser.retry_navigation(_UPWORK_LOGIN_URL, retries=2)
            await self._random_delay(2.0, 4.0)

            # Check for CAPTCHA before attempting login.
            if await self._detect_captcha():
                logger.error("upwork.captcha_before_login")
                return False

            # Enter email.
            await self._type_like_human(_UPWORK_LOGIN_EMAIL_SELECTOR, self._email)
            await self._random_delay(1.0, 3.0)

            # Click continue / next.
            await self._browser.click(_UPWORK_LOGIN_SUBMIT_SELECTOR, no_after_delay=False)
            await self._random_delay(1.5, 4.0)

            # Enter password.
            await self._type_like_human(_UPWORK_LOGIN_PASSWORD_SELECTOR, self._password)
            await self._random_delay(1.0, 2.0)

            # Submit login.
            await self._browser.click(_UPWORK_LOGIN_SUBMIT_SELECTOR)
            await self._random_delay(3.0, 6.0)

            # Check for 2FA.
            if await self._detect_2fa():
                logger.warning("upwork.2fa_required")
                # We cannot automate 2FA — mark as failed and let the
                # operator handle it via a persisted session.
                return False

            # Check for CAPTCHA after login attempt.
            if await self._detect_captcha():
                logger.error("upwork.captcha_after_login")
                return False

            # Verify success by checking redirect.
            current_url = self._browser.page.url
            if "/login" in current_url.lower():
                logger.error("upwork.login_failed_still_on_login")
                return False

            logger.info("upwork.login_successful")
            self._authenticated = True
            return True

        except Exception as exc:
            logger.error("upwork.login_exception", error=str(exc))
            return False

    async def search(self, query: str) -> None:
        """Navigate to search results for *query* on the Find Work page.

        Parameters
        ----------
        query : str
            Search term for filtering jobs.
        """
        url = _UPWORK_SEARCH_URL_TEMPLATE.format(query=query.replace(" ", "+"))

        if not query:
            url = _UPWORK_FIND_WORK_URL

        logger.info("upwork.searching", query=query or "all")

        try:
            await self._browser.retry_navigation(url, retries=2, timeout_ms=60_000)
            await self._random_delay(2.0, 5.0)

            # Trigger lazy loading with a gentle scroll.
            await self._random_scroll()

        except Exception as exc:
            logger.error("upwork.search_navigation_error", query=query, error=str(exc))
            # If navigation fails, we may still be on a valid page.
            raise

    async def parse_results(self) -> list[RawLead]:
        """Parse job listing cards from the current page.

        Returns
        -------
        list of RawLead
        """
        leads: list[RawLead] = []

        try:
            cards = await self._browser.page.query_selector_all(_UPWORK_JOB_CARD_SELECTOR)
        except Exception as exc:
            logger.warning("upwork.parse_no_cards", error=str(exc))
            return []

        if not cards:
            logger.warning("upwork.no_cards_found")
            return []

        for card in cards:
            try:
                lead = await self._parse_card(card)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("upwork.card_parse_error", error=str(exc))
                continue

        return leads

    async def next_page(self) -> bool:
        """Advance to the next search-results page.

        Returns
        -------
        bool
            *True* if the next page was loaded.
        """
        try:
            next_btn = await self._browser.page.query_selector(_UPWORK_NEXT_PAGE_SELECTOR)
            if not next_btn:
                return False

            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled is not None:
                return False

            # Scroll to pagination.
            await next_btn.scroll_into_view_if_needed()
            await self._random_delay(0.5, 1.5)

            await next_btn.click()
            await self._browser.wait_for_navigation(timeout_ms=30_000)
            await self._random_delay(2.0, 5.0)

            return True
        except Exception as exc:
            logger.debug("upwork.next_page_error", error=str(exc))
            return False

    # ── Card parsing ────────────────────────────────────────────────────

    async def _parse_card(self, card: Any) -> RawLead | None:  # noqa: ANN401
        """Parse a single job card element into a :class:`RawLead`."""
        title = await self._get_el_text(card, _UPWORK_TITLE_SELECTOR)
        if not title:
            return None

        url = await self._get_el_href(card, _UPWORK_TITLE_SELECTOR)

        description = await self._get_el_text(card, _UPWORK_DESCRIPTION_SELECTOR)
        budget_text = await self._get_el_text(card, _UPWORK_BUDGET_SELECTOR)
        posted_text = await self._get_el_text(card, _UPWORK_POSTED_DATE_SELECTOR)

        # Skills.
        skills = await self._parse_skills(card)

        # Budget parsing.
        budget_min, budget_max = self._parse_upwork_budget(budget_text) if budget_text else (None, None)

        # Extract job ID from URL.
        job_id = self._extract_upwork_job_id(url or title)

        return RawLead(
            platform="upwork",
            platform_job_id=job_id,
            title=title.strip()[:500],
            company=None,  # Upwork hides the client name until you open the job.
            description=(description or "").strip(),
            url=url,
            posted_date=posted_text.strip() if posted_text else None,
            budget_min=budget_min,
            budget_max=budget_max,
            skills=skills,
        )

    async def _parse_skills(self, card: Any) -> list[str]:  # noqa: ANN401
        """Extract skill labels from a job card."""
        try:
            skill_els = await card.query_selector_all(_UPWORK_SKILLS_SELECTOR)
            return [
                (await el.inner_text()).strip()
                for el in skill_els
            ]
        except Exception:
            return []

    @staticmethod
    def _parse_upwork_budget(text: str) -> tuple[float | None, float | None]:
        """Parse Upwork budget strings.

        Handles formats: ``$15-$30/hr``, ``$500``, ``Hourly: $20.00-$40.00``,
        ``Budget: $1,000``, ``Fixed-price``.
        """
        import re

        if not text:
            return None, None

        text_clean = text.replace(",", "")

        # Match dollar amounts.
        amounts = re.findall(r"\$(\d+(?:\.\d+)?)", text_clean)
        amounts = [float(a) for a in amounts]

        if not amounts:
            return None, None

        if len(amounts) >= 2:
            return amounts[0], amounts[1]

        # Single amount — treat as max if it looks like a total budget,
        # or as both if it's a rate.
        if "hourly" in text.lower() or "/hr" in text.lower():
            return amounts[0], amounts[0]

        return None, amounts[0]

    @staticmethod
    def _extract_upwork_job_id(text: str) -> str:
        """Extract the Upwork job ID from a URL or text.

        Upwork job URLs have the format: ``~017d0b3c7a1c2b3d4e``
        """
        import re

        match = re.search(r"(~[\w]+)", text)
        if match:
            return match.group(1)

        # Fallback: hash the URL.
        import hashlib

        return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]

    # ── Detection helpers ───────────────────────────────────────────────

    async def _is_authenticated(self) -> bool:
        """Check if the current session is authenticated on Upwork.

        Visits the Find Work page; if redirected to login, we're not
        authenticated.
        """
        try:
            await self._browser.navigate(
                _UPWORK_FIND_WORK_URL,
                timeout_ms=20_000,
                wait_until="domcontentloaded",
            )
            await self._random_delay(1.0, 2.0)

            current = self._browser.page.url.lower()
            if "/login" in current:
                return False

            # Also check for the user avatar / profile indicator.
            return await self._browser.is_element_visible(
                "img[data-test='user-avatar'], div[data-test='user-menu'], "
                "img[data-qa='user-avatar']"
            )
        except Exception:
            return False

    async def _detect_2fa(self) -> bool:
        """Check if the page is showing a 2FA / OTP input.

        Returns
        -------
        bool
            *True* if a 2FA challenge is visible.
        """
        try:
            return await self._browser.is_element_visible(_UPWORK_2FA_INPUT_SELECTOR)
        except Exception:
            return False

    # ── Element helpers ─────────────────────────────────────────────────

    @staticmethod
    async def _get_el_text(card: Any, selector: str) -> str:  # noqa: ANN401
        """Get inner text from a child of *card* (or empty string)."""
        try:
            el = await card.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    @staticmethod
    async def _get_el_href(card: Any, selector: str) -> str | None:  # noqa: ANN401
        """Get ``href`` from a child anchor."""
        try:
            el = await card.query_selector(selector)
            if el:
                href = await el.get_attribute("href")
                if href and href.startswith("/"):
                    href = f"https://www.upwork.com{href}"
                return href
        except Exception:
            pass
        return None
