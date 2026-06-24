"""Tests for the extraction pipeline — RawLead, GenericPlaywrightExtractor, and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from freelance_lead_gen.discovery.extractor import (
    GenericPlaywrightExtractor,
    RawLead,
)

# ── RawLead ─────────────────────────────────────────────────────────────────────


class TestRawLead:
    """Tests for the RawLead dataclass."""

    def test_create_minimal(self) -> None:
        """Verify a minimal RawLead can be created."""
        lead = RawLead(
            platform="upwork",
            platform_job_id="job-001",
            title="AI Engineer Needed",
        )
        assert lead.platform == "upwork"
        assert lead.platform_job_id == "job-001"
        assert lead.title == "AI Engineer Needed"
        assert lead.description == ""
        assert lead.company is None
        assert lead.skills == []
        assert lead.currency == "USD"

    def test_create_full(self) -> None:
        """Verify a fully-populated RawLead has all fields."""
        lead = RawLead(
            platform="linkedin",
            platform_job_id="LI-12345",
            title="Senior Backend Engineer",
            company="Acme Corp",
            description="We need a backend expert.",
            url="https://linkedin.com/jobs/12345",
            posted_date="2026-06-24T10:00:00Z",
            budget_min=100.0,
            budget_max=150.0,
            skills=["Python", "FastAPI"],
            location="Remote",
            raw_html="<div>job card</div>",
        )
        assert lead.company == "Acme Corp"
        assert lead.budget_min == 100.0
        assert lead.extracted_at is not None

    def test_default_extracted_at(self) -> None:
        """Verify extracted_at defaults to ISO-format timestamp."""
        lead = RawLead(
            platform="upwork",
            platform_job_id="j-1",
            title="Test",
        )
        assert "T" in lead.extracted_at  # ISO format contains 'T'


# ── Extractor configuration ─────────────────────────────────────────────────────


class TestGenericPlaywrightExtractorConfig:
    """Tests for GenericPlaywrightExtractor construction."""

    def test_minimal_config(self, mock_browser: AsyncMock) -> None:
        """Verify the extractor can be created with minimal selectors."""
        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2.title a",
            url_selector="h2.title a",
            platform_name="test_platform",
        )
        assert extractor._platform_name == "test_platform"
        assert extractor._max_results == 25
        assert extractor._paginate is True

    def test_full_config(self, mock_browser: AsyncMock) -> None:
        """Verify all constructor parameters are stored correctly."""
        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".listing",
            title_selector="h3 a",
            url_selector="h3 a",
            description_selector=".desc",
            company_selector=".company",
            budget_selector=".rate",
            posted_date_selector=".date",
            extra_selectors={"location": ".loc"},
            max_results=10,
            paginate=False,
            next_page_selector="a.next",
            platform_name="custom",
        )
        assert extractor._max_results == 10
        assert extractor._paginate is False
        assert extractor._description_selector == ".desc"
        assert extractor._extra_selectors == {"location": ".loc"}


# ── Captcha and login detection ─────────────────────────────────────────────────


class TestDetectionHelpers:
    """Tests for CAPTCHA and login redirect detection."""

    @pytest.mark.asyncio
    async def test_captcha_detected_in_url(self, mock_browser: AsyncMock) -> None:
        """Verify CAPTCHA is detected when indicator is in the URL."""
        mock_browser.page.content = AsyncMock(return_value="<html>normal page</html>")
        mock_browser.page.url = "https://example.com/challenge-platform/hc"

        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2 a",
            url_selector="h2 a",
        )
        detected = await extractor._detect_captcha()
        assert detected is True

    @pytest.mark.asyncio
    async def test_captcha_not_detected(self, mock_browser: AsyncMock) -> None:
        """Verify normal pages do not trigger CAPTCHA detection."""
        mock_browser.page.content = AsyncMock(return_value="<html>normal page content</html>")
        mock_browser.page.url = "https://example.com/search?q=python"

        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2 a",
            url_selector="h2 a",
        )
        detected = await extractor._detect_captcha()
        assert detected is False

    @pytest.mark.asyncio
    async def test_login_redirect_detected(self, mock_browser: AsyncMock) -> None:
        """Verify login redirect is detected."""
        mock_browser.page.url = "https://example.com/login?redirect=/search"
        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2 a",
            url_selector="h2 a",
        )
        detected = await extractor._detect_login_redirect(
            "https://example.com/search?q=python"
        )
        assert detected is True

    @pytest.mark.asyncio
    async def test_login_redirect_not_detected(self, mock_browser: AsyncMock) -> None:
        """Verify same-page URLs do not trigger login detection."""
        mock_browser.page.url = "https://example.com/search?q=python"

        extractor = GenericPlaywrightExtractor(
            browser=mock_browser,  # type: ignore[arg-type]
            search_url_template="https://example.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2 a",
            url_selector="h2 a",
        )
        detected = await extractor._detect_login_redirect(
            "https://example.com/search?q=python"
        )
        assert detected is False


# ── Budget parsing ──────────────────────────────────────────────────────────────


class TestBudgetParsing:
    """Tests for the static budget parsing utility."""

    def test_parse_range(self) -> None:
        """Verify parsing '$30-$50' returns (30.0, 50.0)."""
        result = GenericPlaywrightExtractor._parse_budget("$30-$50")
        assert result == (30.0, 50.0)

    def test_parse_single(self) -> None:
        """Verify parsing '$500' returns (500.0, None)."""
        result = GenericPlaywrightExtractor._parse_budget("$500")
        assert result == (500.0, None)

    def test_parse_with_hr_suffix(self) -> None:
        """Verify parsing '$30-$50/hr' works."""
        result = GenericPlaywrightExtractor._parse_budget("$30-$50/hr")
        assert result == (30.0, 50.0)

    def test_parse_empty(self) -> None:
        """Verify parsing empty text returns (None, None)."""
        result = GenericPlaywrightExtractor._parse_budget("")
        assert result == (None, None)

    def test_parse_no_match(self) -> None:
        """Verify parsing text without dollar amounts returns (None, None)."""
        result = GenericPlaywrightExtractor._parse_budget("Competitive rate")
        assert result == (None, None)


# ── Job ID extraction ───────────────────────────────────────────────────────────


class TestJobIdExtraction:
    """Tests for the static job ID extraction utility."""

    def test_from_url_generates_hash(self) -> None:
        """Verify a hash is generated from a URL."""
        card = MagicMock()
        card.get_attribute.return_value = None
        job_id = GenericPlaywrightExtractor._extract_job_id(
            "https://example.com/jobs/abc-123", card
        )
        assert job_id is not None
        assert len(job_id) == 12

    def test_from_data_attribute(self) -> None:
        """Verify data-job-id attribute takes priority."""
        card = MagicMock()
        card.get_attribute.side_effect = {
            "data-job-id": "custom-id-456",
        }.get

        job_id = GenericPlaywrightExtractor._extract_job_id(
            "https://example.com/jobs/abc-123", card
        )
        assert job_id == "custom-id-456"

    def test_fallback_for_no_url(self) -> None:
        """Verify a fallback hash is generated when no URL is available."""
        card = MagicMock()
        card.get_attribute.return_value = None
        job_id = GenericPlaywrightExtractor._extract_job_id("", card)
        assert job_id is not None
        assert len(job_id) == 12
