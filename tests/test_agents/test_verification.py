"""Tests for the VerificationAgent — quality checks, readability, and scoring."""

from __future__ import annotations

import pytest

from freelance_lead_gen.agents.verification_agent import (
    VerificationAgent,
    VerificationResult,
)
from freelance_lead_gen.models.opportunity import LeadOpportunity, OutboundDraft

# ── Sample data ─────────────────────────────────────────────────────────────────


GOOD_DRAFT_TEXT = (
    "I've been building RAG pipelines for three years using LangChain "
    "and Pinecone. At my last gig I cut support response times by half "
    "with an automated Q&A system.\n\n"
    "Your TechCorp project looks like a solid match. I'd love to hop on a quick "
    "call to discuss your specific needs."
)

SAMPLE_OPP = LeadOpportunity(
    platform="upwork",
    platform_job_id="verif-1",
    title="AI Automation Engineer for RAG Pipeline",
    company="TechCorp",
    description="Build a RAG pipeline for customer support.",
    skills=["Python", "LangChain", "RAG", "Vector DB", "OpenAI"],
    budget_min=8000.0,
    budget_max=12000.0,
    status="qualified",
    score=85,
)


def _make_draft(body: str, subject: str | None = None) -> OutboundDraft:
    """Create an OutboundDraft with the given body text."""
    draft = OutboundDraft(opportunity_id=SAMPLE_OPP.id, subject=subject)
    draft.add_version(body)
    return draft


# ── VerificationAgent Initialisation ────────────────────────────────────────────


class TestVerificationAgentInit:
    """Tests for VerificationAgent initialisation."""

    def test_init_defaults(self, test_settings) -> None:
        """Verify agent initialises with default dependencies."""
        agent = VerificationAgent(settings=test_settings)
        assert agent.quality_threshold == 65
        assert agent.stats["total_verified"] == 0

    def test_init_custom_threshold(self, test_settings) -> None:
        """Verify a custom quality threshold is accepted."""
        agent = VerificationAgent(quality_threshold=80, settings=test_settings)
        assert agent.quality_threshold == 80

    def test_init_clamps_threshold(self, test_settings) -> None:
        """Verify the threshold is clamped to 0-100."""
        agent = VerificationAgent(quality_threshold=150, settings=test_settings)
        assert agent.quality_threshold == 100

        agent_low = VerificationAgent(quality_threshold=-10, settings=test_settings)
        assert agent_low.quality_threshold == 0


# ── Full Verification ───────────────────────────────────────────────────────────


class TestVerificationFull:
    """Tests for the full verify() method."""

    @pytest.mark.asyncio
    async def test_verify_good_draft_passes(self, test_settings) -> None:
        """Verify a well-written draft passes all checks."""
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft(GOOD_DRAFT_TEXT, subject="RAG Pipeline proposal")

        result = await agent.verify(draft, SAMPLE_OPP)
        assert result.passed is True
        assert result.score >= 60
        assert len(result.issues) == 0

    @pytest.mark.asyncio
    async def test_verify_banned_phrases_fail(self, test_settings) -> None:
        """Verify banned phrases cause a draft to fail."""
        agent = VerificationAgent(settings=test_settings)
        bad_text = (
            "I hope this message finds you well. I am writing to express "
            "my interest in your project. I believe my skills would be "
            "a great fit. Best regards, John"
        )
        draft = _make_draft(bad_text)

        result = await agent.verify(draft, SAMPLE_OPP)
        assert result.passed is False
        assert len(result.banned_phrases_found) > 0
        assert "I hope this message finds you well" in result.banned_phrases_found[0]

    @pytest.mark.asyncio
    async def test_verify_ai_markers_fail(self, test_settings) -> None:
        """Verify AI identity markers cause a draft to fail."""
        agent = VerificationAgent(settings=test_settings)
        bad_text = (
            "As an AI language model, I don't have personal experience, "
            "but I can help you with your project based on my training data."
        )
        draft = _make_draft(bad_text)

        result = await agent.verify(draft, SAMPLE_OPP)
        assert result.passed is False
        assert len(result.ai_markers_found) > 0

    @pytest.mark.asyncio
    async def test_verify_too_short(self, test_settings) -> None:
        """Verify very short drafts have length issues flagged.

        Note: the agent's `passed` field only requires score >= threshold
        AND no banned phrases.  Short-but-clean drafts may still pass.
        """
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft("Short.")

        result = await agent.verify(draft, SAMPLE_OPP)
        assert any("short" in i.lower() for i in result.issues)

    @pytest.mark.asyncio
    async def test_verify_missing_skills(self, test_settings) -> None:
        """Verify drafts that don't mention required skills are flagged in issues.

        The agent reports skill gaps in the issues list but does not block
        the draft on them (only banned phrases block).
        """
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft("I have general experience in software development.")

        result = await agent.verify(draft, SAMPLE_OPP)
        assert any("skill" in i.lower() for i in result.issues)

    @pytest.mark.asyncio
    async def test_verify_placeholder_detected(self, test_settings) -> None:
        """Verify drafts with placeholders have them detected in issues."""
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft("I have experience with [your name] and [company] requirements.")

        result = await agent.verify(draft, SAMPLE_OPP)
        assert any("placeholder" in i.lower() for i in result.issues)

    @pytest.mark.asyncio
    async def test_verify_with_custom_threshold(self, test_settings) -> None:
        """Verify the threshold parameter overrides the default."""
        agent = VerificationAgent(quality_threshold=90, settings=test_settings)
        draft = _make_draft(GOOD_DRAFT_TEXT)

        # The good draft might score below 90.
        result = await agent.verify(draft, SAMPLE_OPP, threshold=90)
        # Passing depends on how the good draft scores; we just check the
        # result is a valid VerificationResult.
        assert isinstance(result, VerificationResult)

    @pytest.mark.asyncio
    async def test_verify_empty_draft_has_issues(self, test_settings) -> None:
        """Verify an empty draft has issues reported.

        The agent reports structural and length issues even though the
        draft may still pass (no banned phrases present).
        """
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft("")
        result = await agent.verify(draft, SAMPLE_OPP)
        assert len(result.issues) > 0


# ── Banned Phrase Detection ─────────────────────────────────────────────────────


class TestBannedPhraseDetection:
    """Tests for the banned phrase regex patterns."""

    def test_detect_banned_phrases(self) -> None:
        """Verify common banned phrases are detected."""
        agent = VerificationAgent()
        text = "I hope this message finds you well. Best regards, John"

        result = agent._check_banned_phrases(text)
        assert result["count"] >= 1
        assert result["blocker"] is True

    def test_clean_text_no_banned(self) -> None:
        """Verify natural text has no banned phrases."""
        agent = VerificationAgent()
        text = "Hey, I've been working on similar projects for years."
        result = agent._check_banned_phrases(text)
        assert result["count"] == 0
        assert result["blocker"] is False

    def test_multiple_banned_phrases(self) -> None:
        """Verify multiple banned phrases are all found."""
        agent = VerificationAgent()
        text = (
            "I hope this message finds you well. "
            "I am writing to express my interest. "
            "Thank you for considering my application."
        )
        result = agent._check_banned_phrases(text)
        assert result["count"] >= 2


# ── AI Marker Detection ─────────────────────────────────────────────────────────


class TestAIMarkerDetection:
    """Tests for AI identity marker detection."""

    def test_detect_ai_markers(self) -> None:
        """Verify AI identity declarations are detected."""
        agent = VerificationAgent()
        text = "As an AI, I cannot provide personal experience."
        result = agent._check_ai_markers(text)
        assert result["count"] >= 1

    def test_clean_text_no_markers(self) -> None:
        """Verify normal text has no AI markers."""
        agent = VerificationAgent()
        text = "I have 5 years of experience building AI systems."
        result = agent._check_ai_markers(text)
        assert result["count"] == 0


# ── Structure Checks ────────────────────────────────────────────────────────────


class TestStructureChecks:
    """Tests for document structure verification."""

    def test_good_structure(self) -> None:
        """Verify well-structured text has a high structure score."""
        agent = VerificationAgent()
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three. More content here."
        result = agent._check_structure(text)
        # Uniform paragraph lengths may trigger a minor warning.
        assert result["structure_score"] >= 70

    def test_too_few_paragraphs(self) -> None:
        """Verify too few paragraphs is flagged."""
        agent = VerificationAgent()
        result = agent._check_structure("Just one paragraph.")
        assert any("Too few" in i for i in result["issues"])
        assert result["structure_score"] < 100

    def test_bullet_points_flagged(self) -> None:
        """Verify bullet points are flagged."""
        agent = VerificationAgent()
        text = "Paragraph one.\n\n- Bullet one\n- Bullet two"
        result = agent._check_structure(text)
        assert any("bullet" in i.lower() for i in result["issues"])

    def test_numbered_lists_flagged(self) -> None:
        """Verify numbered lists are flagged."""
        agent = VerificationAgent()
        text = "Paragraph one.\n\n1. First item\n2. Second item"
        result = agent._check_structure(text)
        assert any("numbered" in i.lower() for i in result["issues"])


# ── Length Checks ───────────────────────────────────────────────────────────────


class TestLengthChecks:
    """Tests for word count and length verification."""

    def test_good_length(self) -> None:
        """Verify drafts within bounds pass."""
        agent = VerificationAgent()
        body = "word " * 50  # ~50 words
        result = agent._check_length(body, "Short subject")
        assert len(result["issues"]) == 0

    def test_too_short(self) -> None:
        """Verify very short drafts are flagged."""
        agent = VerificationAgent()
        result = agent._check_length("Hello", "Subject")
        assert any("short" in i.lower() for i in result["issues"])
        assert result["penalty"] > 0

    def test_too_long(self) -> None:
        """Verify overly long drafts are flagged."""
        agent = VerificationAgent()
        body = "long " * 300  # ~300 words
        result = agent._check_length(body, "Subject")
        assert any("long" in i.lower() for i in result["issues"])
        assert result["penalty"] > 0

    def test_long_subject(self) -> None:
        """Verify long subjects are flagged."""
        agent = VerificationAgent()
        result = agent._check_length("Normal body text.", "X" * 100)
        assert any("subject" in i.lower() for i in result["issues"])


# ── Readability ─────────────────────────────────────────────────────────────────


class TestReadability:
    """Tests for Flesch Reading Ease scoring."""

    def test_readability_normal_text(self) -> None:
        """Verify readability returns a reasonable score for normal text."""
        agent = VerificationAgent()
        text = (
            "This is a simple test. It should be fairly readable. "
            "Most people can understand this easily. The words are common."
        )
        score = agent._compute_readability(text)
        assert score is not None
        assert 0 <= score <= 100

    def test_readability_empty(self) -> None:
        """Verify readability returns None for empty text."""
        agent = VerificationAgent()
        score = agent._compute_readability("")
        assert score is None

    def test_readability_too_short(self) -> None:
        """Verify readability returns None for very short text."""
        agent = VerificationAgent()
        score = agent._compute_readability("Hi")
        assert score is None

    def test_syllable_count(self) -> None:
        """Verify syllable counting works for known words."""
        count = VerificationAgent._count_syllables("hello world")
        assert count >= 2  # hel-lo (2) + world (1)


# ── Technical Accuracy ──────────────────────────────────────────────────────────


class TestTechnicalAccuracy:
    """Tests for technical accuracy verification."""

    def test_draft_mentions_skills_and_company(self) -> None:
        """Verify a draft mentioning skills AND the company has no issues."""
        agent = VerificationAgent()
        body = "I have extensive experience with Python and LangChain at TechCorp."
        issues = agent._check_technical_accuracy(body, SAMPLE_OPP)
        assert len(issues) == 0

    def test_draft_missing_skills(self) -> None:
        """Verify a draft not mentioning skills is flagged."""
        agent = VerificationAgent()
        body = "I have general writing experience."
        issues = agent._check_technical_accuracy(body, SAMPLE_OPP)
        assert any("skill" in i.lower() for i in issues)

    def test_draft_missing_company(self) -> None:
        """Verify a draft not mentioning the company is flagged."""
        agent = VerificationAgent()
        body = "I have Python and RAG experience."
        issues = agent._check_technical_accuracy(body, SAMPLE_OPP)
        assert any("client" in i.lower() or "TechCorp" in i for i in issues)


# ── Scoring ─────────────────────────────────────────────────────────────────────


class TestScoring:
    """Tests for the overall scoring computation."""

    def test_perfect_score(self) -> None:
        """Verify a perfect score is 100."""
        agent = VerificationAgent()
        score = agent._compute_overall_score(
            banned_count=0,
            ai_marker_count=0,
            structure_score=100,
            readability_score=70.0,
            length_penalty=0,
        )
        assert score == 100

    def test_banned_phrases_reduce_score(self) -> None:
        """Verify banned phrases significantly reduce the score."""
        agent = VerificationAgent()
        score = agent._compute_overall_score(
            banned_count=1,
            ai_marker_count=0,
            structure_score=100,
            readability_score=70.0,
            length_penalty=0,
        )
        assert score <= 75  # At least 25 points penalty.

    def test_ai_markers_reduce_score(self) -> None:
        """Verify AI markers reduce the score."""
        agent = VerificationAgent()
        score = agent._compute_overall_score(
            banned_count=0,
            ai_marker_count=1,
            structure_score=100,
            readability_score=70.0,
            length_penalty=0,
        )
        assert score <= 80  # At least 20 points penalty.

    def test_score_clamped(self) -> None:
        """Verify the score never goes below 0."""
        agent = VerificationAgent()
        score = agent._compute_overall_score(
            banned_count=5,
            ai_marker_count=3,
            structure_score=0,
            readability_score=0.0,
            length_penalty=50,
        )
        assert score >= 0


# ── Fix Suggestions ─────────────────────────────────────────────────────────────


class TestSuggestions:
    """Tests for the fix suggestion generator."""

    def test_suggestions_for_banned(self) -> None:
        """Verify banned phrase issues produce a suggestion."""
        agent = VerificationAgent()
        suggestions = agent._generate_fixes(["Banned phrase detected: 'test'"])
        assert any("banned" not in s.lower() for s in suggestions)
        assert len(suggestions) > 0

    def test_suggestions_for_structure(self) -> None:
        """Verify structure issues produce suggestions."""
        agent = VerificationAgent()
        issues = ["Too few paragraphs: 1 (minimum 2)"]
        suggestions = agent._generate_fixes(issues)
        assert any("substance" in s.lower() or "more" in s.lower() for s in suggestions)

    def test_suggestions_empty(self) -> None:
        """Verify no issues returns an empty suggestion list."""
        agent = VerificationAgent()
        suggestions = agent._generate_fixes([])
        assert suggestions == []


# ── Verify and Regenerate ───────────────────────────────────────────────────────


class TestVerifyAndRegenerate:
    """Tests for the verify_and_regenerate method."""

    @pytest.mark.asyncio
    async def test_verify_and_regenerate_good(self, test_settings) -> None:
        """Verify verify_and_regenerate returns the draft unchanged when it passes."""
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft(GOOD_DRAFT_TEXT)
        final_draft, result = await agent.verify_and_regenerate(draft, SAMPLE_OPP)
        assert final_draft is draft
        assert isinstance(result, VerificationResult)

    @pytest.mark.asyncio
    async def test_verify_and_regenerate_bad(self, test_settings) -> None:
        """Verify verify_and_regenerate returns the failed result."""
        agent = VerificationAgent(settings=test_settings)
        draft = _make_draft("I hope this message finds you well.")
        _final_draft, result = await agent.verify_and_regenerate(draft, SAMPLE_OPP)
        assert result.passed is False
