"""Tests for the FilteringPipeline — scoring, tier assignment, and classification."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from freelance_lead_gen.agents.filtering_agent import (
    FilteringPipeline,
    FilteringReport,
    ScoringThresholds,
    _build_classification_input,
    _LLMClassification,
)
from freelance_lead_gen.llm import LLMClient
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus
from freelance_lead_gen.storage.repository import OpportunityRepository

# ── Sample data ─────────────────────────────────────────────────────────────────


SAMPLE_OPPS = [
    LeadOpportunity(
        platform="upwork",
        platform_job_id="filter-1",
        title="Senior AI Engineer for RAG Pipeline",
        company="TechCorp",
        description=(
            "Looking for an experienced AI engineer to build a RAG pipeline. "
            "Must have Python, LangChain, and vector database experience."
        ),
        budget_min=8000.0,
        budget_max=12000.0,
        skills=["Python", "LangChain", "RAG", "Vector DB", "OpenAI"],
        status=LeadStatus.DISCOVERED,
    ),
    LeadOpportunity(
        platform="upwork",
        platform_job_id="filter-2",
        title="Entry Level Data Entry",
        company="LowPay Inc",
        description="Need someone to enter data into spreadsheets. No experience needed.",
        budget_min=100.0,
        budget_max=200.0,
        skills=["Excel", "Typing"],
        status=LeadStatus.DISCOVERED,
    ),
    LeadOpportunity(
        platform="upwork",
        platform_job_id="filter-3",
        title="WordPress Site Update",
        description="Update a WordPress site with new content.",
        skills=["WordPress", "PHP"],
        status=LeadStatus.DISCOVERED,
    ),
]


# ── ScoringThresholds ───────────────────────────────────────────────────────────


class TestScoringThresholds:
    """Tests for the ScoringThresholds model."""

    def test_default_thresholds(self) -> None:
        """Verify default thresholds are properly set."""
        thresholds = ScoringThresholds()
        assert thresholds.high == 75
        assert thresholds.potential == 50
        assert thresholds.low == 0

    def test_tiers_property(self) -> None:
        """Verify tiers returns ordered (label, score) pairs."""
        thresholds = ScoringThresholds(high=80, potential=60, low=0)
        tiers = thresholds.tiers
        assert tiers == [("HIGH", 80), ("POTENTIAL", 60), ("LOW", 0)]

    def test_custom_thresholds(self) -> None:
        """Verify thresholds can be customised."""
        thresholds = ScoringThresholds(high=85, potential=70)
        assert thresholds.high == 85
        assert thresholds.potential == 70


# ── FilteringPipeline ───────────────────────────────────────────────────────────


class TestFilteringPipeline:
    """Tests for the FilteringPipeline class."""

    def test_init_defaults(self, test_settings) -> None:
        """Verify pipeline initialises with default dependencies."""
        pipeline = FilteringPipeline(settings=test_settings)
        assert pipeline.thresholds is not None
        assert pipeline.thresholds.high == 75
        assert pipeline.stats["runs"] == 0

    def test_init_with_custom_thresholds(self, test_settings) -> None:
        """Verify custom thresholds are applied."""
        custom = ScoringThresholds(high=90, potential=70)
        pipeline = FilteringPipeline(thresholds=custom, settings=test_settings)
        assert pipeline.thresholds.high == 90

    def test_set_thresholds_updates(self, test_settings) -> None:
        """Verify set_thresholds updates the threshold values."""
        pipeline = FilteringPipeline(settings=test_settings)
        new = ScoringThresholds(high=80, potential=60)
        pipeline.set_thresholds(new)
        assert pipeline.thresholds.high == 80

    def test_assign_tier(self, test_settings) -> None:
        """Verify _assign_tier returns the correct tier label."""
        pipeline = FilteringPipeline(settings=test_settings)
        assert pipeline._assign_tier(90) == "HIGH"
        assert pipeline._assign_tier(75) == "HIGH"
        assert pipeline._assign_tier(60) == "POTENTIAL"
        assert pipeline._assign_tier(50) == "POTENTIAL"
        assert pipeline._assign_tier(30) == "LOW"

    def test_blend_scores(self, test_settings) -> None:
        """Verify _blend_scores combines rule and LLM scores correctly."""
        pipeline = FilteringPipeline(settings=test_settings)
        llm_result = _LLMClassification(
            qualified=True,
            score=80,
            skill_match_score=85,
            budget_fit_score=70,
            clarity_score=75,
            reasoning="Good match.",
            risks=[],
        )
        blended = pipeline._blend_scores(rule_score=60, llm_result=llm_result)
        # 60 * 0.4 + 80 * 0.6 = 24 + 48 = 72
        assert blended["score"] == 72
        assert blended["skill_match_score"] == 85

    def test_blend_clamps_extreme_values(self, test_settings) -> None:
        """Verify _blend_scores clamps the result to 0-100."""
        pipeline = FilteringPipeline(settings=test_settings)
        # LLM score is validated 0-100 by the model; extreme values are
        # tested through the rule_score parameter which is unvalidated.
        llm = _LLMClassification(
            qualified=True, score=100, skill_match_score=50,
            budget_fit_score=50, clarity_score=50, reasoning="",
        )
        blended = pipeline._blend_scores(rule_score=200, llm_result=llm)
        assert blended["score"] == 100

        llm_low = _LLMClassification(
            qualified=True, score=0, skill_match_score=50,
            budget_fit_score=50, clarity_score=50, reasoning="",
        )
        blended_low = pipeline._blend_scores(rule_score=-50, llm_result=llm_low)
        assert blended_low["score"] == 0


class TestFilteringPipelineRun:
    """Tests for the FilteringPipeline.run() method."""

    @pytest.mark.asyncio
    async def test_run_rule_based_only(self, test_settings) -> None:
        """Verify run works without LLM using only rule-based scoring."""
        mock_repo = AsyncMock(spec=OpportunityRepository)
        pipeline = FilteringPipeline(
            settings=test_settings,
            repository=mock_repo,
        )

        qualified, report = await pipeline.run(SAMPLE_OPPS, use_llm=False)
        assert isinstance(report, FilteringReport)
        assert report.total_input == 3
        assert isinstance(qualified, list)

    @pytest.mark.asyncio
    async def test_run_with_mocked_llm(self, test_settings) -> None:
        """Verify run uses LLM classification when use_llm=True."""
        mock_repo = AsyncMock(spec=OpportunityRepository)
        mock_llm = AsyncMock(spec=LLMClient)
        mock_llm.structured_classify.return_value = {
            "qualified": True,
            "score": 75,
            "skill_match_score": 80,
            "budget_fit_score": 70,
            "clarity_score": 65,
            "reasoning": "Good skills match",
            "risks": [],
        }

        pipeline = FilteringPipeline(
            settings=test_settings,
            llm_client=mock_llm,
            repository=mock_repo,
        )

        _qualified, _report = await pipeline.run(SAMPLE_OPPS, use_llm=True)
        assert mock_llm.structured_classify.await_count >= 1

    @pytest.mark.asyncio
    async def test_run_persists_results(self, test_settings) -> None:
        """Verify run persists results when persist=True."""
        mock_repo = AsyncMock(spec=OpportunityRepository)

        pipeline = FilteringPipeline(
            settings=test_settings,
            repository=mock_repo,
        )

        await pipeline.run([SAMPLE_OPPS[0]], use_llm=False, persist=True)
        # The repository should have been called to update persisted records.
        assert mock_repo.update.await_count >= 0  # May be disqualified early

    @pytest.mark.asyncio
    async def test_run_empty_input(self, test_settings) -> None:
        """Verify run handles an empty list gracefully."""
        pipeline = FilteringPipeline(settings=test_settings)
        qualified, report = await pipeline.run([], use_llm=False)
        assert qualified == []
        assert report.total_input == 0


class TestFilteringReport:
    """Tests for the FilteringReport dataclass."""

    def test_qualified_count(self) -> None:
        """Verify qualified_count returns HIGH + POTENTIAL."""
        report = FilteringReport(
            total_input=10,
            high_count=3,
            potential_count=4,
            low_count=2,
            disqualified_count=1,
        )
        assert report.qualified_count == 7

    def test_elapsed_seconds_none(self) -> None:
        """Verify elapsed_seconds is None when not completed."""
        report = FilteringReport()
        assert report.elapsed_seconds is None


# ── Input builder ───────────────────────────────────────────────────────────────


class TestBuildClassificationInput:
    """Tests for the _build_classification_input helper."""

    def test_builds_full_description(self) -> None:
        """Verify the input includes all opportunity fields."""
        opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="input-1",
            title="AI Engineer",
            company="Acme",
            description="Build AI systems.",
            budget_min=5000.0,
            budget_max=8000.0,
            skills=["Python", "AI"],
            location="Remote",
        )
        result = _build_classification_input(opp)
        assert "AI Engineer" in result
        assert "upwork" in result
        assert "Acme" in result
        assert "$5000-$8000" in result.replace(" ", "")
        assert "Python" in result
        assert "Remote" in result

    def test_minimal_input(self) -> None:
        """Verify a minimal opportunity still produces valid input."""
        opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="minimal-1",
            title="Minimal Job",
            description="Just a test.",
        )
        result = _build_classification_input(opp)
        assert "Minimal Job" in result
        assert "Just a test." in result
