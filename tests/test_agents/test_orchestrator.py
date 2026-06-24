"""Tests for the LeadGenOrchestrator pipeline lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from freelance_lead_gen.agents.filtering_agent import FilteringPipeline, FilteringReport
from freelance_lead_gen.agents.orchestrator import (
    LeadGenOrchestrator,
    OrchestratorReport,
    PipelinePhase,
)
from freelance_lead_gen.agents.personalization_agent import PersonalizationAgent
from freelance_lead_gen.agents.profile_matcher import TargetProfile
from freelance_lead_gen.agents.verification_agent import (
    VerificationAgent,
    VerificationResult,
)
from freelance_lead_gen.discovery.discovery_agent import DiscoveryAgent, DiscoveryCycleReport
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus, OutboundDraft
from freelance_lead_gen.storage.repository import OpportunityRepository


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_qualified_opps() -> list[LeadOpportunity]:
    """Return two qualified opportunities for pipeline testing."""
    return [
        LeadOpportunity(
            platform="upwork",
            platform_job_id="pipe-1",
            title="Python AI Engineer",
            description="Build AI solutions.",
            status=LeadStatus.DISCOVERED,
        ),
        LeadOpportunity(
            platform="upwork",
            platform_job_id="pipe-2",
            title="RAG Pipeline Developer",
            description="Implement RAG.",
            status=LeadStatus.DISCOVERED,
        ),
    ]


@pytest.fixture
def mock_all_agents() -> dict:
    """Return a dict of fully mocked pipeline agents."""
    mock_discovery = AsyncMock(spec=DiscoveryAgent)
    mock_discovery.run_discovery_cycle.return_value = DiscoveryCycleReport(
        total_new=2,
        total_found=2,
        total_errors=0,
        platforms_attempted=1,
        platforms_succeeded=1,
        per_platform={"upwork": {"found": 2, "new": 2, "failed": 0, "searched": 1}},
    )

    mock_filtering = AsyncMock(spec=FilteringPipeline)
    mock_filtering.run.return_value = (
        [],
        FilteringReport(),
    )

    mock_personalization = AsyncMock(spec=PersonalizationAgent)

    mock_verification = AsyncMock(spec=VerificationAgent)
    mock_verification.verify.return_value = VerificationResult(
        passed=True, score=85, word_count=50, paragraph_count=3,
    )

    mock_repo = AsyncMock(spec=OpportunityRepository)

    mock_llm = AsyncMock()

    return {
        "discovery": mock_discovery,
        "filtering": mock_filtering,
        "personalization": mock_personalization,
        "verification": mock_verification,
        "repository": mock_repo,
        "llm": mock_llm,
    }


# ── Tests ───────────────────────────────────────────────────────────────────────


class TestOrchestratorInit:
    """Tests for LeadGenOrchestrator initialisation."""

    def test_create_with_defaults(self) -> None:
        """Verify the orchestrator can be created with default dependencies."""
        orchestrator = LeadGenOrchestrator()
        assert orchestrator.is_running is False
        assert orchestrator.shutdown_requested is False
        assert orchestrator.stats["runs"] == 0

    def test_create_with_test_settings(self, test_settings) -> None:  # noqa: ANN001
        """Verify the orchestrator accepts a custom settings object."""
        orchestrator = LeadGenOrchestrator(settings=test_settings)
        assert orchestrator._settings is test_settings


class TestOrchestratorPipeline:
    """Tests for the full pipeline lifecycle."""

    @pytest.mark.asyncio
    async def test_empty_discovery(self, mock_all_agents: dict) -> None:
        """Verify pipeline completes successfully when discovery finds nothing."""
        mock_all_agents["repository"].search.return_value = []
        mock_all_agents["discovery"].run_discovery_cycle.return_value = DiscoveryCycleReport(
            total_new=0,
            total_found=0,
            total_errors=0,
            platforms_attempted=1,
            platforms_succeeded=1,
            per_platform={},
        )

        orchestrator = LeadGenOrchestrator(
            discovery_agent=mock_all_agents["discovery"],
            filtering_pipeline=mock_all_agents["filtering"],
            personalization_agent=mock_all_agents["personalization"],
            verification_agent=mock_all_agents["verification"],
            repository=mock_all_agents["repository"],
            llm_client=mock_all_agents["llm"],
        )

        report = await orchestrator.run_full_pipeline()

        assert report.success is True
        assert report.total_discovered == 0
        assert report.total_qualified == 0
        assert report.total_drafted == 0

    @pytest.mark.asyncio
    async def test_full_pipeline_with_pre_discovered(
        self,
        mock_all_agents: dict,
        sample_qualified_opps: list[LeadOpportunity],
    ) -> None:
        """Verify the pipeline processes pre-discovered opportunities end-to-end."""
        mock_all_agents["repository"].search.return_value = sample_qualified_opps
        mock_all_agents["filtering"].run.return_value = (
            sample_qualified_opps,
            FilteringReport(
                total_input=2,
                high_count=1,
                potential_count=1,
                low_count=0,
                errors=0,
            ),
        )

        def _make_draft(opp: LeadOpportunity, *args, **kwargs) -> OutboundDraft:
            draft = OutboundDraft(opportunity_id=opp.id)
            draft.add_version(f"Draft for {opp.title}")
            return draft

        mock_all_agents["personalization"].generate_draft.side_effect = _make_draft

        orchestrator = LeadGenOrchestrator(
            discovery_agent=mock_all_agents["discovery"],
            filtering_pipeline=mock_all_agents["filtering"],
            personalization_agent=mock_all_agents["personalization"],
            verification_agent=mock_all_agents["verification"],
            repository=mock_all_agents["repository"],
            llm_client=mock_all_agents["llm"],
        )

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_qualified_opps,
        )

        assert report.success is True
        assert report.total_drafted == 2
        assert report.total_verified_pass >= 1

        # Verify the discovery phase was still called.
        mock_all_agents["discovery"].run_discovery_cycle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_filtering_phase(self, sample_qualified_opps: list[LeadOpportunity]) -> None:
        """Verify pipeline works when filtering is skipped."""
        mock_discovery = AsyncMock(spec=DiscoveryAgent)
        mock_repo = AsyncMock(spec=OpportunityRepository)
        mock_llm = AsyncMock()

        orchestrator = LeadGenOrchestrator(
            discovery_agent=mock_discovery,
            repository=mock_repo,
            llm_client=mock_llm,
        )

        report = await orchestrator.run_full_pipeline(
            opportunities=sample_qualified_opps,
            run_discovery=False,
        )

        assert report.success is True
        # Without discovery or filtering, the opportunities flow through
        # to drafting only if filtering is skipped.
        assert report.phases_completed is not None


class TestOrchestratorPhaseIsolation:
    """Tests for running individual pipeline phases."""

    @pytest.mark.asyncio
    async def test_run_unknown_phase(self) -> None:
        """Verify run_phase raises for an unknown phase name."""
        orchestrator = LeadGenOrchestrator()
        with pytest.raises(ValueError, match="Unknown phase"):
            await orchestrator.run_phase("nonexistent")

    @pytest.mark.asyncio
    async def test_run_filtering_phase_no_opportunities(self) -> None:
        """Verify run_phase raises when opportunities are missing for filtering."""
        orchestrator = LeadGenOrchestrator()
        with pytest.raises(ValueError, match="opportunities are required"):
            await orchestrator.run_phase("filtering")


class TestOrchestratorReport:
    """Tests for the OrchestratorReport dataclass."""

    def test_elapsed_seconds_none_when_not_completed(self) -> None:
        """Verify elapsed_seconds is None when completed_at is not set."""
        report = OrchestratorReport()
        assert report.elapsed_seconds is None

    def test_summary_keys(self) -> None:
        """Verify summary returns the expected keys."""
        report = OrchestratorReport(success=True)
        summary = report.summary
        assert summary["success"] is True
        assert "phases_completed" in summary
        assert "elapsed_seconds" in summary
        assert summary["elapsed_seconds"] is None


class TestOrchestratorShutdown:
    """Tests for orchestrator graceful shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_sets_event(self) -> None:
        """Verify shutdown() sets the shutdown event."""
        orchestrator = LeadGenOrchestrator()
        assert orchestrator.shutdown_requested is False
        await orchestrator.shutdown()
        assert orchestrator.shutdown_requested is True

    @pytest.mark.asyncio
    async def test_double_run_prevented(self) -> None:
        """Verify running the pipeline twice concurrently raises."""
        mock_repo = AsyncMock(spec=OpportunityRepository)
        mock_llm = AsyncMock()

        orchestrator = LeadGenOrchestrator(
            repository=mock_repo,
            llm_client=mock_llm,
        )

        # Simulate a running pipeline.
        orchestrator._is_running = True

        with pytest.raises(RuntimeError, match="already running"):
            await orchestrator.run_full_pipeline(run_discovery=False)
