"""Freelancer.com platform extractor.

Freelancer.com has moderate anti-bot protection.  This extractor handles:

- Credential-based login with session persistence.
- Browsing projects by category / search term.
- Parsing both regular project listings and contest listings.
- Budget and skill extraction from project cards.

.. note::
    Freelancer.com's DOM changes frequently.  The selectors below target
    the current class names — they may need periodic updates.
"""

from __future__ import annotations as _annotations

import random
from typing import Any

import structlog

from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor, RateLimitConfig

logger = structlog.get_logger(__name__)

# ── Selectors ──────────────────────────────────────────────────────────────────

_LOGIN_EMAIL_SELECTOR: str = "input#email, input[name='email'], input[name='login']"
_LOGIN_PASSWORD_SELECTOR: str = "input#password, input[name='password']"
_LOGIN_SUBMIT_SELECTOR: str = "button[type='submit'], button#login_button"
_PROJECT_CARD_SELECTOR: str = (
    "div.JobSearchCard-item, div.ProjectCard-item, "
    "div[class*='project-card'], div[class*='job-card'], "
    "div[class*='project-list-item']"
)
_PROJECT_TITLE_SELECTOR: str = (
    "a.JobSearchCard-primary-heading-link, "
    "a.ProjectCard-project-title-link, "
    "a[class*='project-title'], h2 a"
)
_PROJECT_DESCRIPTION_SELECTOR: str = (
    "p.JobSearchCard-primary-description, "
    "div.ProjectCard-description, "
    "div[class*='project-description'], p[class*='description']"
)
_PROJECT_BUDGET_SELECTOR: str = (
    "span.JobSearchCard-primary-price, "
    "div.ProjectCard-budget, "
    "strong[class*='budget'], span[class*='price']"
)
_PROJECT_SKILLS_SELECTOR: str = (
    "div.JobSearchCard-primary-tags a, "
    "div[class*='project-tags'] a, "
    "span[class*='skill'], a[class*='tag']"
)
_PROJECT_POSTED_SELECTOR: str = (
    "span.JobSearchCard-primary-timestamp, "
    "span[class*='time-ago'], span[class*='posted']"
)
_PROJECT_LOCATION_SELECTOR: str = (
    "span.JobSearchCard-primary-location, "
    "span[class*='location'], span[class*='country']"
)
_PROJECT_NEXT_PAGE_SELECTOR: str = (
    "a[rel='next'], a.pagination-next, "
    "a[class*='next'], button[class*='next']"
)
_CONTEST_CARD_SELECTOR: str = "div.ContestCard-item, div[class*='contest-card']"

_FREELANCER_LOGIN_URL: str = "https://www.freelancer.com/login"
_FREELANCER_SEARCH_URL_TEMPLATE: str = (
    "https://www.freelancer.com/search/projects/?keyword={query}&sort=date"
)
_FREELANCER_CONTEST_URL_TEMPLATE: str = (
    "https://www.freelancer.com/contest/search/?keyword={query}&sort=date"
)


# ── Extractor ──────────────────────────────────────────────────────────────────


class FreelancerExtractor(BasePlatformExtractor):
    """Extractor for Freelancer.com project and contest listings.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance.
    rate_limit : RateLimitConfig or None
        Defaults to moderate delays (4–10 s).
    email : str or None
        Optional email for login.
    password : str or None
        Optional password for login.
    include_contests : bool
        If *True*, also search contests (default ``False``).
    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        email: str | None = None,
        password: str | None = None,
        include_contests: bool = False,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit or RateLimitConfig(
                min_delay=4.0,
                max_delay=10.0,
                jitter_factor=0.35,
                requests_per_minute=8,
                max_pages_per_session=10,
                cooldown_after_session=60.0,
            ),
        )
        self._email = email
        self._password = password
        self._include_contests = include_contests

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "freelancer"

    @property
    def search_url_template(self) -> str:
        return _FREELANCER_SEARCH_URL_TEMPLATE

    async def login(self) -> bool:
        """Authenticate on Freelancer.com.

        Returns
        -------
        bool
            *True* if login succeeded.
        """
        if await self._is_authenticated():
            logger.info("freelancer.already_authenticated")
            self._authenticated = True
            return True

        if not self._email or not self._password:
            logger.error("freelancer.login_no_credentials")
            return False

        logger.info("freelancer.login_starting")

        try:
            await self._browser.retry_navigation(_FREELANCER_LOGIN_URL, retries=2)
            await self._random_delay(2.0, 4.0)

            if await self._detect_captcha():
                logger.error("freelancer.captcha_before_login")
                return False

            await self._type_like_human(_LOGIN_EMAIL_SELECTOR, self._email)
            await self._random_delay(1.0, 2.5)

            await self._type_like_human(_LOGIN_PASSWORD_SELECTOR, self._password)
            await self._random_delay(1.0, 2.5)

            await self._browser.click(_LOGIN_SUBMIT_SELECTOR)
            await self._random_delay(3.0, 6.0)

            if await self._detect_captcha():
                logger.error("freelancer.captcha_after_login")
                return False

            current_url = self._browser.page.url.lower()
            if "/login" in current_url:
                logger.error("freelancer.login_failed_still_on_login")
                return False

            logger.info("freelancer.login_successful")
            self._authenticated = True
            return True

        except Exception as exc:
            logger.error("freelancer.login_exception", error=str(exc))
            return False

    async def search(self, query: str) -> None:
        """Navigate to search results for *query*.

        Parameters
        ----------
        query : str
            Search term for filtering projects.
        """
        url = _FREELANCER_SEARCH_URL_TEMPLATE.format(query=query.replace(" ", "+"))
        logger.info("freelancer.searching", query=query)

        try:
            await self._browser.retry_navigation(url, retries=2, timeout_ms=60_000)
            await self._random_delay(2.0, 5.0)
            await self._random_scroll()
        except Exception as exc:
            logger.error("freelancer.search_navigation_error", query=query, error=str(exc))
            raise

    async def parse_results(self) -> list[RawLead]:
        """Parse project listing cards from the current page.

        Returns
        -------
        list of RawLead
        """
        leads = await self._parse_cards(_PROJECT_CARD_SELECTOR, self._parse_project_card)

        # Optionally include contest listings.
        if self._include_contests:
            contest_leads = await self._parse_cards(
                _CONTEST_CARD_SELECTOR, self._parse_contest_card
            )
            leads.extend(contest_leads)

        return leads

    async def next_page(self) -> bool:
        """Advance to the next page of search results.

        Returns
        -------
        bool
            *True* if the next page was loaded.
        """
        try:
            next_btn = await self._browser.page.query_selector(_PROJECT_NEXT_PAGE_SELECTOR)
            if not next_btn:
                return False

            is_disabled = await next_btn.get_attribute("disabled")
            if is_disabled is not None:
                return False

            await next_btn.scroll_into_view_if_needed()
            await self._random_delay(0.5, 1.5)

            await next_btn.click()
            await self._browser.wait_for_navigation(timeout_ms=30_000)
            await self._random_delay(2.0, 5.0)

            return True
        except Exception as exc:
            logger.debug("freelancer.next_page_error", error=str(exc))
            return False

    # ── Card parsing ────────────────────────────────────────────────────

    async def _parse_cards(
        self,
        card_selector: str,
        parser: Any,  # noqa: ANN401 — callable
    ) -> list[RawLead]:
        """Generic card parsing: query all *card_selector* elements and run *parser* on each."""
        leads: list[RawLead] = []

        try:
            cards = await self._browser.page.query_selector_all(card_selector)
        except Exception as exc:
            logger.warning("freelancer.parse_no_cards", selector=card_selector, error=str(exc))
            return []

        for card in cards:
            try:
                lead = await parser(card)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("freelancer.card_parse_error", error=str(exc))
                continue

        return leads

    async def _parse_project_card(self, card: Any) -> RawLead | None:  # noqa: ANN401
        """Parse a Freelancer project card."""
        title = await self._get_el_text(card, _PROJECT_TITLE_SELECTOR)
        if not title:
            return None

        url = await self._get_el_href(card, _PROJECT_TITLE_SELECTOR)
        description = await self._get_el_text(card, _PROJECT_DESCRIPTION_SELECTOR)
        budget_text = await self._get_el_text(card, _PROJECT_BUDGET_SELECTOR)
        posted_text = await self._get_el_text(card, _PROJECT_POSTED_SELECTOR)
        location = await self._get_el_text(card, _PROJECT_LOCATION_SELECTOR)

        skills = await self._parse_skills(card)
        budget_min, budget_max = self._parse_freelancer_budget(budget_text)

        job_id = self._extract_freelancer_job_id(url or title)

        return RawLead(
            platform="freelancer",
            platform_job_id=job_id,
            title=title.strip()[:500],
            company=None,
            description=(description or "").strip(),
            url=url,
            posted_date=posted_text.strip() if posted_text else None,
            budget_min=budget_min,
            budget_max=budget_max,
            skills=skills,
            location=location.strip() if location else None,
        )

    async def _parse_contest_card(self, card: Any) -> RawLead | None:  # noqa: ANN401
        """Parse a Freelancer contest listing."""
        title = await self._get_el_text(card, _PROJECT_TITLE_SELECTOR)
        if not title:
            return None

        url = await self._get_el_href(card, _PROJECT_TITLE_SELECTOR)
        description = await self._get_el_text(card, _PROJECT_DESCRIPTION_SELECTOR)
        budget_text = await self._get_el_text(card, _PROJECT_BUDGET_SELECTOR)

        budget_min, budget_max = self._parse_freelancer_budget(budget_text)
        job_id = self._extract_freelancer_job_id(url or title)

        return RawLead(
            platform="freelancer",
            platform_job_id=job_id,
            title=f"[Contest] {title.strip()[:490]}",
            company=None,
            description=(description or "").strip(),
            url=url,
            budget_min=budget_min,
            budget_max=budget_max,
        )

    async def _parse_skills(self, card: Any) -> list[str]:  # noqa: ANN401
        """Extract skill tags from a card."""
        skills: list[str] = []
        try:
            els = await card.query_selector_all(_PROJECT_SKILLS_SELECTOR)
            for el in els:
                text = (await el.inner_text()).strip()
                if text:
                    skills.append(text)
        except Exception:
            pass
        return skills

    @staticmethod
    def _parse_freelancer_budget(text: str) -> tuple[float | None, float | None]:
        """Parse Freelancer budget text.

        Formats: ``$10-$50``, ``$500``, ``A$30-A$100``, ``EUR 50-200``.
        """
        import re

        if not text:
            return None, None

        text_clean = text.replace(",", "")

        # Match amounts with optional currency prefix.
        amounts = re.findall(r"(?:[\$€£A\$]?\s*)(\d+(?:\.\d+)?)", text_clean)
        amounts = [float(a) for a in amounts if a]

        if not amounts:
            return None, None

        if len(amounts) >= 2:
            return amounts[0], amounts[1]
        return amounts[0], None

    @staticmethod
    def _extract_freelancer_job_id(text: str) -> str:
        """Extract a numeric Freelancer project ID from the text or URL."""
        import re

        match = re.search(r"/projects/(\d+)", text)
        if match:
            return match.group(1)

        match = re.search(r"project[_-]id[=:](\d+)", text)
        if match:
            return match.group(1)

        # Fallback.
        import hashlib
        return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]

    # ── Authentication check ────────────────────────────────────────────

    async def _is_authenticated(self) -> bool:
        """Check if the session is authenticated on Freelancer.com."""
        try:
            await self._browser.navigate(
                "https://www.freelancer.com/dashboard",
                timeout_ms=20_000,
                wait_until="domcontentloaded",
            )
            await self._random_delay(1.0, 2.0)

            current = self._browser.page.url.lower()
            if "/login" in current:
                return False

            # Look for user avatar / dashboard indicator.
            return await self._browser.is_element_visible(
                "img[class*='avatar'], div[class*='user-menu'], "
                "a[href*='dashboard']"
            )
        except Exception:
            return False

    # ── Element helpers ─────────────────────────────────────────────────

    @staticmethod
    async def _get_el_text(card: Any, selector: str) -> str:  # noqa: ANN401
        """Get inner text from a child element."""
        try:
            el = await card.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    @staticmethod
    async def _get_el_href(card: Any, selector: str) -> str | None:  # noqa: ANN401
        """Get ``href`` from a child anchor, making relative URLs absolute."""
        try:
            el = await card.query_selector(selector)
            if el:
                href = await el.get_attribute("href")
                if href and href.startswith("/"):
                    href = f"https://www.freelancer.com{href}"
                return href
        except Exception:
            pass
        return None
