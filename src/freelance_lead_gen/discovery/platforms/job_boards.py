"""Job board extractors — Remote OK, Y Combinator "Work at a Startup", and a
generic aggregator for custom boards.

These platforms are simpler than Upwork/LinkedIn/Freelancer: they generally
do not require authentication and may expose REST APIs or straightforward
HTML that can be parsed via direct HTTP calls or minimal browser interaction.
"""

from __future__ import annotations as _annotations

import asyncio
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
import structlog

from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor, RateLimitConfig

if TYPE_CHECKING:
    from freelance_lead_gen.config.settings import Settings

logger = structlog.get_logger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_id(text: str) -> str:
    """Produce a short hash-based ID from *text*."""
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════════════════════
# Remote OK
# ═══════════════════════════════════════════════════════════════════════════════


class RemoteOKExtractor(BasePlatformExtractor):
    """Extractor for Remote OK (remoteok.com).

    Remote OK has a public JSON API at ``/api`` that returns all listings.
    No authentication is required.  The extractor fetches the API directly
    with ``httpx`` rather than using the browser, falling back to browser
    extraction if the API is unavailable.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance (used for fallback only).
    rate_limit : RateLimitConfig or None
        Conservative defaults (2–5 s).

    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        credentials: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit
            or RateLimitConfig(
                min_delay=2.0,
                max_delay=5.0,
                jitter_factor=0.3,
                requests_per_minute=15,
                max_pages_per_session=5,
                cooldown_after_session=30.0,
            ),
            credentials=credentials,
            settings=settings,
        )
        self._http_client: httpx.AsyncClient | None = None

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "remote_ok"

    @property
    def search_url_template(self) -> str:
        return "https://remoteok.com/remote-{query}-jobs"

    async def login(self) -> bool:
        """Remote OK does not require authentication."""
        return True

    async def search(self, query: str) -> None:
        """Remote OK API fetch is done in :meth:`parse_results` directly."""
        # We override extract_listings_raw instead.

    async def parse_results(self) -> list[RawLead]:
        """Parse results — called by the base :meth:`extract_listings_raw`."""
        # This path is used only if the base class orchestrates the calls.
        # We override extract_listings_raw entirely to bypass the browser.
        return []

    async def next_page(self) -> bool:
        """Remote OK returns all results on one page."""
        return False

    # ── Override extraction to use HTTP instead of browser ─────────────

    async def extract_listings_raw(self) -> list[RawLead]:
        """Fetch listings from Remote OK's JSON API.

        Returns
        -------
        list of RawLead

        """
        logger.info("remote_ok.fetching_api")

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )

        try:
            response = await self._http_client.get("https://remoteok.com/api")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("remote_ok.api_failed", error=str(exc))
            # Fallback: use browser extraction.
            return await self._browser_fallback()

        # The API returns an array; index 0 is a metadata object.
        if isinstance(data, list) and len(data) > 1:
            listings = data[1:]
        else:
            listings = data if isinstance(data, list) else []

        leads: list[RawLead] = []
        for job in listings:
            try:
                lead = self._api_job_to_lead(job)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("remote_ok.parse_error", error=str(exc))
                continue

        logger.info("remote_ok.fetched", count=len(leads))
        return leads

    def _api_job_to_lead(self, job: dict[str, Any]) -> RawLead | None:
        """Convert a Remote OK API job object to a :class:`RawLead`."""
        title = job.get("position", "") or job.get("title", "")
        if not title:
            return None

        company = job.get("company")
        description = job.get("description", "")
        url = job.get("url") or job.get("apply_url")

        # Budget — Remote OK shows salary ranges in the `salary` field.
        salary = job.get("salary", "")
        budget_min, budget_max = self._parse_salary(salary)

        tags = job.get("tags", []) or job.get("categories", [])
        skills = [t.strip() for t in tags if t]

        return RawLead(
            platform="remote_ok",
            platform_job_id=_make_id(url or title),
            title=title.strip()[:500],
            company=company,
            description=description.strip() if description else "",
            url=url,
            posted_date=job.get("date"),
            budget_min=budget_min,
            budget_max=budget_max,
            skills=skills,
            location=job.get("location") or "Remote",
        )

    @staticmethod
    def _parse_salary(salary: str) -> tuple[float | None, float | None]:
        """Parse salary strings like ``$80k-$120k`` or ``$100k+``."""
        if not salary:
            return None, None

        amounts = re.findall(r"\$?(\d+)(?:k|K|,000)?", salary)
        amounts = [
            float(a) * 1000 if "k" in salary.lower() or len(a) <= 6 else float(a) for a in amounts
        ]

        if not amounts:
            return None, None

        if len(amounts) >= 2:
            return amounts[0], amounts[1]
        return amounts[0], None

    async def _browser_fallback(self) -> list[RawLead]:
        """Fallback: use the browser to scrape Remote OK."""
        logger.info("remote_ok.browser_fallback")
        try:
            await self._browser.navigate("https://remoteok.com/", wait_until="networkidle")
            await asyncio.sleep(2.0)

            cards = await self._browser.page.query_selector_all("tr.job, td.company_and_position")
            leads: list[RawLead] = []

            for card in cards:
                try:
                    title_el = await card.query_selector("h2[itemprop='title'], a[itemprop='url']")
                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    href = await title_el.get_attribute("href")
                    url = urljoin("https://remoteok.com/", href) if href else None

                    company_el = await card.query_selector("span[itemprop='name'], span.company")
                    company = (await company_el.inner_text()).strip() if company_el else None

                    leads.append(
                        RawLead(
                            platform="remote_ok",
                            platform_job_id=_make_id(url or title),
                            title=title[:500],
                            company=company,
                            url=url,
                        )
                    )
                except Exception:
                    continue

            return leads
        except Exception as exc:
            logger.exception("remote_ok.browser_fallback_failed", error=str(exc))
            return []

    async def __del__(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()


# ═══════════════════════════════════════════════════════════════════════════════
# Y Combinator — Work at a Startup
# ═══════════════════════════════════════════════════════════════════════════════


class YCWorkExtractor(BasePlatformExtractor):
    """Extractor for Y Combinator's "Work at a Startup" job board.

    YC Work has a public JSON API endpoint that returns all listings.
    No authentication is required for browsing.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance (used for fallback only).
    rate_limit : RateLimitConfig or None

    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        credentials: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit
            or RateLimitConfig(
                min_delay=2.0,
                max_delay=4.0,
                jitter_factor=0.3,
                requests_per_minute=20,
                max_pages_per_session=5,
                cooldown_after_session=30.0,
            ),
            credentials=credentials,
            settings=settings,
        )
        self._http_client: httpx.AsyncClient | None = None

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "yc_work"

    @property
    def search_url_template(self) -> str:
        return "https://www.workatastartup.com/jobs?search={query}"

    async def login(self) -> bool:
        """YC Work does not require authentication to browse."""
        return True

    async def search(self, query: str) -> None:
        """Overridden — we fetch via API directly."""

    async def parse_results(self) -> list[RawLead]:
        """Overridden — we fetch via API directly."""
        return []

    async def next_page(self) -> bool:
        return False

    # ── Override extraction to use HTTP ─────────────────────────────────

    async def extract_listings_raw(self) -> list[RawLead]:
        """Fetch listings from YC Work's public API.

        Returns
        -------
        list of RawLead

        """
        logger.info("yc_work.fetching_api")

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )

        try:
            response = await self._http_client.get(
                "https://www.workatastartup.com/jobs",
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()

            # YC Work injects JSON into a script tag or returns a JSON array.
            # Try direct JSON first; if it's HTML, parse the embedded data.
            try:
                data = response.json()
            except (json.JSONDecodeError, Exception):
                data = await self._parse_embedded_json(response.text)

        except Exception as exc:
            logger.warning("yc_work.api_failed", error=str(exc))
            return await self._browser_fallback()

        listings = data if isinstance(data, list) else data.get("jobs", data.get("data", []))

        leads: list[RawLead] = []
        for job in listings:
            try:
                lead = self._api_job_to_lead(job)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("yc_work.parse_error", error=str(exc))
                continue

        logger.info("yc_work.fetched", count=len(leads))
        return leads

    def _api_job_to_lead(self, job: dict[str, Any]) -> RawLead | None:
        """Convert a YC Work API job object to a :class:`RawLead`."""
        title = job.get("title", "") or job.get("position", "")
        if not title:
            return None

        company = (
            job.get("company", {}).get("name", "")
            if isinstance(job.get("company"), dict)
            else job.get("company_name", "")
        )
        description = job.get("description", "") or job.get("descriptionHtml", "") or ""
        url = job.get("url") or f"https://www.workatastartup.com/jobs/{job.get('id', '')}"

        salary = job.get("salaryHigh") or job.get("salaryMax") or job.get("salary")
        salary_min = job.get("salaryLow") or job.get("salaryMin")

        skills = job.get("skills", []) if isinstance(job.get("skills"), list) else []
        location = job.get("location") or job.get("officeLocation")

        return RawLead(
            platform="yc_work",
            platform_job_id=str(job.get("id", _make_id(url))),
            title=title.strip()[:500],
            company=company or None,
            description=description.strip() if description else "",
            url=url,
            posted_date=job.get("createdAt") or job.get("postedDate"),
            budget_min=float(salary_min) if salary_min else None,
            budget_max=float(salary) if salary else None,
            skills=[s.strip() for s in skills] if skills else [],
            location=location or None,
        )

    @staticmethod
    async def _parse_embedded_json(html: str) -> list[dict[str, Any]]:
        """Parse JSON embedded in the page HTML (e.g. in a ``<script type="application/json">`` tag)."""
        import json

        # Try __NEXT_DATA__ or similar.
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"\s+type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try window.__INITIAL_STATE__.
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.*?});", html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        return []

    async def _browser_fallback(self) -> list[RawLead]:
        """Fallback: use the browser to scrape YC Work."""
        logger.info("yc_work.browser_fallback")
        try:
            await self._browser.navigate("https://www.workatastartup.com/jobs")
            await asyncio.sleep(3.0)

            leads: list[RawLead] = []
            cards = await self._browser.page.query_selector_all(
                "a[class*='job'], div[class*='job-card'], div[class*='JobCard']"
            )

            for card in cards:
                try:
                    title_el = await card.query_selector("h2, h3, a[class*='title']")
                    if not title_el:
                        continue

                    title = (await title_el.inner_text()).strip()
                    href = await title_el.get_attribute("href")
                    url = urljoin("https://www.workatastartup.com/", href) if href else None

                    company_el = await card.query_selector(
                        "div[class*='company'], span[class*='company']"
                    )
                    company = (await company_el.inner_text()).strip() if company_el else None

                    leads.append(
                        RawLead(
                            platform="yc_work",
                            platform_job_id=_make_id(url or title),
                            title=title[:500],
                            company=company,
                            url=url,
                        )
                    )
                except Exception:
                    continue

            return leads
        except Exception as exc:
            logger.exception("yc_work.browser_fallback_failed", error=str(exc))
            return []

    async def __del__(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregator (Generic/Custom job board)
# ═══════════════════════════════════════════════════════════════════════════════


class AggregatorExtractor(BasePlatformExtractor):
    """Generic aggregator extractor for job boards that expose a simple API or
    HTML structure.

    Configured through a dictionary that defines the search URL template,
    pagination style, and CSS selectors for each site.  This is useful for
    custom or niche job boards.

    Parameters
    ----------
    browser : ManagedBrowser
        An active browser instance.
    rate_limit : RateLimitConfig or None
    site_config : dict or None
        Optional configuration overrides.  Defaults to a minimal config.
        Keys:
        - ``search_url_template`` (str) — URL with ``{query}`` placeholder.
        - ``card_selector`` (str) — CSS selector for listing cards.
        - ``title_selector`` (str) — CSS selector for the title within a card.
        - ``url_selector`` (str) — CSS selector for the link (``href`` extracted).
        - ``description_selector`` (str, optional).
        - ``company_selector`` (str, optional).
        - ``location_selector`` (str, optional).
        - ``paginate`` (bool, default True).
        - ``next_page_selector`` (str) — CSS selector for the "next" button.

    """

    def __init__(
        self,
        browser: ManagedBrowser,
        *,
        rate_limit: RateLimitConfig | None = None,
        site_config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            browser,
            rate_limit=rate_limit
            or RateLimitConfig(
                min_delay=3.0,
                max_delay=8.0,
                jitter_factor=0.3,
                requests_per_minute=10,
                max_pages_per_session=8,
                cooldown_after_session=45.0,
            ),
            credentials=credentials,
            settings=settings,
        )
        self._site_config: dict[str, Any] = {
            "search_url_template": "https://example.com/jobs?q={query}",
            "card_selector": "div.job-listing, li.job-item, tr.job-row",
            "title_selector": "h2 a, h3 a, a.job-title, a[class*='title']",
            "url_selector": "h2 a, h3 a, a.job-title, a[class*='title']",
            "description_selector": "p.description, div.description, span.summary",
            "company_selector": "span.company, div.company, .employer",
            "location_selector": "span.location, .job-location, [data-location]",
            "paginate": True,
            "next_page_selector": "a[rel='next'], a.next, .pagination a:last-child",
            **(site_config or {}),
        }

    # ── BasePlatformExtractor interface ─────────────────────────────────

    @property
    def platform_name(self) -> str:
        return "custom"

    @property
    def search_url_template(self) -> str:
        return self._site_config.get("search_url_template", "")

    async def login(self) -> bool:
        """Generic aggregator — authentication is site-dependent.

        Override in subclass or configure via *site_config*.
        """
        return True

    async def search(self, query: str) -> None:
        """Navigate to the search URL for *query*."""
        url = self.search_url_template.format(query=query.replace(" ", "+"))
        logger.info("aggregator.searching", query=query, url=url)

        try:
            await self._browser.retry_navigation(url, retries=2, timeout_ms=60_000)
            await self._random_delay(2.0, 4.0)
            await self._random_scroll()
        except Exception as exc:
            logger.exception("aggregator.search_navigation_error", query=query, error=str(exc))
            raise

    async def parse_results(self) -> list[RawLead]:
        """Parse job listing cards using the configured selectors."""
        leads: list[RawLead] = []

        try:
            cards = await self._browser.page.query_selector_all(self._site_config["card_selector"])
        except Exception as exc:
            logger.warning("aggregator.parse_no_cards", error=str(exc))
            return []

        for card in cards:
            try:
                lead = await self._parse_card(card)
                if lead:
                    leads.append(lead)
            except Exception as exc:
                logger.debug("aggregator.card_parse_error", error=str(exc))
                continue

        return leads

    async def next_page(self) -> bool:
        """Navigate to the next results page using the configured selector."""
        if not self._site_config.get("paginate", True):
            return False

        next_sel = self._site_config.get("next_page_selector")
        if not next_sel:
            return False

        try:
            btn = await self._browser.page.query_selector(next_sel)
            if not btn:
                return False

            is_disabled = await btn.get_attribute("disabled")
            if is_disabled is not None:
                return False

            await btn.scroll_into_view_if_needed()
            await self._random_delay(0.5, 1.5)
            await btn.click()
            await self._browser.wait_for_navigation(timeout_ms=30_000)
            await self._random_delay(2.0, 4.0)

            return True
        except Exception as exc:
            logger.debug("aggregator.next_page_error", error=str(exc))
            return False

    # ── Card parsing ────────────────────────────────────────────────────

    async def _parse_card(self, card: Any) -> RawLead | None:
        """Parse a single card using the configured selectors."""
        cfg = self._site_config

        title = await self._get_el_text(card, cfg["title_selector"])
        if not title:
            return None

        url = await self._get_el_href(card, cfg["url_selector"])
        description = await self._get_el_text(card, cfg.get("description_selector", ""))
        company = await self._get_el_text(card, cfg.get("company_selector", ""))
        location = await self._get_el_text(card, cfg.get("location_selector", ""))

        return RawLead(
            platform="custom",
            platform_job_id=_make_id(url or title),
            title=title.strip()[:500],
            company=company.strip() if company else None,
            description=(description or "").strip(),
            url=url,
            location=location.strip() if location else None,
        )

    # ── Element helpers ─────────────────────────────────────────────────

    @staticmethod
    async def _get_el_text(card: Any, selector: str) -> str:
        """Get inner text from a child element."""
        if not selector:
            return ""
        try:
            el = await card.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    @staticmethod
    async def _get_el_href(card: Any, selector: str) -> str | None:
        """Get ``href`` from a child anchor."""
        try:
            el = await card.query_selector(selector)
            if el:
                href = await el.get_attribute("href")
                if href and href.startswith("/"):
                    href = urljoin("https://example.com", href)
                return href
        except Exception:
            pass
        return None
