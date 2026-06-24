"""Tests for ProfileMatcher scoring and matching logic."""
from __future__ import annotations

import pytest

from freelance_lead_gen.agents.profile_matcher import ProfileMatcher, TargetProfile
from freelance_lead_gen.models.opportunity import LeadOpportunity

# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def profile() -> TargetProfile:
    """Return a target profile with specific skills, industries, and budget.

    Uses a narrow skill set so match/no-match boundaries are clear, and
    a defined budget range so budget-fit tests are deterministic.
    """
    return TargetProfile(
        skills=["Python", "RAG", "LangChain", "LLM", "Vector Database"],
        industries=["Technology", "Finance"],
        budget_range=(5000, 15000),
    )


# ── Tests ───────────────────────────────────────────────────────────────────────


def test_empty_skills_scores_zero(profile: TargetProfile) -> None:
    """Verify an opportunity with empty skills scores zero for skill match.

    When no skills are listed the matcher should not find any matches
    against the profile, producing a *skill_match_score* of 0.
    """
    opp = LeadOpportunity(
        platform="upwork",
        platform_job_id="empty-1",
        title="Test",
        description="No skills listed",
        skills=[],
        budget_min=10000,
        budget_max=12000,
    )
    matcher = ProfileMatcher(profile=profile)
    result = matcher.score_opportunity(opp)

    assert result["skill_match_score"] == 0


def test_budget_below_minimum_reduces_score(profile: TargetProfile) -> None:
    """Verify budget below the profile's minimum produces a low budget-fit score.

    The opportunity's budget range (1000 – 2000) does not overlap with the
    profile's target range (5000 – 15000), so *budget_fit_score* should be
    at or near 0.
    """
    opp = LeadOpportunity(
        platform="upwork",
        platform_job_id="budget-1",
        title="Test",
        description="Low budget",
        skills=["Python"],
        budget_min=1000,
        budget_max=2000,
    )
    matcher = ProfileMatcher(profile=profile)
    result = matcher.score_opportunity(opp)

    assert result["budget_fit_score"] <= 30


def test_perfect_skill_match_scores_high(profile: TargetProfile) -> None:
    """Verify a perfect skill match yields a high score.

    When all profile skills are present in the opportunity, the matcher
    should return *skill_match_score* >= 90, and the composite *overall_score*
    should reflect this strongly.
    """
    opp = LeadOpportunity(
        platform="upwork",
        platform_job_id="perfect-1",
        title="Test",
        description="Perfect match",
        skills=["Python", "RAG", "LangChain", "LLM", "Vector Database"],
        budget_min=10000,
        budget_max=12000,
    )
    matcher = ProfileMatcher(profile=profile)
    result = matcher.score_opportunity(opp)

    assert result["skill_match_score"] >= 90
    assert result["overall_score"] >= 60
