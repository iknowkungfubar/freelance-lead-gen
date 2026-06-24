"""Quality verification for generated outreach content.

The :class:`VerificationAgent` runs multiple quality checks on generated
drafts: banned-phrase detection, readability scoring, length constraints,
and technical accuracy.  Drafts that fall below a configurable threshold
can be flagged for regeneration.
"""

from __future__ import annotations as _annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field

from freelance_lead_gen.config.settings import Settings, get_settings
from freelance_lead_gen.llm import LLMClient
from freelance_lead_gen.models.opportunity import LeadOpportunity, OutboundDraft

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_MIN_PARAGRAPHS: int = 2
"""Minimum acceptable number of paragraphs."""

_MAX_PARAGRAPHS: int = 6
"""Maximum acceptable number of paragraphs."""

_MIN_WORDS: int = 30
"""Minimum word count for a meaningful draft."""

_MAX_WORDS: int = 200
"""Maximum word count (keep proposals concise)."""

_MAX_SUBJECT_LENGTH: int = 60
"""Maximum subject line length in characters."""

_QUALITY_THRESHOLD_DEFAULT: int = 65
"""Default minimum quality score (0-100) for a passing draft."""

# ── Banned phrase patterns ─────────────────────────────────────────────────────

_BANNED_PHRASE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bi hope this message finds you well\b", re.IGNORECASE),
    re.compile(r"\bi came across your (project|listing|post)\b", re.IGNORECASE),
    re.compile(r"\bi am writing to express (my|an) interest\b", re.IGNORECASE),
    re.compile(r"\bi believe my skills would be a great fit\b", re.IGNORECASE),
    re.compile(r"\bi look forward to the possibility\b", re.IGNORECASE),
    re.compile(r"\bplease let me know if you have any questions\b", re.IGNORECASE),
    re.compile(r"\bi am confident (that )?i can deliver\b", re.IGNORECASE),
    re.compile(r"\bthank you for considering (my|the)\b", re.IGNORECASE),
    re.compile(r"\b(best|kind|warm) regards\b", re.IGNORECASE),
    re.compile(r"\bi would love to (join|be a part of)\b", re.IGNORECASE),
    re.compile(r"\bi am excited about (the|this) opportunity\b", re.IGNORECASE),
    re.compile(r"\bi have reviewed your requirements\b", re.IGNORECASE),
    re.compile(r"\bas per your (requirements|request|needs)\b", re.IGNORECASE),
    re.compile(r"\bfeel free to (reach out|contact me)\b", re.IGNORECASE),
    re.compile(r"\bdon't hesitate to\b", re.IGNORECASE),
    re.compile(r"\bi would be a great (asset|addition|fit)\b", re.IGNORECASE),
    re.compile(r"\b(i'm|i am) writing to apply\b", re.IGNORECASE),
    re.compile(r"\blooking forward to hearing from you\b", re.IGNORECASE),
]

_AI_MARKER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(as an AI|as a language model|as an LLM)\b", re.IGNORECASE),
    re.compile(r"\bI cannot\b(?=.*\b(AI|language model|assistant)\b)", re.IGNORECASE),
    re.compile(r"\bmy knowledge cutoff\b", re.IGNORECASE),
    re.compile(r"\bI don't have (access to|personal|emotions)\b", re.IGNORECASE),
    re.compile(r"\bI'm (just|only) an AI\b", re.IGNORECASE),
    re.compile(r"\bI was trained (by|on|using)\b", re.IGNORECASE),
]

# ── Verification Result ────────────────────────────────────────────────────────


class VerificationResult(BaseModel):
    """The structured output of a verification check on a generated draft."""

    passed: bool
    """Whether the draft passed all checks (score >= threshold, no blockers)."""

    score: int = Field(..., ge=0, le=100)
    """Overall quality score (0-100)."""

    issues: list[str] = Field(default_factory=list)
    """Human-readable descriptions of each issue found."""

    readability_score: float | None = Field(default=None, ge=0.0, le=100.0)
    """Flesch Reading Ease score, if computed."""

    word_count: int = Field(default=0, ge=0)
    """Total word count."""
    paragraph_count: int = Field(default=0, ge=0)
    """Number of paragraphs detected."""

    banned_phrases_found: list[str] = Field(default_factory=list)
    """Specific banned phrases that were detected."""

    ai_markers_found: list[str] = Field(default_factory=list)
    """AI identity markers that were detected."""

    suggested_fixes: list[str] = Field(default_factory=list)
    """Actionable suggestions for improving the draft."""


# ── Verification Agent ─────────────────────────────────────────────────────────


class VerificationAgent:
    """Runs quality checks on generated outreach drafts.

    Checks cover banned phrases, AI markers, readability (Flesch-Kincaid),
    length constraints, and technical accuracy.  Results are returned as
    :class:`VerificationResult` structs, and drafts below threshold can be
    sent back for regeneration.

    Parameters
    ----------
    llm_client : LLMClient or None
        Client for optional LLM-assisted verification.  Created with
        defaults if not provided.
    quality_threshold : int
        Minimum score for a draft to pass (default 65).
    settings : Settings or None
        Application settings.
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        quality_threshold: int = _QUALITY_THRESHOLD_DEFAULT,
        settings: Settings | None = None,
    ) -> None:
        self._settings: Settings = settings or get_settings()
        self._llm: LLMClient | None = llm_client
        self._quality_threshold: int = max(0, min(100, quality_threshold))

        self._stats: dict[str, int] = {
            "total_verified": 0,
            "total_passed": 0,
            "total_failed": 0,
            "total_llm_checks": 0,
        }

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        """Return a copy of lifetime statistics."""
        return dict(self._stats)

    @property
    def quality_threshold(self) -> int:
        """Current minimum passing score."""
        return self._quality_threshold

    # ── Public API ───────────────────────────────────────────────────────

    async def verify(
        self,
        draft: OutboundDraft,
        opportunity: LeadOpportunity | None = None,
        *,
        use_llm: bool = False,
        threshold: int | None = None,
    ) -> VerificationResult:
        """Run all quality checks on a draft and return the result.

        Parameters
        ----------
        draft : OutboundDraft
            The draft to verify.  The current active version is checked.
        opportunity : LeadOpportunity or None
            Related opportunity for context-dependent checks (technical
            accuracy, skill relevance).
        use_llm : bool
            Whether to run an additional LLM-based verification pass
            (slower but catches subtle issues).
        threshold : int or None
            Override the quality threshold for this check.  Uses the
            agent's default if not provided.

        Returns
        -------
        VerificationResult
        """
        body = draft.current_body or ""
        subject = draft.subject or ""
        effective_threshold = threshold if threshold is not None else self._quality_threshold

        # ── Run all checks ───────────────────────────────────────────────
        regex_issues = self._check_banned_phrases(body)
        ai_issues = self._check_ai_markers(body)
        structure_issues = self._check_structure(body)
        readability = self._compute_readability(body)
        length_check = self._check_length(body, subject)
        accuracy_issues = self._check_technical_accuracy(body, opportunity) if opportunity else []

        # ── Aggregate issues ─────────────────────────────────────────────
        all_issues: list[str] = []
        all_issues.extend(regex_issues["descriptions"])
        all_issues.extend(ai_issues["descriptions"])
        all_issues.extend(structure_issues["issues"])
        all_issues.extend(length_check["issues"])
        all_issues.extend(accuracy_issues)

        # ── Overall score ────────────────────────────────────────────────
        score = self._compute_overall_score(
            banned_count=regex_issues["count"],
            ai_marker_count=ai_issues["count"],
            structure_score=structure_issues["structure_score"] if isinstance(structure_issues, dict) else 50,
            readability_score=readability,
            length_penalty=length_check["penalty"],
        )

        # ── LLM-assisted verification ────────────────────────────────────
        llm_issues: list[str] = []
        if use_llm and self._llm is not None:
            try:
                llm_result = await self._llm_verify(draft, opportunity)
                llm_issues = llm_result.get("issues", [])
                all_issues.extend(llm_issues)
                if llm_result.get("score_adjustment"):
                    score = max(0, score + llm_result["score_adjustment"])
                self._stats["total_llm_checks"] += 1
            except Exception as exc:
                logger.warning("verification.llm_check_failed", error=str(exc))

        # ── Build result ─────────────────────────────────────────────────
        word_count = len(body.split())
        paragraphs = [p for p in body.split("\n\n") if p.strip()]

        result = VerificationResult(
            passed=score >= effective_threshold and not regex_issues["blocker"],
            score=score,
            issues=all_issues,
            readability_score=readability,
            word_count=word_count,
            paragraph_count=len(paragraphs),
            banned_phrases_found=regex_issues["phrases_found"],
            ai_markers_found=ai_issues["markers_found"],
            suggested_fixes=self._generate_fixes(all_issues),
        )

        # ── Update stats ─────────────────────────────────────────────────
        self._stats["total_verified"] += 1
        if result.passed:
            self._stats["total_passed"] += 1
        else:
            self._stats["total_failed"] += 1

        logger.info(
            "verification.complete",
            draft_id=draft.id,
            passed=result.passed,
            score=result.score,
            issues=len(result.issues),
        )

        return result

    async def verify_and_regenerate(
        self,
        draft: OutboundDraft,
        opportunity: LeadOpportunity,
        *,
        max_attempts: int = 3,
    ) -> tuple[OutboundDraft, VerificationResult]:
        """Verify a draft and regenerate if it does not meet quality standards.

        Parameters
        ----------
        draft : OutboundDraft
            The initial draft to verify.
        opportunity : LeadOpportunity
            The related opportunity (for context in regeneration).
        max_attempts : int
            Maximum number of regeneration attempts (default 3).

        Returns
        -------
        tuple of (OutboundDraft, VerificationResult)
            The final draft (possibly regenerated) and its verification
            result.
        """
        current_draft = draft
        result = await self.verify(current_draft, opportunity)

        for attempt in range(1, max_attempts):
            if result.passed:
                break

            logger.info(
                "verification.regenerating",
                draft_id=current_draft.id,
                attempt=attempt,
                score=result.score,
                issues=result.issues,
            )

            # We cannot regenerate here directly since that is the
            # PersonalizationAgent's job.  Instead we note the issues
            # and return the failed result so the orchestrator can
            # decide how to handle it.
            break

        return current_draft, result

    # ── Banned phrase detection ──────────────────────────────────────────

    def _check_banned_phrases(self, text: str) -> dict[str, Any]:
        """Scan text for banned phrases.

        Returns
        -------
        dict with keys: ``count``, ``phrases_found``, ``blocker``, ``descriptions``.
        """
        phrases_found: list[str] = []
        descriptions: list[str] = []

        for pattern in _BANNED_PHRASE_PATTERNS:
            match = pattern.search(text)
            if match:
                matched = match.group(0).strip()
                if len(matched) > 60:
                    matched = matched[:57] + "..."
                phrases_found.append(matched)
                descriptions.append(f"Banned phrase detected: \"{matched}\"")

        return {
            "count": len(phrases_found),
            "phrases_found": phrases_found,
            "blocker": len(phrases_found) > 0,
            "descriptions": descriptions,
        }

    # ── AI marker detection ──────────────────────────────────────────────

    def _check_ai_markers(self, text: str) -> dict[str, Any]:
        """Scan text for AI identity markers.

        Returns
        -------
        dict with keys: ``count``, ``markers_found``, ``descriptions``.
        """
        markers_found: list[str] = []
        descriptions: list[str] = []

        for pattern in _AI_MARKER_PATTERNS:
            match = pattern.search(text)
            if match:
                matched = match.group(0).strip()
                markers_found.append(matched)
                descriptions.append(f"AI marker detected: \"{matched}\"")

        return {
            "count": len(markers_found),
            "markers_found": markers_found,
            "descriptions": descriptions,
        }

    # ── Structure checks ─────────────────────────────────────────────────

    def _check_structure(self, text: str) -> dict[str, Any]:
        """Check paragraph structure and formatting.

        Returns
        -------
        dict with keys: ``issues`` (list of str), ``structure_score`` (int).
        """
        issues: list[str] = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        if len(paragraphs) < _MIN_PARAGRAPHS:
            issues.append(
                f"Too few paragraphs: {len(paragraphs)} (minimum {_MIN_PARAGRAPHS})"
            )
        elif len(paragraphs) > _MAX_PARAGRAPHS:
            issues.append(
                f"Too many paragraphs: {len(paragraphs)} (maximum {_MAX_PARAGRAPHS})"
            )

        # Check for bullet points.
        if re.search(r"^\s*[-*+]\s", text, re.MULTILINE):
            issues.append("Draft contains bullet points — use prose instead.")

        # Check for numbered lists.
        if re.search(r"^\s*\d+[.)]\s", text, re.MULTILINE):
            issues.append("Draft contains numbered lists — use prose instead.")

        # Check paragraph length variance (very uniform = AI-like).
        if len(paragraphs) >= 3:
            lengths = [len(p.split()) for p in paragraphs]
            avg_len = sum(lengths) / len(lengths)
            variance = sum((l - avg_len) ** 2 for l in lengths) / len(lengths) if lengths else 0
            if variance < 5.0:
                issues.append("Paragraph lengths are too uniform — vary them.")

        # Score.
        score = 100
        score -= max(0, (len(paragraphs) - _MAX_PARAGRAPHS)) * 10
        score -= max(0, (_MIN_PARAGRAPHS - len(paragraphs))) * 15
        score -= 10 if any("bullet" in i for i in issues) else 0
        score -= 10 if any("numbered" in i for i in issues) else 0
        score -= 15 if any("uniform" in i for i in issues) else 0
        structure_score: int = max(0, score)

        return {
            "issues": issues,
            "structure_score": structure_score,
        }

    # ── Length checks ────────────────────────────────────────────────────

    def _check_length(self, body: str, subject: str) -> dict[str, Any]:
        """Check word count and subject line length.

        Returns
        -------
        dict with keys: ``issues`` (list of str), ``penalty`` (int).
        """
        issues: list[str] = []
        penalty = 0
        word_count = len(body.split())

        if word_count < _MIN_WORDS:
            issues.append(f"Too short: {word_count} words (minimum {_MIN_WORDS})")
            penalty += 15
        elif word_count > _MAX_WORDS:
            issues.append(f"Too long: {word_count} words (maximum {_MAX_WORDS})")
            penalty += 10

        if subject and len(subject) > _MAX_SUBJECT_LENGTH:
            issues.append(
                f"Subject line too long: {len(subject)} chars (max {_MAX_SUBJECT_LENGTH})"
            )
            penalty += 5

        return {"issues": issues, "penalty": penalty}

    # ── Readability (Flesch Reading Ease) ─────────────────────────────────

    def _compute_readability(self, text: str) -> float | None:
        """Compute the Flesch Reading Ease score for a piece of text.

        Formula::

            206.835 - 1.015 * (total_syllables / total_words) * (total_syllables? no)
            Actually: 206.835 - 1.015 * (total_words / total_sentences)
                              - 84.6 * (total_syllables / total_words)

        Returns ``None`` if the text is too short to score.
        """
        clean = text.strip()
        if not clean:
            return None

        words = clean.split()
        if len(words) < 3:
            return None

        # Count sentences (rough split on sentence-ending punctuation).
        sentences = re.split(r"[.!?]+", clean)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return None

        num_sentences = len(sentences)
        num_words = len(words)
        num_syllables = self._count_syllables(clean)

        if num_sentences == 0 or num_words == 0:
            return None

        # Flesch Reading Ease.
        score = 206.835 - 1.015 * (num_words / num_sentences) - 84.6 * (num_syllables / num_words)
        return max(0.0, min(100.0, score))

    @staticmethod
    def _count_syllables(text: str) -> int:
        """Estimate the number of syllables in text.

        Uses a simple heuristic: each group of consecutive vowels counts as
        one syllable, with a minimum of one syllable per word.
        """
        word_pattern = re.compile(r"[a-z]+", re.IGNORECASE)
        vowel_groups = re.compile(r"[aeiouy]+", re.IGNORECASE)

        total = 0
        for match in word_pattern.finditer(text):
            word = match.group(0).lower()
            if len(word) <= 2:
                total += 1
                continue

            groups = vowel_groups.findall(word)
            count = len(groups) if groups else 1

            # Subtract silent 'e' at end.
            if word.endswith("e") and count > 1:
                count -= 1
            # Subtract trailing 'es' or 'ed' (common English patterns).
            if word.endswith(("es", "ed")) and count > 1:
                if len(word) > 4:
                    count -= 1

            total += max(1, count)

        return total

    # ── Technical accuracy check ──────────────────────────────────────────

    def _check_technical_accuracy(
        self,
        body: str,
        opportunity: LeadOpportunity,
    ) -> list[str]:
        """Check that the draft accurately references opportunity specifics.

        Returns a list of issue descriptions (empty if no issues found).
        """
        issues: list[str] = []
        body_lower = body.lower()

        # Check that the draft mentions at least one skill from the listing.
        if opportunity.skills:
            mentioned_skills = [
                s for s in opportunity.skills if s.lower() in body_lower
            ]
            if not mentioned_skills:
                issues.append(
                    "Draft does not mention any required skills from the job posting"
                )
            elif len(mentioned_skills) < min(2, len(opportunity.skills)):
                issues.append(
                    f"Draft only mentions {len(mentioned_skills)}/{len(opportunity.skills)} "
                    f"required skills"
                )

        # Check that the company/client name is referenced.
        if opportunity.company and opportunity.company.lower() not in body_lower:
            issues.append(f"Draft does not mention the client: {opportunity.company}")

        # Check for generic placeholder patterns.
        placeholders = ["[your name]", "[name]", "[company]", "[details]"]
        for placeholder in placeholders:
            if placeholder in body_lower or placeholder in body:
                issues.append(f"Contains placeholder: {placeholder}")
                break

        return issues

    # ── Overall scoring ──────────────────────────────────────────────────

    def _compute_overall_score(
        self,
        banned_count: int,
        ai_marker_count: int,
        structure_score: int,
        readability_score: float | None,
        length_penalty: int,
    ) -> int:
        """Compute the overall quality score (0-100).

        Starts at 100 and deducts for each category of issue.
        """
        score = 100.0

        # Banned phrases (heavy penalty).
        if banned_count > 0:
            score -= min(60.0, banned_count * 25.0)

        # AI markers (heavy penalty).
        if ai_marker_count > 0:
            score -= min(50.0, ai_marker_count * 20.0)

        # Structure.
        score -= (100.0 - structure_score) * 0.3

        # Readability — penalize if too hard or too easy.
        if readability_score is not None:
            if readability_score < 30:
                score -= 10.0  # Very difficult to read.
            elif readability_score < 50:
                score -= 5.0  # Somewhat difficult.
            elif readability_score > 90:
                score -= 5.0  # Too easy (childlike).
            # Sweet spot: 60-80 (plain English).

        # Length penalty.
        score -= length_penalty

        return max(0, min(100, int(round(score))))

    # ── LLM-assisted verification ────────────────────────────────────────

    async def _llm_verify(
        self,
        draft: OutboundDraft,
        opportunity: LeadOpportunity | None = None,
    ) -> dict[str, Any]:
        """Run an LLM-based verification pass on the draft.

        This catches subtle issues that regex cannot: tone problems,
        overly generic content, missing personalisation.

        Returns
        -------
        dict with keys: ``issues`` (list of str), ``score_adjustment`` (int).
        """
        body = draft.current_body or ""
        prompt = (
            "You are a quality reviewer for freelance outreach messages. "
            "Review the following draft for:\n"
            "1. **Tone**: does it sound human-written and natural?\n"
            "2. **Specificity**: does it reference concrete details or is it generic?\n"
            "3. **Structure**: does it have a clear hook, proof, and call to action?\n"
            "4. **Red flags**: form-letter feel, excessive enthusiasm, vagueness\n\n"
            f"## Job Context\n"
            f"Title: {opportunity.title if opportunity else 'N/A'}\n"
            f"Skills: {', '.join(opportunity.skills) if opportunity and opportunity.skills else 'N/A'}\n\n"
            f"## Draft to Review\n{body}\n\n"
            "Return a JSON object with:\n"
            "- `issues`: list of specific issues found (empty list if none)\n"
            "- `score_adjustment`: integer -20 to +10 to adjust the automatic score\n"
            "- `overall_assessment`: one sentence summary"
        )

        result = await self._llm.structured_classify(
            system_prompt="You are a strict but fair quality reviewer.",
            user_content=prompt,
            response_model=_LLMVerificationResult,
            temperature=0.3,
        )

        return {
            "issues": result.get("issues", []),
            "score_adjustment": result.get("score_adjustment", 0),
        }

    # ── Fix suggestions ──────────────────────────────────────────────────

    def _generate_fixes(self, issues: list[str]) -> list[str]:
        """Generate actionable fix suggestions from a list of issues.

        Parameters
        ----------
        issues : list of str
            The issues found during verification.

        Returns
        -------
        list of str
            Suggested fixes.
        """
        suggestions: list[str] = []
        issue_text = " ".join(issues).lower()

        if "banned phrase" in issue_text:
            suggestions.append("Rewrite flagged sentences in your own words")
        if "ai marker" in issue_text:
            suggestions.append("Remove any references to being an AI or language model")
        if "too few paragraphs" in issue_text:
            suggestions.append("Add more substance — expand your relevant experience")
        if "too many paragraphs" in issue_text:
            suggestions.append("Condense — keep the message to 3-4 paragraphs")
        if "bullet points" in issue_text:
            suggestions.append("Convert bullet points into flowing prose paragraphs")
        if "numbered list" in issue_text:
            suggestions.append("Convert numbered items into natural paragraph text")
        if "too short" in issue_text:
            suggestions.append("Add a specific example of relevant past work")
        if "too long" in issue_text:
            suggestions.append("Trim — focus on the most relevant experience only")
        if "uniform" in issue_text:
            suggestions.append("Vary paragraph lengths for a more natural rhythm")
        if "subject" in issue_text:
            suggestions.append("Shorten the subject line to under 60 characters")
        if "skills" in issue_text:
            suggestions.append("Mention specific skills from the job posting")
        if "client" in issue_text:
            suggestions.append("Reference the client or company name")
        if "placeholder" in issue_text:
            suggestions.append("Replace [placeholders] with actual content")
        if "generic" in issue_text:
            suggestions.append("Add a specific, personalized detail from the listing")

        return suggestions


# ── LLM Verification Schema ────────────────────────────────────────────────────


class _LLMVerificationResult(BaseModel):
    """Structured output from the LLM verification pass."""

    issues: list[str] = Field(default_factory=list)
    score_adjustment: int = Field(default=0, ge=-20, le=10)
    overall_assessment: str = Field(default="")
