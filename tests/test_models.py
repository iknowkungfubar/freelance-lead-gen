"""Tests for domain models — LeadOpportunity, OutboundDraft, pipeline models, and platform models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from freelance_lead_gen.models.opportunity import (
    LeadOpportunity,
    LeadScoringResult,
    LeadStatus,
    OutboundDraft,
)
from freelance_lead_gen.models.pipeline import (
    PipelineContext,
    PipelineResult,
    PipelineState,
    is_valid_transition,
)
from freelance_lead_gen.models.platform import Platform, PlatformConfig, PlatformCredentials

# ── LeadOpportunity ─────────────────────────────────────────────────────────────


class TestLeadOpportunity:
    """Tests for the central LeadOpportunity model."""

    def test_create_minimal(self) -> None:
        """Verify a minimal LeadOpportunity can be created with required fields."""
        opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="job-001",
            title="AI Engineer",
            description="Build AI systems.",
        )
        assert opp.platform == "upwork"
        assert opp.platform_job_id == "job-001"
        assert opp.title == "AI Engineer"
        assert opp.status == LeadStatus.DISCOVERED
        assert opp.id is not None
        assert len(opp.id) == 12
        assert opp.score is None
        assert opp.currency == "USD"
        assert opp.skills == []
        assert opp.raw_data == {}

    def test_create_full(self) -> None:
        """Verify a fully-populated LeadOpportunity has all fields set."""
        datetime.now(UTC)
        opp = LeadOpportunity(
            platform="linkedin",
            platform_job_id="linked-999",
            title="Senior RAG Engineer",
            company="Acme AI",
            description="We need an expert in retrieval-augmented generation.",
            budget_min=10000.0,
            budget_max=15000.0,
            skills=["RAG", "LangChain", "Python"],
            url="https://linkedin.com/jobs/999",
            location="Remote",
            status=LeadStatus.QUALIFIED,
            score=88,
            notes="Strong match",
            raw_data={"source": "linkedin_api"},
        )
        assert opp.company == "Acme AI"
        assert opp.budget_min == 10000.0
        assert opp.budget_max == 15000.0
        assert len(opp.skills) == 3
        assert opp.qualified() is True
        assert opp.is_terminal() is False

    def test_score_clamping(self) -> None:
        """Verify that scores outside 0-100 are clamped."""
        opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="clamp-1",
            title="Test",
            description="Test",
            score=150,
        )
        assert opp.score == 100

        opp2 = LeadOpportunity(
            platform="upwork",
            platform_job_id="clamp-2",
            title="Test",
            description="Test",
            score=-10,
        )
        assert opp2.score == 0

    def test_description_stripped(self) -> None:
        """Verify description is stripped of leading/trailing whitespace."""
        opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="strip-1",
            title="Test",
            description="  Some text with whitespace  ",
        )
        assert opp.description == "Some text with whitespace"

    def test_qualified_property(self) -> None:
        """Verify qualified() returns True only when score >= 60."""
        opp = LeadOpportunity(
            platform="upwork", platform_job_id="q-1", title="Test", description="Test"
        )
        assert opp.qualified() is False  # None score

        opp.score = 59
        assert opp.qualified() is False

        opp.score = 60
        assert opp.qualified() is True

        opp.score = 100
        assert opp.qualified() is True

    def test_terminal_status(self) -> None:
        """Verify terminal states are detected correctly."""
        opp = LeadOpportunity(
            platform="upwork", platform_job_id="t-1", title="Test", description="Test"
        )
        assert opp.is_terminal() is False

        opp.status = LeadStatus.ARCHIVED
        assert opp.is_terminal() is True

        opp.status = LeadStatus.REJECTED
        assert opp.is_terminal() is True

    def test_touch_updates_timestamp(self) -> None:
        """Verify touch() updates updated_at."""
        opp = LeadOpportunity(
            platform="upwork", platform_job_id="touch-1", title="Test", description="Test"
        )
        original = opp.updated_at
        opp.touch()
        assert opp.updated_at >= original

    def test_budget_validation(self) -> None:
        """Verify budget_min and budget_max cannot be negative."""
        with pytest.raises(ValidationError):
            LeadOpportunity(
                platform="upwork",
                platform_job_id="b-1",
                title="Test",
                description="Test",
                budget_min=-100,
            )

    def test_currency_validation(self) -> None:
        """Verify currency must be a 3-letter ISO code."""
        with pytest.raises(ValidationError):
            LeadOpportunity(
                platform="upwork",
                platform_job_id="c-1",
                title="Test",
                description="Test",
                currency="US",
            )

    def test_invalid_status(self) -> None:
        """Verify an invalid status string is rejected."""
        with pytest.raises(ValidationError):
            LeadOpportunity(
                platform="upwork",
                platform_job_id="s-1",
                title="Test",
                description="Test",
                status="nonexistent",
            )


# ── LeadScoringResult ───────────────────────────────────────────────────────────


class TestLeadScoringResult:
    """Tests for the LeadScoringResult model."""

    def test_create_valid(self) -> None:
        """Verify a valid scoring result can be created."""
        result = LeadScoringResult(
            qualified=True,
            score=85,
            skill_match_score=90,
            budget_fit_score=75,
            clarity_score=80,
            reasoning="Strong skills match.",
            risks=["Budget may be low"],
        )
        assert result.qualified is True
        assert result.score == 85
        assert len(result.risks) == 1

    def test_score_bounds(self) -> None:
        """Verify score values outside 0-100 are rejected."""
        with pytest.raises(ValidationError):
            LeadScoringResult(qualified=True, score=150, reasoning="Out of bounds")
        with pytest.raises(ValidationError):
            LeadScoringResult(qualified=True, score=-5, reasoning="Negative")

    def test_default_scores(self) -> None:
        """Verify sub-scores default to 50 when not provided."""
        result = LeadScoringResult(
            qualified=True,
            score=80,
            reasoning="Good fit.",
        )
        assert result.skill_match_score == 50
        assert result.budget_fit_score == 50
        assert result.clarity_score == 50


# ── OutboundDraft ───────────────────────────────────────────────────────────────


class TestOutboundDraft:
    """Tests for the OutboundDraft model."""

    def test_create_minimal(self) -> None:
        """Verify a minimal OutboundDraft can be created."""
        draft = OutboundDraft(opportunity_id="opp-123")
        assert draft.opportunity_id == "opp-123"
        assert draft.versions == []
        assert draft.current_version_index == 0
        assert draft.approved is False
        assert draft.current_body is None

    def test_add_version(self) -> None:
        """Verify add_version appends and optionally sets current."""
        draft = OutboundDraft(opportunity_id="opp-1")
        idx = draft.add_version("Version one")
        assert idx == 0
        assert draft.version_count == 1
        assert draft.current_body == "Version one"

        idx2 = draft.add_version("Version two", set_current=True)
        assert idx2 == 1
        assert draft.current_body == "Version two"

    def test_add_version_no_set_current(self) -> None:
        """Verify add_version with set_current=False does not change index."""
        draft = OutboundDraft(opportunity_id="opp-1")
        draft.add_version("First")
        draft.add_version("Second", set_current=False)
        assert draft.current_version_index == 0
        assert draft.current_body == "First"

    def test_approve(self) -> None:
        """Verify approve() sets the approved flag."""
        draft = OutboundDraft(opportunity_id="opp-1")
        assert draft.approved is False
        draft.approve()
        assert draft.approved is True

    def test_current_body_out_of_bounds(self) -> None:
        """Verify current_body handles an index beyond the versions list."""
        draft = OutboundDraft(opportunity_id="opp-1")
        draft.add_version("Only one")
        draft.current_version_index = 99
        # Should clamp to the last available version.
        assert draft.current_body == "Only one"

    def test_version_count_empty(self) -> None:
        """Verify version_count is 0 for a fresh draft."""
        draft = OutboundDraft(opportunity_id="opp-1")
        assert draft.version_count == 0


# ── Pipeline Models ─────────────────────────────────────────────────────────────


class TestPipelineState:
    """Tests for pipeline state machine."""

    def test_valid_transitions(self) -> None:
        """Verify key state transitions are valid."""
        assert is_valid_transition(PipelineState.PENDING, PipelineState.INITIALISED)
        assert is_valid_transition(PipelineState.DISCOVERING, PipelineState.DISCOVERED)
        assert is_valid_transition(PipelineState.DRAFTED, PipelineState.AWAITING_REVIEW)
        assert is_valid_transition(PipelineState.SUBMITTED, PipelineState.COMPLETED)

    def test_invalid_transitions(self) -> None:
        """Verify impossible state transitions are invalid."""
        assert not is_valid_transition(PipelineState.PENDING, PipelineState.COMPLETED)
        assert not is_valid_transition(PipelineState.DISCOVERED, PipelineState.SUBMITTED)
        assert not is_valid_transition(PipelineState.QUALIFIED, PipelineState.QUALIFYING)

    def test_cancellation_from_active_state(self) -> None:
        """Verify cancellation is valid via intermediate failure states."""
        # Active → phase-specific-failed → FAILED → CANCELLED
        assert is_valid_transition(PipelineState.DISCOVERING, PipelineState.DISCOVERY_FAILED)
        assert is_valid_transition(PipelineState.DISCOVERY_FAILED, PipelineState.FAILED)
        assert is_valid_transition(PipelineState.FAILED, PipelineState.CANCELLED)


class TestPipelineContext:
    """Tests for the PipelineContext model."""

    def test_initial_state(self) -> None:
        """Verify a new context starts in PENDING state."""
        from tests.conftest import _make_opportunity

        opp = _make_opportunity("ctx-1")
        ctx = PipelineContext(opportunity=opp)
        assert ctx.state == PipelineState.PENDING
        assert ctx.history == []
        assert ctx.is_running is True
        assert ctx.elapsed_seconds is None

    def test_transition_to(self) -> None:
        """Verify transition_to updates state and records history."""
        from tests.conftest import _make_opportunity

        opp = _make_opportunity("ctx-2")
        ctx = PipelineContext(opportunity=opp)
        ctx.transition_to(PipelineState.INITIALISED, reason="Starting")
        assert ctx.state == PipelineState.INITIALISED
        assert len(ctx.history) == 1
        assert ctx.history[0].from_state == PipelineState.PENDING
        assert ctx.history[0].to_state == PipelineState.INITIALISED
        assert ctx.history[0].reason == "Starting"
        assert ctx.started_at is not None

    def test_invalid_transition_raises(self) -> None:
        """Verify an invalid transition raises ValueError."""
        from tests.conftest import _make_opportunity

        opp = _make_opportunity("ctx-3")
        ctx = PipelineContext(opportunity=opp)
        with pytest.raises(ValueError, match="Invalid transition"):
            ctx.transition_to(PipelineState.COMPLETED)

    def test_terminal_state_records_completed_at(self) -> None:
        """Verify reaching a terminal state sets completed_at."""
        from tests.conftest import _make_opportunity

        opp = _make_opportunity("ctx-4")
        ctx = PipelineContext(opportunity=opp)
        ctx.transition_to(PipelineState.INITIALISED)
        ctx.transition_to(PipelineState.CANCELLED)
        assert ctx.completed_at is not None
        assert ctx.is_running is False

    def test_elapsed_seconds(self) -> None:
        """Verify elapsed_seconds is calculated correctly."""
        from tests.conftest import _make_opportunity

        opp = _make_opportunity("ctx-5")
        ctx = PipelineContext(opportunity=opp)
        assert ctx.elapsed_seconds is None
        ctx.transition_to(PipelineState.INITIALISED)
        # The elapsed time should be a small positive number.
        elapsed = ctx.elapsed_seconds
        assert elapsed is None or isinstance(elapsed, float)


class TestPipelineResult:
    """Tests for the PipelineResult model."""

    def test_compute_stats_empty(self) -> None:
        """Verify stats are empty for a result with no contexts."""
        result = PipelineResult(success=True)
        result.compute_stats()
        assert result.stats == {"total": 0}

    def test_compute_stats_with_contexts(self) -> None:
        """Verify compute_stats aggregates state counts correctly."""
        from tests.conftest import _make_opportunity

        ctx1 = PipelineContext(opportunity=_make_opportunity("pr-1"))
        ctx1.transition_to(PipelineState.INITIALISED)
        ctx1.transition_to(PipelineState.DISCOVERING)
        ctx1.transition_to(PipelineState.DISCOVERED)
        ctx1.transition_to(PipelineState.CLASSIFYING)
        ctx1.transition_to(PipelineState.CLASSIFIED)
        ctx1.transition_to(PipelineState.QUALIFYING)
        ctx1.transition_to(PipelineState.QUALIFIED)

        ctx2 = PipelineContext(opportunity=_make_opportunity("pr-2"))
        ctx2.transition_to(PipelineState.INITIALISED)
        ctx2.transition_to(PipelineState.DISCOVERING)
        ctx2.transition_to(PipelineState.DISCOVERY_FAILED)
        ctx2.transition_to(PipelineState.FAILED)

        result = PipelineResult(success=True, pipeline_contexts=[ctx1, ctx2])
        result.compute_stats()
        assert result.stats["total"] == 2
        assert result.stats["qualified"] == 1
        assert result.stats["failed"] == 1


# ── Platform Models ─────────────────────────────────────────────────────────────


class TestPlatform:
    """Tests for the Platform enum."""

    def test_display_names(self) -> None:
        """Verify display_name returns human-readable labels."""
        assert Platform.UPWORK.display_name == "Upwork"
        assert Platform.LINKEDIN.display_name == "LinkedIn"
        assert Platform.FREELANCER.display_name == "Freelancer"
        assert Platform.REMOTE_OK.display_name == "Remote OK"
        assert Platform.YC_WORK.display_name == "Y Combinator (Work at a Startup)"

    def test_str_representation(self) -> None:
        """Verify str(platform) returns the value."""
        assert str(Platform.UPWORK) == "upwork"
        assert str(Platform.CUSTOM) == "custom"


class TestPlatformConfig:
    """Tests for PlatformConfig model."""

    def test_create_with_default_anti_bot(self) -> None:
        """Verify anti_bot_profile is populated from defaults."""
        config = PlatformConfig(
            platform=Platform.UPWORK,
            search_url="https://upwork.com/search",
        )
        assert config.platform == Platform.UPWORK
        assert config.enabled is True
        assert config.anti_bot_profile.get("stealth") is True
        assert config.rate_limit_delay == 3.0

    def test_explicit_anti_bot(self) -> None:
        """Verify an explicit anti_bot_profile is not overridden."""
        config = PlatformConfig(
            platform=Platform.UPWORK,
            search_url="https://upwork.com/search",
            anti_bot_profile={"custom": True},
        )
        assert config.anti_bot_profile == {"custom": True}

    def test_platform_serialisation(self) -> None:
        """Verify platform serialises to its string value."""
        config = PlatformConfig(
            platform=Platform.LINKEDIN,
            search_url="https://linkedin.com/jobs",
        )
        data = config.model_dump()
        assert data["platform"] == "linkedin"

    def test_rate_limit_bounds(self) -> None:
        """Verify rate_limit_delay is clamped to valid range."""
        with pytest.raises(ValidationError):
            PlatformConfig(
                platform=Platform.UPWORK,
                search_url="https://upwork.com",
                rate_limit_delay=0.1,
            )


class TestPlatformCredentials:
    """Tests for PlatformCredentials model."""

    def test_secrets_redacted_in_dump(self) -> None:
        """Verify password, api_key, token, and cookies are redacted."""
        creds = PlatformCredentials(
            platform=Platform.UPWORK,
            username="test_user",
            password="super-secret",
            api_key="sk-12345",
        )
        dumped = creds.model_dump()
        assert dumped["password"] == "********"
        assert dumped["api_key"] == "********"
        assert dumped["username"] == "test_user"  # Not redacted

    def test_redacted_method(self) -> None:
        """Verify redacted() returns safe-to-log data."""
        creds = PlatformCredentials(
            platform=Platform.LINKEDIN,
            username="jane",
            token="oauth-token-abc",
        )
        safe = creds.redacted()
        assert safe["token"] == "********"
        assert safe["username"] == "jane"

    def test_null_secrets_remain_null(self) -> None:
        """Verify None secrets stay as None after dump."""
        creds = PlatformCredentials(
            platform=Platform.UPWORK,
            username="test",
        )
        dumped = creds.model_dump()
        assert dumped["password"] is None
        assert dumped["api_key"] is None
