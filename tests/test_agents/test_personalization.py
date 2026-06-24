"""Tests for the PersonalizationAgent — draft generation and anti-AI quality checks."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from freelance_lead_gen.agents.personalization_agent import (
    DraftGenerationError,
    PersonalizationAgent,
    PersonalizationReport,
    _DraftGeneration,
    _AI_MARKER_PATTERNS,
    _BANNED_PHRASES,
)
from freelance_lead_gen.agents.profile_matcher import TargetProfile
from freelance_lead_gen.config.settings import get_settings
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus, OutboundDraft
from freelance_lead_gen.storage.repository import OpportunityRepository


# ── Sample data ─────────────────────────────────────────────────────────────────


SAMPLE_OPP = LeadOpportunity(
    platform="upwork",
    platform_job_id="personal-1",
    title="AI Automation Engineer for RAG Pipeline",
    company="TechCorp",
    description=(
        "We need an experienced AI engineer to build a RAG pipeline "
        "for customer support automation."
    ),
    budget_min=8000.0,
    budget_max=12000.0,
    skills=["Python", "LangChain", "RAG", "Vector DB"],
    location="Remote",
    status=LeadStatus.QUALIFIED,
    score=85,
)

SAMPLE_PROFILE = TargetProfile.default()


# ── PersonalizationAgent ────────────────────────────────────────────────────────


class TestPersonalizationAgentInit:
    """Tests for PersonalizationAgent initialisation."""

    def test_init_defaults(self, test_settings) -> None:  # noqa: ANN001
        """Verify agent initialises with default dependencies."""
        agent = PersonalizationAgent(settings=test_settings)
        assert agent.stats["runs"] == 0
        assert agent.stats["total_drafted"] == 0
        assert len(agent.banned_phrases) > 0

    def test_init_with_custom_deps(self, test_settings) -> None:  # noqa: ANN001
        """Verify agent accepts custom dependencies."""
        mock_repo = AsyncMock(spec=OpportunityRepository)
        mock_llm = AsyncMock()
        agent = PersonalizationAgent(
            llm_client=mock_llm,
            repository=mock_repo,
            settings=test_settings,
        )
        assert agent._llm is mock_llm
        assert agent._repository is mock_repo


class TestPersonalizationGenerateDraft:
    """Tests for draft generation."""

    @pytest.mark.asyncio
    async def test_generate_draft_success(self, test_settings) -> None:  # noqa: ANN001
        """Verify draft generation produces a valid OutboundDraft."""
        mock_llm = AsyncMock()
        mock_llm.chat_completion.return_value = {
            "subject": "Proposal for RAG Pipeline project",
            "body": (
                "I've built RAG pipelines for three enterprise clients using "
                "LangChain and Pinecone. At my last role, I reduced support "
                "ticket resolution time by 40% with an automated answer system.\n\n"
                "Your project looks like a great match for my skills. "
                "I'd love to discuss how I can help."
            ),
            "version": 1,
            "platform_adaptations": ["Upwork proposal format"],
        }

        mock_repo = AsyncMock(spec=OpportunityRepository)

        agent = PersonalizationAgent(
            llm_client=mock_llm,
            repository=mock_repo,
            settings=test_settings,
        )

        draft = await agent.generate_draft(SAMPLE_OPP, SAMPLE_PROFILE)

        assert isinstance(draft, OutboundDraft)
        assert draft.opportunity_id == SAMPLE_OPP.id
        assert draft.subject == "Proposal for RAG Pipeline project"
        assert draft.current_body is not None
        assert draft.version_count >= 1
        assert draft.approved is False

        # Verify the draft was persisted.
        mock_repo.create_draft.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_draft_quality_retry(self, test_settings) -> None:  # noqa: ANN001
        """Verify the agent retries when quality checks fail."""
        mock_llm = AsyncMock()
        # First call returns a banned phrase draft; second returns a clean one.
        GOOD_BODY = (
            "Hey, I saw you're hiring for a RAG engineer. "
            "I've built similar systems before and would love to "
            "chat about your specific needs.\n\n"
            "I have 5 years of Python experience and 3 years "
            "working with LangChain and vector databases.\n\n"
            "Let me know if you want to set up a quick call."
        )
        mock_llm.chat_completion.side_effect = [
            {
                "subject": "Bad draft",
                "body": (
                    "I hope this message finds you well. I am writing to express "
                    "my interest in your project. I believe my skills would be "
                    "a great fit. Best regards, AI Bot"
                ),
                "version": 1,
                "platform_adaptations": [],
            },
            {
                "subject": "Good draft",
                "body": GOOD_BODY,
                "version": 1,
                "platform_adaptations": [],
            },
        ]

        mock_repo = AsyncMock(spec=OpportunityRepository)

        agent = PersonalizationAgent(
            llm_client=mock_llm,
            repository=mock_repo,
            settings=test_settings,
        )

        draft = await agent.generate_draft(SAMPLE_OPP, SAMPLE_PROFILE)

        assert draft.current_body is not None
        assert "I hope this message finds you well" not in draft.current_body
        # The LLM was called twice (initial + one retry).
        assert mock_llm.chat_completion.await_count == 2

    @pytest.mark.asyncio
    async def test_generate_draft_all_retries_exhausted(self, test_settings) -> None:  # noqa: ANN001
        """Verify the agent raises when the LLM keeps failing."""
        mock_llm = AsyncMock()
        # LLM keeps raising errors — even after retries the agent fails.
        mock_llm.chat_completion.side_effect = RuntimeError("LLM unavailable")

        mock_repo = AsyncMock(spec=OpportunityRepository)

        agent = PersonalizationAgent(
            llm_client=mock_llm,
            repository=mock_repo,
            settings=test_settings,
        )

        with pytest.raises(DraftGenerationError, match="Failed to generate"):
            await agent.generate_draft(SAMPLE_OPP, SAMPLE_PROFILE, max_retries_on_quality=1)

    @pytest.mark.asyncio
    async def test_generate_draft_llm_error(self, test_settings) -> None:  # noqa: ANN001
        """Verify the agent handles LLM failures gracefully."""
        mock_llm = AsyncMock()
        mock_llm.chat_completion.side_effect = RuntimeError("LLM unavailable")

        mock_repo = AsyncMock(spec=OpportunityRepository)

        agent = PersonalizationAgent(
            llm_client=mock_llm,
            repository=mock_repo,
            settings=test_settings,
        )

        with pytest.raises(DraftGenerationError):
            await agent.generate_draft(SAMPLE_OPP, SAMPLE_PROFILE, max_retries_on_quality=1)


# ── Anti-AI Quality Checks ──────────────────────────────────────────────────────


class TestAntiAIQuality:
    """Tests for anti-AI tone detection and banned phrase checking."""

    def test_banned_phrases_detected(self) -> None:
        """Verify banned phrases are correctly detected."""
        agent = PersonalizationAgent()
        text = (
            "I hope this message finds you well. "
            "I am writing to express my interest in your project. "
            "I look forward to the possibility of working together. "
            "Best regards, John"
        )
        result = agent.check_human_tone(text)
        assert len(result["banned_phrases_found"]) > 0
        assert result["passed"] is False

    def test_clean_text_passes(self) -> None:
        """Verify natural-sounding text passes quality checks."""
        agent = PersonalizationAgent()
        text = (
            "Hey! I've been building AI systems for the past 5 years. "
            "I saw your project and it looks like a solid fit. "
            "I've done similar work for a few startups. "
            "Let me know if you want to chat about it."
        )
        result = agent.check_human_tone(text)
        assert result["passed"] is True
        assert len(result["banned_phrases_found"]) == 0
        assert len(result["ai_markers_found"]) == 0

    def test_ai_markers_detected(self) -> None:
        """Verify AI identity markers are detected."""
        agent = PersonalizationAgent()
        text = "As an AI language model, I cannot provide personal opinions."
        result = agent.check_human_tone(text)
        assert len(result["ai_markers_found"]) > 0
        assert result["passed"] is False

    def test_formal_language_penalised(self) -> None:
        """Verify overly formal language lowers the score."""
        agent = PersonalizationAgent()
        text = (
            "Furthermore, I have extensive experience in this domain. "
            "Moreover, my background aligns perfectly with your requirements. "
            "Additionally, I bring unique skills. Consequently, I am confident."
        )
        result = agent.check_human_tone(text)
        # Score should be reduced by formal markers, but no banned phrases.
        assert len(result["banned_phrases_found"]) == 0
        assert result["score"] < 100


# ── Quality check internals ─────────────────────────────────────────────────────


class TestPersonalizationQualityInternals:
    """Tests for internal quality-check methods."""

    def test_check_quality_clean(self) -> None:
        """Verify _check_quality returns empty list for good text."""
        agent = PersonalizationAgent()
        body = (
            "I've been building RAG pipelines for 3 years. "
            "My last project reduced hallucination rates by 40%.\n\n"
            "I'd love to help with your project. "
            "I think my experience maps well to what you need.\n\n"
            "Let me know if you want to chat. "
            "Happy to hop on a quick call."
        )
        issues = agent._check_quality(body, "Short subject")
        assert issues == []

    def test_check_quality_banned_phrase(self) -> None:
        """Verify _check_quality detects banned phrases."""
        agent = PersonalizationAgent()
        body = "I am writing to express my interest in this opportunity."
        issues = agent._check_quality(body)
        assert len(issues) >= 1
        assert "Banned phrase" in issues[0]

    def test_check_quality_bullet_points(self) -> None:
        """Verify _check_quality flags bullet points."""
        agent = PersonalizationAgent()
        body = (
            "I have relevant experience:\n"
            "- Built RAG pipelines\n"
            "- Worked with LangChain\n"
            "- Deployed to production"
        )
        issues = agent._check_quality(body)
        assert any("bullet" in i.lower() for i in issues)

    def test_check_quality_too_few_paragraphs(self) -> None:
        """Verify _check_quality flags drafts with too few paragraphs."""
        agent = PersonalizationAgent()
        body = "Just one short paragraph."
        issues = agent._check_quality(body)
        assert any("Too few paragraphs" in i for i in issues)

    def test_check_quality_long_subject(self) -> None:
        """Verify _check_quality flags overly long subjects."""
        agent = PersonalizationAgent()
        body = "A normal paragraph.\n\nAnother paragraph."
        subject = "X" * 61
        issues = agent._check_quality(body, subject)
        assert any("Subject too long" in i for i in issues)

    def test_score_human_tone_contractions(self) -> None:
        """Verify contractions increase the human tone score."""
        agent = PersonalizationAgent()
        text_with = "I've built this. I'll help you. Don't worry."
        text_without = "I have built this. I will help you. Do not worry."
        score_with = agent._score_human_tone(text_with)
        score_without = agent._score_human_tone(text_without)
        assert score_with >= score_without


# ── PersonalizationReport ───────────────────────────────────────────────────────


class TestPersonalizationReport:
    """Tests for PersonalizationReport dataclass."""

    def test_elapsed_seconds_none(self) -> None:
        """Verify elapsed_seconds is None when not completed."""
        report = PersonalizationReport()
        assert report.elapsed_seconds is None

    def test_elapsed_seconds_computed(self) -> None:
        """Verify elapsed_seconds works when completed."""
        from datetime import datetime, timezone

        report = PersonalizationReport(
            started_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
        )
        assert report.elapsed_seconds == 60.0
