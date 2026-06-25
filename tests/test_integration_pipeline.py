"""Full pipeline integration tests using a mock LLM server and a real database.

Exercises the complete :class:`LeadGenOrchestrator` pipeline — discovery
(skipped), filtering, personalisation, verification, and HITL review —
with a lightweight mock HTTP server that mimics the OpenAI chat completions
API.  No real API key is required.
"""

from __future__ import annotations

import pytest

from freelance_lead_gen.agents.orchestrator import LeadGenOrchestrator
from freelance_lead_gen.config.settings import Settings
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus
from freelance_lead_gen.storage.repository import OpportunityRepository
from tests.mock_llm_server import MockLLMServer

# ── Sample data ─────────────────────────────────────────────────────────────────


_RICH_DESCRIPTION = """\
We are looking for an experienced senior AI Automation Engineer to build a \
RAG pipeline for our customer support system. This is a remote contract \
position for a long-term project-based engagement.

Requirements:
- 3+ years of Python backend development experience (FastAPI, PostgreSQL)
- Experience with LangChain or LlamaIndex for LLM orchestration
- Familiarity with vector databases (Pinecone, Weaviate) and RAG architectures
- Knowledge of LLM APIs (OpenAI, Anthropic) and fine-tuning techniques
- Docker and Kubernetes experience for cloud deployment on AWS

This is a freelance opportunity in the technology and SaaS sectors.
"""


def _pipeline_opportunity(
    platform_job_id: str,
    title: str,
) -> LeadOpportunity:
    """Create a :class:`LeadOpportunity` tailor-made for pipeline integration tests.

    The description and skills are chosen so that the **rule-based profile**
    matcher produces a score ≥ 50 (POTENTIAL tier), which means the
    opportunity remains qualified even when the mock LLM returns errors
    and the pipeline falls back to rule-only scoring.
    """
    return LeadOpportunity(
        platform="upwork",
        platform_job_id=platform_job_id,
        title=title,
        company="TechCorp",
        description=_RICH_DESCRIPTION,
        skills=[
            "Python",
            "LangChain",
            "RAG",
            "LLM",
            "FastAPI",
            "PostgreSQL",
            "Docker",
            "AWS",
        ],
        budget_min=5000.0,
        budget_max=8000.0,
        currency="USD",
        location="Remote",
        status=LeadStatus.DISCOVERED,
    )


@pytest.fixture
def sample_opportunities() -> list[LeadOpportunity]:
    """Return two pipeline-testable opportunities with strong profile matches."""
    return [
        _pipeline_opportunity("int-happy-1", "AI Automation Engineer for RAG Pipeline"),
        _pipeline_opportunity("int-happy-2", "Senior LangChain Developer for AI Project"),
    ]


def _build_settings(mock_url: str | None = None) -> Settings:
    """Return a :class:`Settings` instance configured for pipeline testing.

    Parameters
    ----------
    mock_url : str or None
        Base URL for the LLM API.  When ``None``, the default is used
        (useful for unreachable-server tests).
    """
    settings = Settings()
    settings.llm.api_key = "test-key"
    if mock_url is not None:
        settings.llm.base_url = mock_url
    settings.llm.model = "mock-model"
    settings.hitl.auto_approve = True  # Bypass human review gate.
    return settings


async def _insert_opportunities(opps: list[LeadOpportunity]) -> None:
    """Insert opportunities into the database so pipeline persist ops succeed."""
    repo = OpportunityRepository()
    for opp in opps:
        await repo.create(opp)


# ═══════════════════════════════════════════════════════════════════════════════
# Happy path — mock LLM responds normally
# ═══════════════════════════════════════════════════════════════════════════════


class TestHappyPath:
    """The mock LLM server responds with valid data; the pipeline completes
    end-to-end without errors."""

    @pytest.mark.asyncio
    async def test_full_pipeline_completes(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """Verify the full pipeline runs end-to-end with a mock LLM server."""
        # Arrange
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        # Act
        report = await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        # Assert — full success.
        assert report.success, f"Pipeline failed: {report.errors}"
        assert report.phases_completed, "No phases were completed"
        assert report.total_errors == 0, f"Unexpected errors: {report.errors}"
        assert mock_llm_server.request_count > 0, "Mock LLM was never called"

    @pytest.mark.asyncio
    async def test_pipeline_produces_qualified_leads(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """Verify the pipeline qualifies, drafts, and verifies opportunities."""
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        assert report.total_discovered >= 2
        assert report.total_qualified >= 1, "No leads qualified"
        assert report.total_drafted >= 1, "No drafts created"
        assert report.total_verified_pass >= 1, "No drafts passed verification"
        assert report.total_verified_fail == 0
        assert report.total_reviewed >= 1, "No drafts were auto-approved"
        assert "filtering" in report.phases_completed
        assert "personalization" in report.phases_completed
        assert "verification" in report.phases_completed

    @pytest.mark.asyncio
    async def test_mock_server_receives_llm_requests(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """Verify the mock server actually received HTTP requests from the LLM client."""
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        before = mock_llm_server.request_count
        await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        # At least one request per opportunity for LLM classification.
        assert mock_llm_server.request_count > before


# ═══════════════════════════════════════════════════════════════════════════════
# Error simulation — LLM returns HTTP errors; pipeline must degrade gracefully
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorRecovery:
    """When the LLM server returns errors, the pipeline should degrade
    gracefully by falling back to rule-based scoring and continuing."""

    @pytest.mark.asyncio
    async def test_handles_rate_limit_error(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """Returning 429 (rate limit) should not crash the pipeline."""
        mock_llm_server.set_error_mode("rate_limit")
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        assert report.success, f"Pipeline failed under rate-limit: {report.errors}"
        # Even without LLM, rule-based scoring should qualify opportunities
        # whose profile match is strong enough.
        assert "filtering" in report.phases_completed
        assert mock_llm_server.request_count > 0, "Mock LLM should have received requests"

    @pytest.mark.asyncio
    async def test_handles_server_error(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """Returning 500 (server error) should not crash the pipeline."""
        mock_llm_server.set_error_mode("server_error")
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        assert report.success, f"Pipeline failed under server-error: {report.errors}"
        assert "filtering" in report.phases_completed

    @pytest.mark.asyncio
    async def test_handles_unreachable_llm(
        self,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """When no server is listening, the pipeline should not crash.

        This test points the LLM client at ``127.0.0.1:1``, where nothing
        listens, to simulate a network-level failure.
        """
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings("http://127.0.0.1:1/v1")
        settings.llm.timeout_seconds = 2  # Fail fast.
        settings.llm.max_retries = 0      # Don't retry — one quick failure.
        orchestrator = LeadGenOrchestrator(settings=settings)

        import asyncio
        report = await asyncio.wait_for(
            orchestrator.run_full_pipeline(
                opportunities=sample_opportunities,
                run_discovery=False,
            ),
            timeout=15,
        )

        assert report.success, f"Pipeline failed when LLM unreachable: {report.errors}"
        assert "filtering" in report.phases_completed


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline-level edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineEdgeCases:
    """Pipeline behaviour at boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_opportunities_list(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
    ) -> None:
        """Passing no opportunities should result in a successful no-op."""
        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        report = await orchestrator.run_full_pipeline(
            opportunities=[],
            run_discovery=False,
        )

        assert report.success
        assert report.total_discovered == 0
        assert report.total_qualified == 0
        assert report.total_drafted == 0
        assert report.total_errors == 0

    @pytest.mark.asyncio
    async def test_configurable_mock_latency(
        self,
        mock_llm_server: MockLLMServer,
        in_memory_db: None,
        sample_opportunities: list[LeadOpportunity],
    ) -> None:
        """With artificial latency configured, the pipeline still completes."""
        mock_llm_server.set_latency(0.01)  # 10 ms per request.
        await _insert_opportunities(sample_opportunities)

        settings = _build_settings(mock_llm_server.base_url)
        orchestrator = LeadGenOrchestrator(settings=settings)

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_opportunities,
            run_discovery=False,
        )

        assert report.success, f"Pipeline failed with latency: {report.errors}"
        assert report.total_qualified >= 1
