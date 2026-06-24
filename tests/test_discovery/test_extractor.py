"""Tests for the RawLead dataclass."""

from __future__ import annotations

from freelance_lead_gen.discovery.extractor import RawLead


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
