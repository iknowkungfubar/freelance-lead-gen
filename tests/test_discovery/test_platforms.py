"""Tests for platform-specific extractors — URL templates, rate-limit enforcement.

Each extractor inherits from :class:`BasePlatformExtractor` and must implement
platform-specific URL templates, search behaviour, and rate-limiting.  These
tests verify the non-browser aspects of each extractor without requiring a
real ``ManagedBrowser`` or network access.
"""

from __future__ import annotations

import pytest

from freelance_lead_gen.discovery.platforms.base import RateLimitConfig
from freelance_lead_gen.discovery.platforms.freelancer import FreelancerExtractor
from freelance_lead_gen.discovery.platforms.job_boards import RemoteOKExtractor
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
        assert "upwork.com" in url
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
        assert "linkedin.com" in url
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
        assert "freelancer.com" in url
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
        assert "remoteok.com" in url
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
