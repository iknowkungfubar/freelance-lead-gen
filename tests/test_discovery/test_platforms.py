"""Tests for platform-specific extractors — URL templates, rate-limit enforcement.

Each extractor inherits from :class:`BasePlatformExtractor` and must implement
platform-specific URL templates, search behaviour, and rate-limiting.  These
tests verify the non-browser aspects of each extractor without requiring a
real ``ManagedBrowser`` or network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.platforms.base import RateLimitConfig
from freelance_lead_gen.discovery.platforms.freelancer import FreelancerExtractor
from freelance_lead_gen.discovery.platforms.job_boards import (
    AggregatorExtractor,
    RemoteOKExtractor,
    YCWorkExtractor,
)
from freelance_lead_gen.discovery.platforms.linkedin import LinkedInExtractor
from freelance_lead_gen.discovery.platforms.upwork import UpworkExtractor

# ═══════════════════════════════════════════════════════════════════════════════
# URL template tests — each extractor's search_url_template formats correctly
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpworkUrlBuilding:
    """Upwork search URL construction."""

    @pytest.mark.asyncio
    async def test_upwork_search_url_contains_domain_and_query(self, mock_browser) -> None:
        """The Upwork URL template includes the domain and formatted query terms."""
        extractor = UpworkExtractor(browser=mock_browser)
        template = extractor.search_url_template
        url = template.format(query="python+developer")

        assert "https://" in url
        assert url.startswith("https://www.upwork.com/")
        assert "python" in url
        assert "developer" in url

    @pytest.mark.asyncio
    async def test_upwork_search_url_uses_recency_sort(self, mock_browser) -> None:
        """Default sort is by recency."""
        extractor = UpworkExtractor(browser=mock_browser)
        url = extractor.search_url_template.format(query="rag+engineer")

        assert "sort=recency" in url

    @pytest.mark.asyncio
    async def test_upwork_search_url_empty_query_returns_find_work(self, mock_browser) -> None:
        """When the query is empty the ``search`` method uses the Find Work URL instead.

        This doesn't call ``search()`` (which requires a real browser) but
        verifies the behaviour via the template property.
        """
        extractor = UpworkExtractor(browser=mock_browser)
        template = extractor.search_url_template

        # With an empty query, the formatted URL still follows the template pattern.
        url = template.format(query="")
        assert "upwork.com/nx/search/jobs/" in url


class TestLinkedInUrlBuilding:
    """LinkedIn Jobs search URL construction."""

    @pytest.mark.asyncio
    async def test_linkedin_search_url_contains_domain_and_query(self, mock_browser) -> None:
        """The LinkedIn URL template includes the domain and encoded query terms."""
        extractor = LinkedInExtractor(browser=mock_browser)
        template = extractor.search_url_template
        url = template.format(query="rag+engineer")

        assert "https://" in url
        assert url.startswith("https://www.linkedin.com/")
        assert "rag" in url
        assert "engineer" in url

    @pytest.mark.asyncio
    async def test_linkedin_search_url_has_date_sort_and_filters(self, mock_browser) -> None:
        """Default filters include contract type and date-sort parameters."""
        extractor = LinkedInExtractor(browser=mock_browser)
        url = extractor.search_url_template.format(query="python")

        assert "sortBy=DD" in url
        assert "f_E=" in url
        assert "distance=100" in url

    @pytest.mark.asyncio
    async def test_linkedin_search_url_spaces_encoded(self, mock_browser) -> None:
        """Query terms with spaces use percent-encoding."""
        extractor = LinkedInExtractor(browser=mock_browser)
        # The template has ``{query}`` — the ``search()`` method encodes spaces
        # with ``%20``, but the template itself is a raw format string.
        url = extractor.search_url_template.format(query="AI+consulting")

        assert "AI+consulting" in url


class TestFreelancerUrlBuilding:
    """Freelancer.com search URL construction."""

    @pytest.mark.asyncio
    async def test_freelancer_search_url_contains_domain_and_query(self, mock_browser) -> None:
        """The Freelancer URL template includes the domain and query parameters."""
        extractor = FreelancerExtractor(browser=mock_browser)
        template = extractor.search_url_template
        url = template.format(query="data+scientist")

        assert "https://" in url
        assert url.startswith("https://www.freelancer.com/")
        assert "keyword=data" in url
        assert "scientist" in url

    @pytest.mark.asyncio
    async def test_freelancer_search_url_date_sort(self, mock_browser) -> None:
        """Default sort is by date."""
        extractor = FreelancerExtractor(browser=mock_browser)
        url = extractor.search_url_template.format(query="python")

        assert "sort=date" in url

    @pytest.mark.asyncio
    async def test_freelancer_platform_name(self, mock_browser) -> None:
        """The platform name matches the registry key."""
        extractor = FreelancerExtractor(browser=mock_browser)
        assert extractor.platform_name == "freelancer"


class TestRemoteOkUrlBuilding:
    """Remote OK search URL construction."""

    @pytest.mark.asyncio
    async def test_remote_ok_search_url_contains_domain_and_query(self, mock_browser) -> None:
        """The Remote OK URL template includes the domain and query."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        template = extractor.search_url_template
        url = template.format(query="rust+developer")

        assert "https://" in url
        assert url.startswith("https://remoteok.com/")
        assert "rust" in url
        assert "developer" in url

    @pytest.mark.asyncio
    async def test_remote_ok_platform_name(self, mock_browser) -> None:
        """The platform name matches the registry key ``remote_ok``."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        assert extractor.platform_name == "remote_ok"

    @pytest.mark.asyncio
    async def test_remote_ok_login_not_required(self, mock_browser) -> None:
        """Remote OK returns ``True`` from login without authentication."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        result = await extractor.login()
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Rate-limit enforcement
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimitEnforcement:
    """The base class ``_enforce_rate_limit`` prevents request bursts."""

    @pytest.mark.asyncio
    async def test_enforce_rate_limit_does_not_raise(self, mock_browser) -> None:
        """Calling ``_enforce_rate_limit`` with near-zero delay does not raise."""
        extractor = UpworkExtractor(
            browser=mock_browser,
            rate_limit=RateLimitConfig(
                min_delay=0.001,
                max_delay=0.01,
            ),
        )

        # Even in rapid succession the method should not raise an exception.
        await extractor._enforce_rate_limit()
        await extractor._enforce_rate_limit()

    @pytest.mark.asyncio
    async def test_enforce_rate_limit_tracks_timing(self, mock_browser) -> None:
        """After enforcement, ``_last_request_time`` is updated to now."""
        extractor = UpworkExtractor(
            browser=mock_browser,
            rate_limit=RateLimitConfig(min_delay=0.001),
        )
        previous = extractor._last_request_time

        await extractor._enforce_rate_limit()

        assert extractor._last_request_time > previous

    @pytest.mark.asyncio
    async def test_rate_limit_config_defaults(self) -> None:
        """Default ``RateLimitConfig`` has sensible values."""
        config = RateLimitConfig()
        assert config.min_delay == 3.0
        assert config.max_delay == 10.0
        assert config.jitter_factor == 0.3
        assert config.requests_per_minute == 10
        assert config.max_pages_per_session == 10
        assert config.cooldown_after_session == 60.0

    @pytest.mark.asyncio
    async def test_upwork_uses_conservative_rate_limit(self, mock_browser) -> None:
        """Upwork uses more conservative defaults (longer delays, fewer RPM)."""
        extractor = UpworkExtractor(browser=mock_browser)
        rl = extractor._rate_limit
        assert rl.min_delay >= 5.0
        assert rl.requests_per_minute <= 6

    @pytest.mark.asyncio
    async def test_linkedin_uses_conservative_rate_limit(self, mock_browser) -> None:
        """LinkedIn uses the most conservative rate limits (longest delays)."""
        extractor = LinkedInExtractor(browser=mock_browser)
        rl = extractor._rate_limit
        assert rl.min_delay >= 8.0
        assert rl.requests_per_minute <= 4

    @pytest.mark.asyncio
    async def test_freelancer_moderate_rate_limit(self, mock_browser) -> None:
        """Freelancer uses moderate rate limits."""
        extractor = FreelancerExtractor(browser=mock_browser)
        rl = extractor._rate_limit
        assert rl.min_delay >= 4.0
        assert rl.requests_per_minute >= 6

    @pytest.mark.asyncio
    async def test_remote_ok_loose_rate_limit(self, mock_browser) -> None:
        """Remote OK (no auth needed) uses the most permissive rate limits."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        rl = extractor._rate_limit
        assert rl.min_delay >= 2.0
        assert rl.requests_per_minute >= 15


# ═══════════════════════════════════════════════════════════════════════════════
# Platform name & identity
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlatformIdentity:
    """Each extractor advertises its correct platform name."""

    @pytest.mark.asyncio
    async def test_upwork_platform_name(self, mock_browser) -> None:
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor.platform_name == "upwork"

    @pytest.mark.asyncio
    async def test_linkedin_platform_name(self, mock_browser) -> None:
        extractor = LinkedInExtractor(browser=mock_browser)
        assert extractor.platform_name == "linkedin"


# ═══════════════════════════════════════════════════════════════════════════════
# Y Combinator — Work at a Startup
# ═══════════════════════════════════════════════════════════════════════════════


class TestYCWorkUrlBuilding:
    """YC Work at a Startup search URL construction."""

    @pytest.mark.asyncio
    async def test_yc_work_search_url_contains_domain_and_query(self, mock_browser) -> None:
        """The YC Work URL template includes the domain and query parameter."""
        extractor = YCWorkExtractor(browser=mock_browser)
        template = extractor.search_url_template
        url = template.format(query="python+developer")

        assert "https://" in url
        assert url.startswith("https://www.workatastartup.com/")
        assert "python" in url
        assert "developer" in url

    @pytest.mark.asyncio
    async def test_yc_work_platform_name(self, mock_browser) -> None:
        """Platform name matches the registry key ``yc_work``."""
        extractor = YCWorkExtractor(browser=mock_browser)
        assert extractor.platform_name == "yc_work"

    @pytest.mark.asyncio
    async def test_yc_work_login_not_required(self, mock_browser) -> None:
        """YC Work does not require authentication to browse."""
        extractor = YCWorkExtractor(browser=mock_browser)
        result = await extractor.login()
        assert result is True

    @pytest.mark.asyncio
    async def test_yc_work_rate_limit_config(self, mock_browser) -> None:
        """YC Work uses permissive rate limits (no auth required)."""
        extractor = YCWorkExtractor(browser=mock_browser)
        rl = extractor._rate_limit
        assert rl.min_delay >= 2.0
        assert rl.max_delay >= 4.0
        assert rl.requests_per_minute >= 20


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregator (generic / custom job boards)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAggregatorUrlBuilding:
    """AggregatorExtractor — generic job board URL construction."""

    @pytest.mark.asyncio
    async def test_aggregator_default_search_url(self, mock_browser) -> None:
        """Default config uses example.com with a ``q`` parameter."""
        extractor = AggregatorExtractor(browser=mock_browser)
        url = extractor.search_url_template.format(query="python")

        assert "example.com" in url
        assert "q=python" in url

    @pytest.mark.asyncio
    async def test_aggregator_platform_name(self, mock_browser) -> None:
        """Platform name defaults to ``custom``."""
        extractor = AggregatorExtractor(browser=mock_browser)
        assert extractor.platform_name == "custom"

    @pytest.mark.asyncio
    async def test_aggregator_custom_site_config(self, mock_browser) -> None:
        """Custom site_config overrides the default template and selectors."""
        config = {
            "search_url_template": "https://custom-board.dev/jobs?q={query}&sort=new",
            "card_selector": "div.listing",
        }
        extractor = AggregatorExtractor(browser=mock_browser, site_config=config)
        url = extractor.search_url_template.format(query="rust")

        assert "custom-board.dev" in url
        assert "sort=new" in url
        # Default values should still be present for keys not overridden.
        assert extractor._site_config["paginate"] is True

    @pytest.mark.asyncio
    async def test_aggregator_default_site_config_has_all_keys(self, mock_browser) -> None:
        """Default site_config includes all expected selector keys."""
        extractor = AggregatorExtractor(browser=mock_browser)
        assert "search_url_template" in extractor._site_config
        assert "card_selector" in extractor._site_config
        assert "title_selector" in extractor._site_config
        assert "url_selector" in extractor._site_config
        assert extractor._site_config["paginate"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Static parsing / utility methods (no browser required)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpworkBudgetParsing:
    """UpworkExtractor._parse_upwork_budget — various budget format strings."""

    def test_parse_budget_range_hourly(self) -> None:
        """``$15-$30/hr`` → (15.0, 30.0)"""
        assert UpworkExtractor._parse_upwork_budget("$15-$30/hr") == (15.0, 30.0)

    def test_parse_budget_single_fixed(self) -> None:
        """``$500`` → (None, 500.0)"""
        assert UpworkExtractor._parse_upwork_budget("$500") == (None, 500.0)

    def test_parse_budget_hourly_label(self) -> None:
        """``Hourly: $20.00-$40.00`` → (20.0, 40.0)"""
        assert UpworkExtractor._parse_upwork_budget("Hourly: $20.00-$40.00") == (20.0, 40.0)

    def test_parse_budget_hourly_single_rate(self) -> None:
        """``$20/hr`` → (20.0, 20.0) — single rate treated as min and max."""
        assert UpworkExtractor._parse_upwork_budget("$20/hr") == (20.0, 20.0)

    def test_parse_budget_with_commas(self) -> None:
        """``Budget: $1,000`` → (None, 1000.0) — commas stripped."""
        assert UpworkExtractor._parse_upwork_budget("Budget: $1,000") == (None, 1000.0)

    def test_parse_budget_fixed_price(self) -> None:
        """``Fixed-price`` → (None, None) — no dollar amounts."""
        assert UpworkExtractor._parse_upwork_budget("Fixed-price") == (None, None)

    def test_parse_budget_empty_string(self) -> None:
        """Empty string → (None, None)."""
        assert UpworkExtractor._parse_upwork_budget("") == (None, None)


class TestUpworkJobIdExtraction:
    """UpworkExtractor._extract_upwork_job_id — job ID from URLs / text."""

    def test_extract_job_id_from_short_form(self) -> None:
        """Short tilde-prefixed ID is returned as-is."""
        result = UpworkExtractor._extract_upwork_job_id("~017d0b3c7a1c2b3d4e")
        assert result == "~017d0b3c7a1c2b3d4e"

    def test_extract_job_id_from_full_url(self) -> None:
        """Job URL with tilde ID is extracted correctly."""
        url = "https://www.upwork.com/jobs/~017d0b3c7a1c2b3d4e"
        result = UpworkExtractor._extract_upwork_job_id(url)
        assert result == "~017d0b3c7a1c2b3d4e"

    def test_extract_job_id_fallback_hash(self) -> None:
        """Non-matching text falls back to MD5 hash prefix (12 chars)."""
        result = UpworkExtractor._extract_upwork_job_id("some-random-text")
        assert len(result) == 12
        assert isinstance(result, str)


class TestLinkedInJobIdExtraction:
    """LinkedInExtractor._extract_linkedin_job_id — various URL formats."""

    def test_extract_id_from_jobs_view_url(self) -> None:
        """``/jobs/view/12345678/`` → ``12345678``"""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/view/12345678/",
            card,
        )
        assert result == "12345678"

    def test_extract_id_from_current_job_id_param(self) -> None:
        """``?currentJobId=87654321`` → ``87654321``"""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/search/?currentJobId=87654321",
            card,
        )
        assert result == "87654321"

    def test_extract_id_from_card_data_attribute(self) -> None:
        """Card ``data-job-id`` attribute takes highest priority."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = "55555555"
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/view/12345678/",
            card,
        )
        assert result == "55555555"

    def test_extract_id_fallback_hash(self) -> None:
        """When nothing matches, fall back to an MD5 hash prefix."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id("no-match-here", card)
        assert len(result) == 12
        assert isinstance(result, str)


class TestFreelancerBudgetParsing:
    """FreelancerExtractor._parse_freelancer_budget — various formats."""

    def test_parse_budget_range_usd(self) -> None:
        """``$10-$50`` → (10.0, 50.0)"""
        assert FreelancerExtractor._parse_freelancer_budget("$10-$50") == (10.0, 50.0)

    def test_parse_budget_single(self) -> None:
        """``$500`` → (500.0, None)"""
        assert FreelancerExtractor._parse_freelancer_budget("$500") == (500.0, None)

    def test_parse_budget_aud_currency(self) -> None:
        """``A$30-A$100`` → (30.0, 100.0)"""
        assert FreelancerExtractor._parse_freelancer_budget("A$30-A$100") == (30.0, 100.0)

    def test_parse_budget_eur_currency(self) -> None:
        """``EUR 50-200`` → (50.0, 200.0)"""
        assert FreelancerExtractor._parse_freelancer_budget("EUR 50-200") == (50.0, 200.0)

    def test_parse_budget_with_commas(self) -> None:
        """``$1,500-$3,000`` → (1500.0, 3000.0) — commas stripped."""
        assert FreelancerExtractor._parse_freelancer_budget("$1,500-$3,000") == (1500.0, 3000.0)

    def test_parse_budget_empty(self) -> None:
        """Empty string → (None, None)."""
        assert FreelancerExtractor._parse_freelancer_budget("") == (None, None)


class TestFreelancerJobIdExtraction:
    """FreelancerExtractor._extract_freelancer_job_id — URL formats."""

    def test_extract_id_from_projects_url(self) -> None:
        """``/projects/123456/`` → ``123456``"""
        result = FreelancerExtractor._extract_freelancer_job_id(
            "https://www.freelancer.com/projects/123456/"
        )
        assert result == "123456"

    def test_extract_id_from_query_param(self) -> None:
        """``project_id=789012`` → ``789012``"""
        result = FreelancerExtractor._extract_freelancer_job_id("project_id=789012")
        assert result == "789012"

    def test_extract_id_fallback_hash(self) -> None:
        """Non-matching text falls back to MD5 hash prefix (12 chars)."""
        result = FreelancerExtractor._extract_freelancer_job_id("some-text-without-id")
        assert len(result) == 12
        assert isinstance(result, str)


class TestRemoteOkSalaryParsing:
    """RemoteOKExtractor._parse_salary — salary range parsing."""

    def test_parse_salary_range_k(self) -> None:
        """``$80k-$120k`` → (80000.0, 120000.0)"""
        assert RemoteOKExtractor._parse_salary("$80k-$120k") == (80000.0, 120000.0)

    def test_parse_salary_single_k(self) -> None:
        """``$100k+`` → (100000.0, None)"""
        assert RemoteOKExtractor._parse_salary("$100k+") == (100000.0, None)

    def test_parse_salary_uppercase_k(self) -> None:
        """``80K-120K`` with uppercase ``K`` → (80000.0, 120000.0)."""
        assert RemoteOKExtractor._parse_salary("80K-120K") == (80000.0, 120000.0)

    def test_parse_salary_empty(self) -> None:
        """Empty string → (None, None)."""
        assert RemoteOKExtractor._parse_salary("") == (None, None)

    def test_parse_salary_none(self) -> None:
        """None → (None, None) — falsy input returns the empty tuple."""
        assert RemoteOKExtractor._parse_salary(None) == (None, None)


class TestMakeId:
    """_make_id from job_boards — hash-based ID generation."""

    def test_make_id_produces_12_char_hash(self) -> None:
        """Generated ID is always 12 hex characters."""
        from freelance_lead_gen.discovery.platforms.job_boards import _make_id

        result = _make_id("hello-world")
        assert len(result) == 12
        assert isinstance(result, str)

    def test_make_id_deterministic(self) -> None:
        """Same input produces the same output."""
        from freelance_lead_gen.discovery.platforms.job_boards import _make_id

        assert _make_id("test") == _make_id("test")

    def test_make_id_different_for_different_input(self) -> None:
        """Different inputs produce different hashes."""
        from freelance_lead_gen.discovery.platforms.job_boards import _make_id

        assert _make_id("abc") != _make_id("xyz")


# ═══════════════════════════════════════════════════════════════════════════════
# Constructor and config tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractorConfig:
    """Extractor constructors accept and store configuration."""

    @pytest.mark.asyncio
    async def test_upwork_custom_rate_limit(self, mock_browser) -> None:
        """Custom RateLimitConfig is stored and used instead of defaults."""
        config = RateLimitConfig(min_delay=1.0, max_delay=3.0)
        extractor = UpworkExtractor(browser=mock_browser, rate_limit=config)
        assert extractor._rate_limit.min_delay == 1.0
        assert extractor._rate_limit.max_delay == 3.0

    @pytest.mark.asyncio
    async def test_linkedin_custom_rate_limit(self, mock_browser) -> None:
        """Custom min_delay overrides LinkedIn's conservative default."""
        config = RateLimitConfig(min_delay=5.0)
        extractor = LinkedInExtractor(browser=mock_browser, rate_limit=config)
        assert extractor._rate_limit.min_delay == 5.0

    @pytest.mark.asyncio
    async def test_freelancer_custom_rate_limit(self, mock_browser) -> None:
        """Custom rate limit overrides Freelancer defaults."""
        config = RateLimitConfig(min_delay=2.0, max_delay=6.0)
        extractor = FreelancerExtractor(browser=mock_browser, rate_limit=config)
        assert extractor._rate_limit.min_delay == 2.0
        assert extractor._rate_limit.max_delay == 6.0

    @pytest.mark.asyncio
    async def test_extractor_stores_settings(self, mock_browser, test_settings) -> None:
        """Explicit settings object is stored on the extractor."""
        extractor = UpworkExtractor(browser=mock_browser, settings=test_settings)
        assert extractor._settings is test_settings

    @pytest.mark.asyncio
    async def test_extractor_stores_credentials(self, mock_browser) -> None:
        """Credentials dict is stored on the extractor."""
        creds = {"email": "test@example.com", "password": "secret123"}
        extractor = UpworkExtractor(browser=mock_browser, credentials=creds)
        assert extractor._credentials == creds

    @pytest.mark.asyncio
    async def test_upwork_extracts_email_from_credentials(self, mock_browser) -> None:
        """Email in credentials is extracted to ``_email``."""
        creds = {"email": "dev@example.com"}
        extractor = UpworkExtractor(browser=mock_browser, credentials=creds)
        assert extractor._email == "dev@example.com"

    @pytest.mark.asyncio
    async def test_upwork_extracts_username_from_credentials(self, mock_browser) -> None:
        """``username`` key is accepted as an email alias in credentials."""
        creds = {"username": "dev_user"}
        extractor = UpworkExtractor(browser=mock_browser, credentials=creds)
        assert extractor._email == "dev_user"


# ═══════════════════════════════════════════════════════════════════════════════
# Base class behaviour
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseExtractorBehaviour:
    """BasePlatformExtractor — non-browser behaviour that can be isolated."""

    @pytest.mark.asyncio
    async def test_session_expired_initial(self, mock_browser) -> None:
        """Before any session, _session_expired returns True (no start time)."""
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor._session_expired() is True

    @pytest.mark.asyncio
    async def test_session_expired_recent(self, mock_browser) -> None:
        """With a recent _session_start, _session_expired returns False."""
        import time

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._session_start = time.time() - 60  # 1 minute ago
        assert extractor._session_expired() is False

    @pytest.mark.asyncio
    async def test_session_expired_old(self, mock_browser) -> None:
        """With _session_start > 30 minutes ago, _session_expired returns True."""
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._session_start = 0.0  # Unix epoch — definitely expired
        assert extractor._session_expired() is True

    @pytest.mark.asyncio
    async def test_authenticated_flag_starts_false(self, mock_browser) -> None:
        """New extractor starts unauthenticated."""
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor._authenticated is False

    @pytest.mark.asyncio
    async def test_current_page_starts_zero(self, mock_browser) -> None:
        """New extractor starts at page 0."""
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor._current_page == 0

    @pytest.mark.asyncio
    async def test_last_request_time_starts_zero(self, mock_browser) -> None:
        """New extractor has ``_last_request_time`` at 0.0 (never called)."""
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor._last_request_time == 0.0

    @pytest.mark.asyncio
    async def test_random_delay_minimal(self, mock_browser) -> None:
        """_random_delay with tiny bounds executes without error (near-zero sleep)."""
        extractor = UpworkExtractor(browser=mock_browser)
        await extractor._random_delay(min_s=0.0001, max_s=0.0001)

    @pytest.mark.asyncio
    async def test_rate_limit_delay_executes(self, mock_browser, monkeypatch) -> None:
        """_rate_limit_delay calls asyncio.sleep (patched to avoid actual delay)."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)
        extractor = UpworkExtractor(
            browser=mock_browser,
            rate_limit=RateLimitConfig(min_delay=0.001, max_delay=0.002),
        )
        await extractor._rate_limit_delay()
        assert len(sleeps) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# HTTPS scheme — all platforms serve over HTTPS
# ═══════════════════════════════════════════════════════════════════════════════


class TestHttpsScheme:
    """All extractor search URLs use HTTPS."""

    @pytest.mark.asyncio
    async def test_upwork_url_https(self, mock_browser) -> None:
        extractor = UpworkExtractor(browser=mock_browser)
        assert extractor.search_url_template.startswith("https://")

    @pytest.mark.asyncio
    async def test_linkedin_url_https(self, mock_browser) -> None:
        extractor = LinkedInExtractor(browser=mock_browser)
        assert extractor.search_url_template.startswith("https://")

    @pytest.mark.asyncio
    async def test_freelancer_url_https(self, mock_browser) -> None:
        extractor = FreelancerExtractor(browser=mock_browser)
        assert extractor.search_url_template.startswith("https://")

    @pytest.mark.asyncio
    async def test_remote_ok_url_https(self, mock_browser) -> None:
        extractor = RemoteOKExtractor(browser=mock_browser)
        assert extractor.search_url_template.startswith("https://")

    @pytest.mark.asyncio
    async def test_yc_work_url_https(self, mock_browser) -> None:
        extractor = YCWorkExtractor(browser=mock_browser)
        assert extractor.search_url_template.startswith("https://")


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP-based extractor non-browser methods — parse_results / next_page / login
# ═══════════════════════════════════════════════════════════════════════════════


class TestRemoteOkNonBrowserMethods:
    """RemoteOKExtractor methods that don't need a real browser."""

    @pytest.mark.asyncio
    async def test_parse_results_returns_empty_list(self, mock_browser) -> None:
        """parse_results returns an empty list — API fetch is in extract_listings_raw."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        result = await extractor.parse_results()
        assert result == []

    @pytest.mark.asyncio
    async def test_next_page_returns_false(self, mock_browser) -> None:
        """All results are on one page; next_page returns False."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        assert await extractor.next_page() is False

    @pytest.mark.asyncio
    async def test_http_client_starts_none(self, mock_browser) -> None:
        """HTTP client is lazily initialised on first API call."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        assert extractor._http_client is None


class TestYCWorkNonBrowserMethods:
    """YCWorkExtractor methods that don't need a real browser."""

    @pytest.mark.asyncio
    async def test_parse_results_returns_empty_list(self, mock_browser) -> None:
        """parse_results returns an empty list — API fetch is in extract_listings_raw."""
        extractor = YCWorkExtractor(browser=mock_browser)
        result = await extractor.parse_results()
        assert result == []

    @pytest.mark.asyncio
    async def test_next_page_returns_false(self, mock_browser) -> None:
        """All results are on one page; next_page returns False."""
        extractor = YCWorkExtractor(browser=mock_browser)
        assert await extractor.next_page() is False

    @pytest.mark.asyncio
    async def test_http_client_starts_none(self, mock_browser) -> None:
        """HTTP client is lazily initialised on first API call."""
        extractor = YCWorkExtractor(browser=mock_browser)
        assert extractor._http_client is None


class TestAggregatorNonBrowserMethods:
    """AggregatorExtractor methods that don't need a real browser."""

    @pytest.mark.asyncio
    async def test_login_returns_true(self, mock_browser) -> None:
        """Default aggregator login succeeds without authentication."""
        extractor = AggregatorExtractor(browser=mock_browser)
        assert await extractor.login() is True


# ═══════════════════════════════════════════════════════════════════════════════
# Element helper static methods — tested with mocked Playwright handles
# ═══════════════════════════════════════════════════════════════════════════════


class TestElementHelpers:
    """Static element-helper methods (shared by all extractors)."""

    @pytest.mark.asyncio
    async def test_get_el_text_found(self) -> None:
        """Returns stripped text when the element is found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value="  Hello World  ")
        card.query_selector = AsyncMock(return_value=el)

        result = await AggregatorExtractor._get_el_text(card, "h2.title")
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_get_el_text_not_found(self) -> None:
        """Returns empty string when the element is not found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        result = await AggregatorExtractor._get_el_text(card, "h2.title")
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_el_text_exception(self) -> None:
        """Returns empty string when query_selector raises."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(side_effect=Exception("boom"))

        result = await AggregatorExtractor._get_el_text(card, "h2.title")
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_el_text_empty_selector(self) -> None:
        """Returns empty string when the selector is empty."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        result = await AggregatorExtractor._get_el_text(card, "")
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_el_href_absolute(self) -> None:
        """Relative href is joined with the base domain."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/jobs/123")
        card.query_selector = AsyncMock(return_value=el)

        result = await AggregatorExtractor._get_el_href(card, "a.title")
        assert result == "https://example.com/jobs/123"

    @pytest.mark.asyncio
    async def test_get_el_href_full_url(self) -> None:
        """Already-absolute href is returned unchanged."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="https://other.com/job/456")
        card.query_selector = AsyncMock(return_value=el)

        result = await AggregatorExtractor._get_el_href(card, "a.title")
        assert result == "https://other.com/job/456"

    @pytest.mark.asyncio
    async def test_get_el_href_not_found(self) -> None:
        """Returns None when the element is not found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        result = await AggregatorExtractor._get_el_href(card, "a.title")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_el_href_exception(self) -> None:
        """Returns None when get_attribute raises."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(side_effect=Exception("boom"))
        card.query_selector = AsyncMock(return_value=el)

        result = await AggregatorExtractor._get_el_href(card, "a.title")
        assert result is None


class TestElementHelpersPerPlatform:
    """Static element-helper methods on each platform."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extractor_cls",
        [UpworkExtractor, LinkedInExtractor, FreelancerExtractor],
    )
    async def test_get_el_text_found(self, extractor_cls) -> None:
        """Returns stripped text when element is found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value="  Job Title  ")
        card.query_selector = AsyncMock(return_value=el)

        result = await extractor_cls._get_el_text(card, "h2")
        assert result == "Job Title"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extractor_cls",
        [UpworkExtractor, LinkedInExtractor, FreelancerExtractor],
    )
    async def test_get_el_text_not_found(self, extractor_cls) -> None:
        """Returns empty string when element is not found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        result = await extractor_cls._get_el_text(card, "h2")
        assert result == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extractor_cls",
        [UpworkExtractor, LinkedInExtractor, FreelancerExtractor],
    )
    async def test_get_el_text_exception(self, extractor_cls) -> None:
        """Returns empty string when query_selector raises."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(side_effect=Exception("boom"))

        result = await extractor_cls._get_el_text(card, "h2")
        assert result == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extractor_cls",
        [UpworkExtractor, LinkedInExtractor, FreelancerExtractor],
    )
    async def test_get_el_href_found(self, extractor_cls) -> None:
        """Returns absolute URL when relative href is found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/jobs/456")
        card.query_selector = AsyncMock(return_value=el)

        result = await extractor_cls._get_el_href(card, "a.title")
        assert result is not None
        assert "jobs/456" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extractor_cls",
        [UpworkExtractor, LinkedInExtractor, FreelancerExtractor],
    )
    async def test_get_el_href_not_found(self, extractor_cls) -> None:
        """Returns None when element is not found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        result = await extractor_cls._get_el_href(card, "a.title")
        assert result is None


class TestFreelancerBudgetEdgeCases:
    """Additional Freelancer budget parsing edge cases."""

    def test_parse_budget_no_numbers(self) -> None:
        """``N/A`` → (None, None) — no numeric content after regex."""
        assert FreelancerExtractor._parse_freelancer_budget("N/A") == (None, None)


class TestLinkedInCredentialParsing:
    """LinkedInExtractor credential handling in __init__."""

    @pytest.mark.asyncio
    async def test_linkedin_stores_named_email(self, mock_browser) -> None:
        """Email passed as named parameter is stored."""
        extractor = LinkedInExtractor(browser=mock_browser, email="user@linkedin.com")
        assert extractor._email == "user@linkedin.com"

    @pytest.mark.asyncio
    async def test_linkedin_stores_named_password(self, mock_browser) -> None:
        """Password passed as named parameter is stored."""
        extractor = LinkedInExtractor(browser=mock_browser, password="hunter2")
        assert extractor._password == "hunter2"

    @pytest.mark.asyncio
    async def test_linkedin_credentials_email_extracted(self, mock_browser) -> None:
        """Email in credentials dict is extracted to ``_email``."""
        extractor = LinkedInExtractor(browser=mock_browser, credentials={"email": "li@example.com"})
        assert extractor._email == "li@example.com"

    @pytest.mark.asyncio
    async def test_linkedin_credentials_username_extracted(self, mock_browser) -> None:
        """Username alias in credentials is extracted to ``_email``."""
        extractor = LinkedInExtractor(browser=mock_browser, credentials={"username": "li_user"})
        assert extractor._email == "li_user"


class TestFreelancerCredentialParsing:
    """FreelancerExtractor credential handling in __init__."""

    @pytest.mark.asyncio
    async def test_freelancer_stores_named_email(self, mock_browser) -> None:
        """Email passed as named parameter is stored."""
        extractor = FreelancerExtractor(browser=mock_browser, email="dev@freelancer.com")
        assert extractor._email == "dev@freelancer.com"

    @pytest.mark.asyncio
    async def test_freelancer_stores_named_password(self, mock_browser) -> None:
        """Password passed as named parameter is stored."""
        extractor = FreelancerExtractor(browser=mock_browser, password="s3cret")
        assert extractor._password == "s3cret"

    @pytest.mark.asyncio
    async def test_freelancer_include_contests_default_false(self, mock_browser) -> None:
        """By default, contests are not included in extraction."""
        extractor = FreelancerExtractor(browser=mock_browser)
        assert extractor._include_contests is False

    @pytest.mark.asyncio
    async def test_freelancer_include_contests_enabled(self, mock_browser) -> None:
        """include_contests=True enables contest extraction."""
        extractor = FreelancerExtractor(browser=mock_browser, include_contests=True)
        assert extractor._include_contests is True


# ═══════════════════════════════════════════════════════════════════════════════
# _api_job_to_lead — RemoteOK and YC Work dict-to-RawLead conversion
# ═══════════════════════════════════════════════════════════════════════════════


class TestRemoteOkApiJobToLead:
    """RemoteOKExtractor._api_job_to_lead — direct dict parsing, no HTTP."""

    @pytest.mark.asyncio
    async def test_full_job_dict(self, mock_browser) -> None:
        """A full job dict is parsed into a RawLead."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {
            "position": "Python Developer",
            "company": "Acme Remote",
            "description": "<p>Build APIs</p>",
            "url": "https://remoteok.com/jobs/python-dev-123",
            "salary": "$80k-$120k",
            "tags": ["python", "django", "api"],
            "date": "2025-06-01",
            "location": "Remote (Global)",
        }
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.platform == "remote_ok"
        assert lead.title == "Python Developer"
        assert lead.company == "Acme Remote"
        assert lead.budget_min == 80000.0
        assert lead.budget_max == 120000.0
        assert "python" in lead.skills
        assert lead.location == "Remote (Global)"
        assert lead.url == "https://remoteok.com/jobs/python-dev-123"

    @pytest.mark.asyncio
    async def test_minimal_job_dict(self, mock_browser) -> None:
        """A job with only a title still produces a valid RawLead."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"position": "Backend Engineer"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.title == "Backend Engineer"
        assert lead.company is None
        assert lead.description == ""
        assert lead.location == "Remote"

    @pytest.mark.asyncio
    async def test_empty_title_returns_none(self, mock_browser) -> None:
        """A job with no title/position returns None."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"company": "Ghost Inc", "url": "https://remoteok.com/x"}
        lead = extractor._api_job_to_lead(job)
        assert lead is None


class TestYCWorkApiJobToLead:
    """YCWorkExtractor._api_job_to_lead — dict-to-RawLead conversion."""

    @pytest.mark.asyncio
    async def test_full_job_dict(self, mock_browser) -> None:
        """A full YC Work job dict is parsed into a RawLead."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {
            "title": "Full Stack Engineer",
            "company": {"name": "YC Startup Inc"},
            "description": "Join us building the future",
            "id": "yc-job-001",
            "salaryHigh": 200000.0,
            "salaryLow": 150000.0,
            "skills": ["react", "python", "aws"],
            "location": "San Francisco, CA",
            "createdAt": "2025-05-15",
        }
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.platform == "yc_work"
        assert lead.title == "Full Stack Engineer"
        assert lead.company == "YC Startup Inc"
        assert lead.budget_min == 150000.0
        assert lead.budget_max == 200000.0
        assert "react" in lead.skills
        assert lead.location == "San Francisco, CA"

    @pytest.mark.asyncio
    async def test_string_company_name(self, mock_browser) -> None:
        """When company is a string (not dict), it's used directly."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {
            "title": "Backend Lead",
            "company_name": "Simple Corp",
            "description": "",
            "id": "simple-001",
        }
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.company == "Simple Corp"

    @pytest.mark.asyncio
    async def test_empty_title_returns_none(self, mock_browser) -> None:
        """A job with no title returns None."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"description": "no title here"}
        lead = extractor._api_job_to_lead(job)
        assert lead is None


# ═══════════════════════════════════════════════════════════════════════════════
# Skills extraction and Easy Apply — tested with mocked cards
# ═══════════════════════════════════════════════════════════════════════════════


class TestSkillsExtraction:
    """Platform-specific skills extraction with mock cards."""

    @pytest.mark.asyncio
    async def test_upwork_parse_skills_found(self) -> None:
        """UpworkExtractor._parse_skills returns skills from mock card."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el1 = AsyncMock()
        el1.inner_text = AsyncMock(return_value="  Python  ")
        el2 = AsyncMock()
        el2.inner_text = AsyncMock(return_value="  Django  ")
        card.query_selector_all = AsyncMock(return_value=[el1, el2])

        extractor = UpworkExtractor(browser=AsyncMock(spec=[]))
        skills = await extractor._parse_skills(card)
        assert skills == ["Python", "Django"]

    @pytest.mark.asyncio
    async def test_upwork_parse_skills_exception(self) -> None:
        """UpworkExtractor._parse_skills returns [] on error."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector_all = AsyncMock(side_effect=Exception("boom"))

        extractor = UpworkExtractor(browser=AsyncMock(spec=[]))
        skills = await extractor._parse_skills(card)
        assert skills == []


class TestEasyApplyDetection:
    """LinkedIn Easy Apply detection with mock cards."""

    @pytest.mark.asyncio
    async def test_easy_apply_found(self) -> None:
        """_detect_easy_apply returns True when indicator element exists."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=AsyncMock())

        extractor = LinkedInExtractor(browser=AsyncMock(spec=[]))
        result = await extractor._detect_easy_apply(card)
        assert result is True

    @pytest.mark.asyncio
    async def test_easy_apply_not_found(self) -> None:
        """_detect_easy_apply returns False when indicator element is absent."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        extractor = LinkedInExtractor(browser=AsyncMock(spec=[]))
        result = await extractor._detect_easy_apply(card)
        assert result is False

    @pytest.mark.asyncio
    async def test_easy_apply_exception(self) -> None:
        """_detect_easy_apply returns False on exception."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(side_effect=Exception("boom"))

        extractor = LinkedInExtractor(browser=AsyncMock(spec=[]))
        result = await extractor._detect_easy_apply(card)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Salary parsing edge case — no numeric content
# ═══════════════════════════════════════════════════════════════════════════════


class TestRemoteOkSalaryEdgeCases:
    """Remote OK salary parsing edge cases."""

    def test_parse_salary_no_numbers(self) -> None:
        """``negotiable`` → (None, None) — non-empty but no digits found."""
        assert RemoteOKExtractor._parse_salary("negotiable") == (None, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Base class — extract_listings format, ensure_authenticated exception paths,
# refresh_session behaviour, utility helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseExtractorAdvanced:
    """BasePlatformExtractor — deeper coverage of helpers and edge cases."""

    # ── extract_listings dict format ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_extract_listings_dict_format(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """extract_listings returns a list of dicts with expected keys."""

        extractor = UpworkExtractor(browser=mock_browser)
        raw = RawLead(
            platform="upwork",
            platform_job_id="~test123",
            title="Test Job",
            company="Acme Inc",
            description="A great job",
            url="https://upwork.com/jobs/~test123",
            posted_date="2026-06-01",
            budget_min=50.0,
            budget_max=100.0,
            currency="USD",
            skills=["Python"],
            location="Remote",
        )
        extractor.extract_listings_raw = AsyncMock(return_value=[raw])  # type: ignore[misc]

        result = await extractor.extract_listings()

        assert len(result) == 1
        entry = result[0]
        assert entry["platform"] == "upwork"
        assert entry["platform_job_id"] == "~test123"
        assert entry["title"] == "Test Job"
        assert entry["company"] == "Acme Inc"
        assert entry["description"] == "A great job"
        assert entry["url"] == "https://upwork.com/jobs/~test123"
        assert entry["posted_date"] == "2026-06-01"
        assert entry["budget_min"] == 50.0
        assert entry["budget_max"] == 100.0
        assert entry["currency"] == "USD"
        assert entry["skills"] == ["Python"]
        assert entry["location"] == "Remote"

    # ── ensure_authenticated — exception path ─────────────────────────

    @pytest.mark.asyncio
    async def test_ensure_authenticated_login_raises(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When login() raises, _authenticated stays False and False is returned."""
        extractor = UpworkExtractor(browser=mock_browser)
        extractor.login = AsyncMock(  # type: ignore[misc]
            side_effect=RuntimeError("network error")
        )
        extractor._authenticated = False

        result = await extractor.ensure_authenticated()

        assert result is False
        assert extractor._authenticated is False

    @pytest.mark.asyncio
    async def test_ensure_authenticated_already_authenticated(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When already authenticated, ensure_authenticated returns True immediately."""
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._authenticated = True

        result = await extractor.ensure_authenticated()

        assert result is True

    # ── refresh_session ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_refresh_session_force_true(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """refresh_session(force=True) calls ensure_authenticated and returns its result."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._authenticated = False

        result = await extractor.refresh_session(force=True)

        # Login with the mock browser succeeds, so result should be True.
        assert result is True

    @pytest.mark.asyncio
    async def test_refresh_session_not_expired(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """refresh_session(force=False) with valid session returns _authenticated directly."""
        import time

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._session_start = time.time() - 60
        extractor._authenticated = True

        result = await extractor.refresh_session(force=False)

        assert result is True

    @pytest.mark.asyncio
    async def test_refresh_session_expired(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """refresh_session(force=False) with expired session re-authenticates."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._session_start = 0.0
        extractor._authenticated = False

        result = await extractor.refresh_session(force=False)

        # Session is expired → calls ensure_authenticated → login succeeds.
        assert result is True
        assert extractor._authenticated is True

    # ── _type_like_human with empty text ───────────────────────────────

    @pytest.mark.asyncio
    async def test_type_like_human_empty_text(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """_type_like_human with empty text returns immediately (no browser call)."""
        extractor = UpworkExtractor(browser=mock_browser)

        await extractor._type_like_human("input#email", "")

        mock_browser.type_text.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Upwork — search URL construction and element helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpworkSearch:
    """UpworkExtractor.search — URL building and navigation behaviour."""

    @pytest.mark.asyncio
    async def test_upwork_search_with_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() replaces spaces with + and navigates to the search URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        # Make _random_scroll a no-op too.
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("python developer")

        expected_url = "https://www.upwork.com/nx/search/jobs/?q=python+developer&sort=recency"
        mock_browser.retry_navigation.assert_awaited_once_with(
            expected_url, retries=2, timeout_ms=60_000
        )

    @pytest.mark.asyncio
    async def test_upwork_search_empty_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() with empty query navigates to the Find Work URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("")

        mock_browser.retry_navigation.assert_awaited_once_with(
            "https://www.upwork.com/nx/find-work/",
            retries=2,
            timeout_ms=60_000,
        )

    @pytest.mark.asyncio
    async def test_upwork_search_special_chars(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() handles queries with special characters by joining with +."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("C# / .NET")

        mock_browser.retry_navigation.assert_awaited_once()
        call_url = mock_browser.retry_navigation.call_args[0][0]
        assert "C%23" not in call_url  # spaces → +, not URL-encoded
        assert "C%2B%23" not in call_url
        # The query should be "C#+/+.NET" (spaces replaced with +)
        assert "C#" in call_url or "C%23" in call_url or "q=C" in call_url


class TestUpworkGetElHref:
    """UpworkExtractor._get_el_href — relative and absolute URL handling."""

    @pytest.mark.asyncio
    async def test_upwork_get_el_href_relative(self) -> None:
        """Relative href is prefixed with the Upwork domain."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/jobs/~test123")
        card.query_selector = AsyncMock(return_value=el)

        result = await UpworkExtractor._get_el_href(card, "a.title")
        assert result == "https://www.upwork.com/jobs/~test123"

    @pytest.mark.asyncio
    async def test_upwork_get_el_href_absolute(self) -> None:
        """Already-absolute href is returned as-is."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="https://other.com/job/789")
        card.query_selector = AsyncMock(return_value=el)

        result = await UpworkExtractor._get_el_href(card, "a.title")
        assert result == "https://other.com/job/789"

    @pytest.mark.asyncio
    async def test_upwork_get_el_href_exception(self) -> None:
        """Exception during get_attribute returns None."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(side_effect=Exception("boom"))
        card.query_selector = AsyncMock(return_value=el)

        result = await UpworkExtractor._get_el_href(card, "a.title")
        assert result is None


class TestUpworkGetElText:
    """UpworkExtractor._get_el_text edge cases."""

    @pytest.mark.asyncio
    async def test_upwork_get_el_text_with_selector(self) -> None:
        """Returns stripped text when the element is found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.inner_text = AsyncMock(return_value="  Job Title  ")
        card.query_selector = AsyncMock(return_value=el)

        result = await UpworkExtractor._get_el_text(card, "h2.title")
        assert result == "Job Title"

    @pytest.mark.asyncio
    async def test_upwork_get_el_text_not_found(self) -> None:
        """Returns empty string when element is not found."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        result = await UpworkExtractor._get_el_text(card, "h2.title")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Freelancer — URL building, contest parsing, and helper methods
# ═══════════════════════════════════════════════════════════════════════════════


class TestFreelancerSearch:
    """FreelancerExtractor.search — URL building."""

    @pytest.mark.asyncio
    async def test_freelancer_search_with_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() replaces spaces with + and navigates to the correct URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = FreelancerExtractor(browser=mock_browser)
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("data scientist")

        expected_url = (
            "https://www.freelancer.com/search/projects/?keyword=data+scientist&sort=date"
        )
        mock_browser.retry_navigation.assert_awaited_once_with(
            expected_url, retries=2, timeout_ms=60_000
        )

    @pytest.mark.asyncio
    async def test_freelancer_search_empty_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() with empty query still builds a valid URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = FreelancerExtractor(browser=mock_browser)
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("")

        expected_url = "https://www.freelancer.com/search/projects/?keyword=&sort=date"
        mock_browser.retry_navigation.assert_awaited_once_with(
            expected_url, retries=2, timeout_ms=60_000
        )


class TestFreelancerContestParsing:
    """FreelancerExtractor contest support and card parsing."""

    @pytest.mark.asyncio
    async def test_parse_results_with_contests(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """parse_results includes contests when include_contests=True.

        This tests that both project and contest cards are queried.
        """
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = FreelancerExtractor(
            browser=mock_browser,
            include_contests=True,
        )
        # Replace _browser with a mock that has a properly configured page.
        # This avoids the spec-managed PropertyMock that creates new mocks on each access.
        page_mock = AsyncMock()
        page_mock.query_selector_all = AsyncMock(return_value=[])
        mock_browser_with_page = AsyncMock()
        mock_browser_with_page.page = page_mock
        extractor._browser = mock_browser_with_page  # type: ignore[assignment]

        results = await extractor.parse_results()

        assert results == []
        # query_selector_all called twice: once for projects, once for contests.
        assert page_mock.query_selector_all.call_count == 2

    @pytest.mark.asyncio
    async def test_parse_results_without_contests(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """parse_results without contests only queries project cards."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = FreelancerExtractor(
            browser=mock_browser,
            include_contests=False,
        )
        page_mock = AsyncMock()
        page_mock.query_selector_all = AsyncMock(return_value=[])
        mock_browser_with_page = AsyncMock()
        mock_browser_with_page.page = page_mock
        extractor._browser = mock_browser_with_page  # type: ignore[assignment]

        results = await extractor.parse_results()

        assert results == []
        # Only project selector queried.
        assert page_mock.query_selector_all.call_count == 1

    @pytest.mark.asyncio
    async def test_parse_contest_card(self) -> None:
        """_parse_contest_card produces a lead with [Contest] prefix."""
        from unittest.mock import AsyncMock

        card = AsyncMock()

        # Title element must support both inner_text and get_attribute
        # since _parse_contest_card uses _get_el_text and _get_el_href
        # with the same selector.
        title_el = AsyncMock()
        title_el.inner_text = AsyncMock(return_value="  Logo Design  ")
        title_el.get_attribute = AsyncMock(return_value="/projects/98765")
        # Description
        desc_el = AsyncMock()
        desc_el.inner_text = AsyncMock(return_value="  Design a logo  ")
        # Budget
        budget_el = AsyncMock()
        budget_el.inner_text = AsyncMock(return_value="  $50-$200  ")

        card.query_selector = AsyncMock()

        # Return different elements for different selectors
        async def _mock_qs(sel: str) -> AsyncMock | None:
            mapping = {
                "a.JobSearchCard-primary-heading-link, a.ProjectCard-project-title-link, "
                "a[class*='project-title'], h2 a": title_el,
                "p.JobSearchCard-primary-description, div.ProjectCard-description, "
                "div[class*='project-description'], p[class*='description']": desc_el,
                "span.JobSearchCard-primary-price, div.ProjectCard-budget, "
                "strong[class*='budget'], span[class*='price']": budget_el,
            }
            return mapping.get(sel)  # type: ignore[arg-type]

        card.query_selector = _mock_qs

        extractor = FreelancerExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        lead = await extractor._parse_contest_card(card)

        assert lead is not None
        assert lead.title == "[Contest] Logo Design"
        assert lead.platform == "freelancer"
        assert lead.budget_min == 50.0
        assert lead.budget_max == 200.0


class TestFreelancerParseSkills:
    """FreelancerExtractor._parse_skills with mocked cards."""

    @pytest.mark.asyncio
    async def test_parse_skills_found(self) -> None:
        """Skills are extracted from skill elements."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el1 = AsyncMock()
        el1.inner_text = AsyncMock(return_value="  Python  ")
        el2 = AsyncMock()
        el2.inner_text = AsyncMock(return_value="  Django  ")
        card.query_selector_all = AsyncMock(return_value=[el1, el2])

        extractor = FreelancerExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        skills = await extractor._parse_skills(card)

        assert skills == ["Python", "Django"]

    @pytest.mark.asyncio
    async def test_parse_skills_empty(self) -> None:
        """Empty skill list returns []."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector_all = AsyncMock(return_value=[])

        extractor = FreelancerExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        skills = await extractor._parse_skills(card)

        assert skills == []

    @pytest.mark.asyncio
    async def test_parse_skills_exception(self) -> None:
        """Exception during parsing returns []."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector_all = AsyncMock(side_effect=Exception("boom"))

        extractor = FreelancerExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        skills = await extractor._parse_skills(card)

        assert skills == []


class TestFreelancerJobIdEdgeCases:
    """FreelancerExtractor._extract_freelancer_job_id edge cases."""

    def test_extract_id_from_empty_string(self) -> None:
        """Empty string falls back to hash."""
        result = FreelancerExtractor._extract_freelancer_job_id("")
        assert len(result) == 12
        assert isinstance(result, str)

    def test_extract_id_with_project_in_path(self) -> None:
        """URL path with /projects/NNNN is extracted."""
        result = FreelancerExtractor._extract_freelancer_job_id(
            "https://www.freelancer.com/projects/55555/Some-Title"
        )
        assert result == "55555"


# ═══════════════════════════════════════════════════════════════════════════════
# LinkedIn — element helper edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestLinkedInGetElHref:
    """LinkedInExtractor._get_el_href — relative and absolute URL handling."""

    @pytest.mark.asyncio
    async def test_linkedin_get_el_href_relative(self) -> None:
        """Relative href is prefixed with linkedin.com base (query stripped)."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/jobs/view/12345678/?ref=search")
        card.query_selector = AsyncMock(return_value=el)

        result = await LinkedInExtractor._get_el_href(card, "a.title")
        assert result == "https://www.linkedin.com/jobs/view/12345678/"

    @pytest.mark.asyncio
    async def test_linkedin_get_el_href_absolute(self) -> None:
        """Already-absolute href is returned unchanged."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="https://linkedin.com/jobs/1")
        card.query_selector = AsyncMock(return_value=el)

        result = await LinkedInExtractor._get_el_href(card, "a.title")
        assert result == "https://linkedin.com/jobs/1"

    @pytest.mark.asyncio
    async def test_linkedin_get_el_href_exception(self) -> None:
        """Exception during get_attribute returns None."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(side_effect=Exception("boom"))
        card.query_selector = AsyncMock(return_value=el)

        result = await LinkedInExtractor._get_el_href(card, "a.title")
        assert result is None


class TestLinkedInJobIdEdgeCases:
    """LinkedInExtractor._extract_linkedin_job_id edge cases."""

    def test_extract_id_from_empty_url(self) -> None:
        """Empty URL falls back to hash."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id("", card)
        assert len(result) == 12

    def test_extract_id_with_card_data_attribute_none(self) -> None:
        """When data-job-id is None, falls through to URL pattern."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/view/99999/",
            card,
        )
        assert result == "99999"


class TestLinkedInSearch:
    """LinkedInExtractor.search — URL building."""

    @pytest.mark.asyncio
    async def test_linkedin_search_with_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() encodes spaces as %20 and builds the correct URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = LinkedInExtractor(browser=mock_browser)
        extractor._slow_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("machine learning")

        mock_browser.retry_navigation.assert_awaited_once()
        call_url = mock_browser.retry_navigation.call_args[0][0]
        assert call_url.startswith("https://www.linkedin.com/jobs/search/")
        assert "keywords=machine%20learning" in call_url


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregator — search, card parsing, and pagination edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestAggregatorSearch:
    """AggregatorExtractor.search — URL building."""

    @pytest.mark.asyncio
    async def test_aggregator_search_with_query(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """search() replaces spaces with + and navigates to the configured URL."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = AggregatorExtractor(browser=mock_browser)
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("full stack")

        mock_browser.retry_navigation.assert_awaited_once_with(
            "https://example.com/jobs?q=full+stack",
            retries=2,
            timeout_ms=60_000,
        )

    @pytest.mark.asyncio
    async def test_aggregator_search_custom_url(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Custom site_config search_url_template is used for navigation."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        config = {
            "search_url_template": "https://custom.dev/jobs?q={query}&sort=new",
        }
        extractor = AggregatorExtractor(
            browser=mock_browser,
            site_config=config,
        )
        extractor._random_scroll = AsyncMock()  # type: ignore[misc]

        await extractor.search("rust")

        mock_browser.retry_navigation.assert_awaited_once_with(
            "https://custom.dev/jobs?q=rust&sort=new",
            retries=2,
            timeout_ms=60_000,
        )


class TestAggregatorCardParsing:
    """AggregatorExtractor._parse_card — full card parsing with mocks."""

    @pytest.mark.asyncio
    async def test_aggregator_parse_card_full(self) -> None:
        """A fully populated card produces a complete RawLead."""
        from unittest.mock import AsyncMock

        card = AsyncMock()

        # Configure query_selector to return different mocks per selector.
        # The title/url selector is the same string, so the element mock must
        # support both inner_text (for _get_el_text) and get_attribute (for _get_el_href).
        title_el = AsyncMock()
        title_el.inner_text = AsyncMock(return_value="  Senior Engineer  ")
        title_el.get_attribute = AsyncMock(return_value="/jobs/42")
        desc_el = AsyncMock()
        desc_el.inner_text = AsyncMock(return_value="  Build stuff  ")
        company_el = AsyncMock()
        company_el.inner_text = AsyncMock(return_value="  Tech Co  ")
        location_el = AsyncMock()
        location_el.inner_text = AsyncMock(return_value="  Remote  ")

        async def _mock_qs(sel: str) -> AsyncMock | None:
            mapping = {
                "h2 a, h3 a, a.job-title, a[class*='title']": title_el,
                "p.description, div.description, span.summary": desc_el,
                "span.company, div.company, .employer": company_el,
                "span.location, .job-location, [data-location]": location_el,
            }
            return mapping.get(sel)  # type: ignore[arg-type]

        card.query_selector = _mock_qs

        extractor = AggregatorExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        lead = await extractor._parse_card(card)

        assert lead is not None
        assert lead.title == "Senior Engineer"
        assert lead.url == "https://example.com/jobs/42"
        assert lead.company == "Tech Co"
        assert lead.description == "Build stuff"
        assert lead.location == "Remote"
        assert lead.platform == "custom"

    @pytest.mark.asyncio
    async def test_aggregator_parse_card_no_title(self) -> None:
        """A card with no title returns None."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        card.query_selector = AsyncMock(return_value=None)

        extractor = AggregatorExtractor(browser=AsyncMock(spec=[]))  # type: ignore[arg-type]
        lead = await extractor._parse_card(card)

        assert lead is None


class TestAggregatorNextPage:
    """AggregatorExtractor.next_page — pagination edge cases."""

    @pytest.mark.asyncio
    async def test_next_page_no_paginate(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When paginate is False, next_page returns False immediately."""
        config = {"paginate": False}
        extractor = AggregatorExtractor(browser=mock_browser, site_config=config)

        result = await extractor.next_page()

        assert result is False

    @pytest.mark.asyncio
    async def test_next_page_no_selector(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When next_page_selector is empty, next_page returns False."""
        config = {"paginate": True, "next_page_selector": ""}
        extractor = AggregatorExtractor(browser=mock_browser, site_config=config)

        result = await extractor.next_page()

        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Remote OK — API job-to-lead edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestRemoteOkApiJobToLeadEdgeCases:
    """RemoteOKExtractor._api_job_to_lead — additional edge cases."""

    @pytest.mark.asyncio
    async def test_title_via_title_key(self, mock_browser: AsyncMock) -> None:
        """When 'position' is absent, 'title' is used instead."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"title": "DevOps Engineer", "company": "CloudKit"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.title == "DevOps Engineer"
        assert lead.company == "CloudKit"

    @pytest.mark.asyncio
    async def test_no_tags(self, mock_browser: AsyncMock) -> None:
        """When tags are absent, skills list is empty."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"position": "Backend Dev", "salary": "$90k-$130k"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.skills == []
        assert lead.budget_min == 90000.0
        assert lead.budget_max == 130000.0

    @pytest.mark.asyncio
    async def test_apply_url_fallback(self, mock_browser: AsyncMock) -> None:
        """When 'url' is absent, 'apply_url' is used."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {
            "position": "QA Engineer",
            "apply_url": "https://remoteok.com/apply/qa-99",
        }
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.url == "https://remoteok.com/apply/qa-99"

    @pytest.mark.asyncio
    async def test_categories_as_tags(self, mock_browser: AsyncMock) -> None:
        """When 'tags' is absent, 'categories' is used."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"position": "Frontend Dev", "categories": ["react", "css"]}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert "react" in lead.skills
        assert "css" in lead.skills

    @pytest.mark.asyncio
    async def test_location_fallback(self, mock_browser: AsyncMock) -> None:
        """When location is absent, defaults to 'Remote'."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"position": "DevRole"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.location == "Remote"

    @pytest.mark.asyncio
    async def test_empty_tags_list(self, mock_browser: AsyncMock) -> None:
        """Tags list with falsy entries is filtered."""
        extractor = RemoteOKExtractor(browser=mock_browser)
        job = {"position": "Engineer", "tags": ["", "python", None]}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.skills == ["python"]


# ═══════════════════════════════════════════════════════════════════════════════
# YC Work — API job-to-lead and _parse_embedded_json
# ═══════════════════════════════════════════════════════════════════════════════


class TestYCWorkApiJobToLeadEdgeCases:
    """YCWorkExtractor._api_job_to_lead — additional edge cases."""

    @pytest.mark.asyncio
    async def test_position_fallback(self, mock_browser: AsyncMock) -> None:
        """When 'title' is absent, 'position' is used."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"position": "Software Engineer", "id": "pos-001"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.title == "Software Engineer"

    @pytest.mark.asyncio
    async def test_company_as_dict(self, mock_browser: AsyncMock) -> None:
        """company dict with name key is resolved."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"title": "Engineer", "company": {"name": "Startup Inc"}}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.company == "Startup Inc"

    @pytest.mark.asyncio
    async def test_no_company(self, mock_browser: AsyncMock) -> None:
        """When no company info is present, company is None."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"title": "Solo Engineer"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.company is None

    @pytest.mark.asyncio
    async def test_salary_as_number(self, mock_browser: AsyncMock) -> None:
        """salaryHigh/salaryLow as numbers are used directly."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {
            "title": "Data Scientist",
            "salaryHigh": 250000,
            "salaryLow": 180000,
        }
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.budget_min == 180000.0
        assert lead.budget_max == 250000.0

    @pytest.mark.asyncio
    async def test_salary_min_fallback(self, mock_browser: AsyncMock) -> None:
        """salaryMin is used when salaryLow is absent."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"title": "Engineer", "salaryMax": 200000, "salaryMin": 150000}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.budget_min == 150000.0
        assert lead.budget_max == 200000.0

    @pytest.mark.asyncio
    async def test_no_skills_list(self, mock_browser: AsyncMock) -> None:
        """When skills is not a list, defaults to empty."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"title": "Engineer", "skills": "python"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.skills == []

    @pytest.mark.asyncio
    async def test_location_or_office_location(self, mock_browser: AsyncMock) -> None:
        """officeLocation is used when location is absent."""
        extractor = YCWorkExtractor(browser=mock_browser)
        job = {"title": "Engineer", "officeLocation": "NYC"}
        lead = extractor._api_job_to_lead(job)
        assert lead is not None
        assert lead.location == "NYC"


class TestYCWorkParseEmbeddedJson:
    """YCWorkExtractor._parse_embedded_json — static HTML parsing."""

    @pytest.mark.asyncio
    async def test_parse_next_data_json(self) -> None:
        """__NEXT_DATA__ script tag is parsed correctly."""
        html = """
        <html>
        <script id="__NEXT_DATA__" type="application/json">
        {"props": {"pageProps": {"jobs": [{"title": "Next Dev"}]}}}
        </script>
        </html>
        """
        result = await YCWorkExtractor._parse_embedded_json(html)
        assert result is not None
        # Returns the parsed JSON data.
        assert isinstance(result, dict)
        assert result["props"]["pageProps"]["jobs"][0]["title"] == "Next Dev"

    @pytest.mark.asyncio
    async def test_parse_initial_state(self) -> None:
        """window.__INITIAL_STATE__ is parsed correctly."""
        html = """
        <script>
        window.__INITIAL_STATE__ = {"jobs": [{"title": "State Dev"}]};
        </script>
        """
        result = await YCWorkExtractor._parse_embedded_json(html)
        assert result is not None
        assert isinstance(result, dict)
        assert result["jobs"][0]["title"] == "State Dev"

    @pytest.mark.asyncio
    async def test_parse_no_match(self) -> None:
        """HTML with no embedded JSON returns an empty list."""
        html = "<html><body>Nothing here</body></html>"
        result = await YCWorkExtractor._parse_embedded_json(html)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Upwork — additional budget parsing edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpworkBudgetEdgeCases:
    """UpworkExtractor._parse_upwork_budget — additional edge cases."""

    def test_parse_budget_hourly_with_percent(self) -> None:
        """``$15.50/hr`` → (15.5, 15.5) — decimal rate handled."""
        assert UpworkExtractor._parse_upwork_budget("$15.50/hr") == (15.5, 15.5)

    def test_parse_budget_range_three_amounts(self) -> None:
        """``$10-$20-$30`` → (10.0, 20.0) — only first two amounts used."""
        assert UpworkExtractor._parse_upwork_budget("$10-$20-$30") == (10.0, 20.0)

    def test_parse_budget_no_dollar_sign(self) -> None:
        """``$0`` → (None, 0.0) — zero budget."""
        assert UpworkExtractor._parse_upwork_budget("$0") == (None, 0.0)

    def test_parse_budget_only_text(self) -> None:
        """Text with no dollar amounts → (None, None)."""
        assert UpworkExtractor._parse_upwork_budget("Negotiable") == (None, None)

    def test_parse_budget_whitespace(self) -> None:
        """Whitespace-only → (None, None)."""
        assert UpworkExtractor._parse_upwork_budget("   ") == (None, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Upwork — job ID extraction edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpworkJobIdEdgeCases:
    """UpworkExtractor._extract_upwork_job_id — additional edge cases."""

    def test_extract_job_id_empty_string(self) -> None:
        """Empty string falls back to hash."""
        result = UpworkExtractor._extract_upwork_job_id("")
        assert len(result) == 12

    def test_extract_job_id_md5_deterministic(self) -> None:
        """Fallback hash is deterministic for the same input."""
        result1 = UpworkExtractor._extract_upwork_job_id("custom-text")
        result2 = UpworkExtractor._extract_upwork_job_id("custom-text")
        assert result1 == result2


# ═══════════════════════════════════════════════════════════════════════════════
# Freelancer — element helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestFreelancerGetElHref:
    """FreelancerExtractor._get_el_href — relative URL handling."""

    @pytest.mark.asyncio
    async def test_freelancer_get_el_href_relative(self) -> None:
        """Relative href is prefixed with freelancer.com domain."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/projects/12345")
        card.query_selector = AsyncMock(return_value=el)

        result = await FreelancerExtractor._get_el_href(card, "a.title")
        assert result == "https://www.freelancer.com/projects/12345"

    @pytest.mark.asyncio
    async def test_freelancer_get_el_href_exception(self) -> None:
        """Exception during get_attribute returns None."""
        from unittest.mock import AsyncMock

        card = AsyncMock()
        el = AsyncMock()
        el.get_attribute = AsyncMock(side_effect=Exception("boom"))
        card.query_selector = AsyncMock(return_value=el)

        result = await FreelancerExtractor._get_el_href(card, "a.title")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Base class — deeper coverage of _type_like_human, _detect_login_redirect,
# _detect_captcha, etc. (needs browser with page configured)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaseDetectHelpers:
    """BasePlatformExtractor detection helpers with mocked pages."""

    @pytest.mark.asyncio
    async def test_type_like_human_with_text(
        self,
        mock_browser: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_type_like_human with non-empty text calls type_text on the browser."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        extractor = UpworkExtractor(browser=mock_browser)
        await extractor._type_like_human("input#email", "user@test.com")

        mock_browser.type_text.assert_awaited_once_with(
            "input#email",
            "user@test.com",
            delay_range=(0.04, 0.18),
            clear_first=True,
        )

    @pytest.mark.asyncio
    async def test_detect_login_redirect_same_url(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When current URL matches original, no redirect is detected."""
        page_mock = AsyncMock()
        page_mock.url = "https://www.upwork.com/nx/find-work/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_login_redirect(
            "https://www.upwork.com/nx/find-work/?ref=test"
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_detect_login_redirect_to_login(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When current URL contains a login indicator, redirect is detected."""
        page_mock = AsyncMock()
        page_mock.url = "https://www.upwork.com/ab/account-security/login"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_login_redirect("https://www.upwork.com/nx/find-work/")

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_login_redirect_exception(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When page.url access raises, redirect detection returns False."""
        page_mock = AsyncMock()
        page_mock.url = None  # Will cause .lower() to raise
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_login_redirect("https://www.upwork.com/nx/find-work/")

        assert result is False

    @pytest.mark.asyncio
    async def test_detect_captcha_content_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When page content contains a CAPTCHA indicator, returns True."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(
            return_value="<html><body>Please verify you are human</body></html>"
        )
        page_mock.url = "https://www.upwork.com/search/jobs/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_captcha()

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_captcha_url_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When the page URL contains a CAPTCHA indicator, returns True."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(return_value="<html><body>Normal page</body></html>")
        page_mock.url = "https://www.upwork.com/challenge?type=recaptcha"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_captcha()

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_captcha_no_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When no CAPTCHA indicators are found, returns False."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(
            return_value="<html><body>Normal job listing page</body></html>"
        )
        page_mock.url = "https://www.upwork.com/search/jobs/?q=python"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_captcha()

        assert result is False

    @pytest.mark.asyncio
    async def test_detect_captcha_exception(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When page.content() raises, returns False."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(side_effect=Exception("boom"))
        page_mock.url = "https://www.upwork.com/search/jobs/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = UpworkExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_captcha()

        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# LinkedIn — security challenge detection and additional edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestLinkedInSecurityChallenge:
    """LinkedInExtractor._detect_security_challenge with mocked pages."""

    @pytest.mark.asyncio
    async def test_detect_challenge_content_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When page content contains a security challenge indicator, returns True."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(
            return_value="Let's do a quick security check before continuing"
        )
        page_mock.url = "https://www.linkedin.com/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = LinkedInExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_security_challenge()

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_challenge_url_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When the page URL contains challenge/checkpoint, returns True."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(return_value="<html><body>Normal page</body></html>")
        page_mock.url = "https://www.linkedin.com/checkpoint/challenge/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = LinkedInExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_security_challenge()

        assert result is True

    @pytest.mark.asyncio
    async def test_detect_challenge_no_match(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When no challenge indicators are found, returns False."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(return_value="<html><body>Normal feed page</body></html>")
        page_mock.url = "https://www.linkedin.com/feed/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = LinkedInExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_security_challenge()

        assert result is False

    @pytest.mark.asyncio
    async def test_detect_challenge_exception(
        self,
        mock_browser: AsyncMock,
    ) -> None:
        """When page.content() raises, returns False."""
        page_mock = AsyncMock()
        page_mock.content = AsyncMock(side_effect=Exception("boom"))
        page_mock.url = "https://www.linkedin.com/"
        browser_mock = AsyncMock()
        browser_mock.page = page_mock
        extractor = LinkedInExtractor(browser=mock_browser)
        extractor._browser = browser_mock  # type: ignore[assignment]

        result = await extractor._detect_security_challenge()

        assert result is False


class TestLinkedInJobIdFromUrlParam:
    """LinkedInExtractor._extract_linkedin_job_id — URL param extraction."""

    def test_extract_id_from_url_param_no_card(self) -> None:
        """currentJobId param is extracted when card has no data-job-id."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/search/?currentJobId=44444444",
            card,
        )
        assert result == "44444444"

    def test_extract_id_from_url_with_query_stripped(self) -> None:
        """URL path /jobs/view/ is matched even with trailing query params."""
        from unittest.mock import Mock

        card = Mock()
        card.get_attribute.return_value = None
        result = LinkedInExtractor._extract_linkedin_job_id(
            "https://www.linkedin.com/jobs/view/33333/?ref=search",
            card,
        )
        assert result == "33333"


class TestLinkedInSlowScroll:
    """LinkedInExtractor._slow_scroll with mocked browser."""

    @pytest.mark.asyncio
    async def test_slow_scroll(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_slow_scroll performs scrolls without error."""
        sleeps: list[float] = []

        async def _mock_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _mock_sleep)

        browser_mock = AsyncMock()
        browser_mock.page = AsyncMock()
        extractor = LinkedInExtractor(browser=browser_mock)

        # Should execute without raising.
        await extractor._slow_scroll()

        # Browser.scroll should have been called at least once.
        assert browser_mock.scroll.call_count >= 1
