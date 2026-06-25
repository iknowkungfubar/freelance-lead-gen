"""Target profile definition and matching engine.

Defines the :class:`TargetProfile` that captures a freelancer's ideal
opportunity profile and :class:`ProfileMatcher` that scores opportunities
against that profile using weighted, multi-dimensional matching.
"""

from __future__ import annotations as _annotations

import json
import os
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from freelance_lead_gen.models.opportunity import LeadOpportunity

logger = structlog.get_logger(__name__)

# ── Type aliases ───────────────────────────────────────────────────────────────

ExperienceLevel = Literal["junior", "mid", "senior", "lead", "any"]
"""Experience level categories."""


# ── Target Profile Model ───────────────────────────────────────────────────────


class TargetProfile(BaseModel):
    """Describes the freelancer's ideal opportunity target.

    This is the reference against which every discovered opportunity is
    compared.  Profiles can be loaded from and saved to YAML or JSON files.
    """

    name: str = Field(default="default", description="Human-readable profile name.")
    skills: list[str] = Field(
        default_factory=lambda: [
            "python",
            "llm",
            "ai",
            "machine learning",
            "rag",
            "fine-tuning",
            "automation",
            "langchain",
            "pytorch",
            "tensorflow",
            "nlp",
            "computer vision",
            "data pipeline",
            "backend",
            "api development",
            "fastapi",
            "postgresql",
            "docker",
            "kubernetes",
            "cloud",
            "aws",
            "gcp",
            "azure",
        ],
        description="Skills the freelancer offers, in order of proficiency.",
    )
    industries: list[str] = Field(
        default_factory=lambda: [
            "technology",
            "healthcare",
            "finance",
            "e-commerce",
            "education",
            "saas",
        ],
        description="Target industries the freelancer wants to work in.",
    )
    budget_range: tuple[float | None, float | None] = Field(
        default=(50.0, None),
        description="Acceptable (min, max) budget/rate in USD per hour. "
        "``None`` means no lower/upper bound.",
    )
    experience_level: ExperienceLevel = Field(
        default="senior",
        description="Minimum experience level expected for opportunities.",
    )
    preferred_platforms: list[str] = Field(
        default_factory=lambda: ["upwork", "linkedin", "freelancer"],
        description="Priority-ordered list of preferred platforms.",
    )
    excluded_keywords: list[str] = Field(
        default_factory=lambda: [
            "unpaid",
            "equity only",
            "internship",
            "volunteer",
            "spec work",
            "contest",
        ],
        description="Keywords that disqualify an opportunity regardless of other scores.",
    )
    preferred_keywords: list[str] = Field(
        default_factory=lambda: [
            "remote",
            "contract",
            "freelance",
            "project-based",
            "asap",
            "long-term",
        ],
        description="Keywords that boost an opportunity's score.",
    )
    description_min_length: int = Field(
        default=100,
        ge=0,
        description="Minimum description length (chars) to consider a listing detailed enough.",
    )
    max_days_since_posted: int | None = Field(
        default=30,
        ge=1,
        description="Max age in days for a listing to be considered fresh. "
        "``None`` disables freshness filtering.",
    )

    @field_validator("experience_level")
    @classmethod
    def _validate_experience(cls, v: str) -> str:
        allowed = {"junior", "mid", "senior", "lead", "any"}
        if v.lower() not in allowed:
            msg = f"experience_level must be one of {allowed}, got {v!r}"
            raise ValueError(msg)
        return v.lower()

    # ── Serialisation helpers ────────────────────────────────────────────

    def to_yaml(self, path: str | Path) -> Path:
        """Serialize the profile to a YAML file.

        Parameters
        ----------
        path : str or Path
            Destination file path.

        Returns
        -------
        Path
            The resolved path the profile was written to.

        """
        resolved = Path(path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                self.model_dump(mode="json"),
                fh,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        logger.info("profile.saved", path=str(resolved))
        return resolved

    def to_json(self, path: str | Path) -> Path:
        """Serialize the profile to a JSON file.

        Parameters
        ----------
        path : str or Path
            Destination file path.

        Returns
        -------
        Path
            The resolved path the profile was written to.

        """
        resolved = Path(path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("w", encoding="utf-8") as fh:
            json.dump(self.model_dump(mode="json"), fh, indent=2, ensure_ascii=False)
        logger.info("profile.saved_json", path=str(resolved))
        return resolved

    @classmethod
    def from_yaml(cls, path: str | Path) -> TargetProfile:
        """Load a profile from a YAML file.

        Parameters
        ----------
        path : str or Path
            Path to the YAML file.

        Returns
        -------
        TargetProfile

        """
        resolved = Path(path).resolve()
        with resolved.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        profile = cls.model_validate(data)
        logger.info("profile.loaded_yaml", path=str(resolved), name=profile.name)
        return profile

    @classmethod
    def from_json(cls, path: str | Path) -> TargetProfile:
        """Load a profile from a JSON file.

        Parameters
        ----------
        path : str or Path
            Path to the JSON file.

        Returns
        -------
        TargetProfile

        """
        resolved = Path(path).resolve()
        with resolved.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        profile = cls.model_validate(data)
        logger.info("profile.loaded_json", path=str(resolved), name=profile.name)
        return profile

    @classmethod
    def default(cls) -> TargetProfile:
        """Return the default profile, optionally overridden by a config path.

        Checks for a ``TARGET_PROFILE_PATH`` environment variable and loads
        from that file if it exists, otherwise returns the in-code defaults.
        """
        env_path = os.environ.get("TARGET_PROFILE_PATH")
        if env_path:
            resolved = Path(env_path).resolve()
            if resolved.is_file():
                if resolved.suffix.lower() in (".yaml", ".yml"):
                    return cls.from_yaml(resolved)
                return cls.from_json(resolved)
            logger.warning("profile.env_path_not_found", path=env_path)
        return cls()


# ── Matching Weights ───────────────────────────────────────────────────────────


class MatchingWeights(BaseModel):
    """Configurable weights for each dimension of the scoring algorithm.

    All weights should sum to 100 to produce a 0-100 final score, but the
    matcher normalises them in case they don't.
    """

    skill_match: float = Field(default=35.0, ge=0.0, le=100.0)
    """Weight for skill overlap."""
    budget_fit: float = Field(default=20.0, ge=0.0, le=100.0)
    """Weight for budget/rate alignment."""
    industry_relevance: float = Field(default=15.0, ge=0.0, le=100.0)
    """Weight for industry match."""
    keyword_match: float = Field(default=15.0, ge=0.0, le=100.0)
    """Weight for preferred vs excluded keywords."""
    freshness: float = Field(default=15.0, ge=0.0, le=100.0)
    """Weight for posting freshness (age)."""

    @property
    def total(self) -> float:
        """Return the sum of all weights."""
        return (
            self.skill_match
            + self.budget_fit
            + self.industry_relevance
            + self.keyword_match
            + self.freshness
        )


# ── Profile Matcher ────────────────────────────────────────────────────────────


class ProfileMatcher:
    """Compares :class:`LeadOpportunity` records against a :class:`TargetProfile`.

    Produces a 0-100 match score across multiple weighted dimensions along
    with a detailed breakdown for each opportunity.

    Parameters
    ----------
    profile : TargetProfile or None
        The target profile to match against.  Defaults to
        :meth:`TargetProfile.default`.
    weights : MatchingWeights or None
        Scoring weights.  Defaults to all-default weights.

    """

    def __init__(
        self,
        profile: TargetProfile | None = None,
        weights: MatchingWeights | None = None,
    ) -> None:
        self._profile: TargetProfile = profile or TargetProfile.default()
        self._weights: MatchingWeights = weights or MatchingWeights()
        self._total_weight: float = self._weights.total

        # Pre-compute lowercase sets for fast matching.
        self._profile_skills_lower: set[str] = {
            s.strip().lower() for s in self._profile.skills if s.strip()
        }
        self._profile_industries_lower: set[str] = {
            i.strip().lower() for i in self._profile.industries if i.strip()
        }
        self._excluded_lower: set[str] = {
            k.strip().lower() for k in self._profile.excluded_keywords if k.strip()
        }
        self._preferred_lower: set[str] = {
            k.strip().lower() for k in self._profile.preferred_keywords if k.strip()
        }

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def profile(self) -> TargetProfile:
        """The current target profile."""
        return self._profile

    @profile.setter
    def profile(self, new_profile: TargetProfile) -> None:
        self._profile = new_profile
        # Recompute caches.
        self._profile_skills_lower = {s.strip().lower() for s in new_profile.skills if s.strip()}
        self._profile_industries_lower = {
            i.strip().lower() for i in new_profile.industries if i.strip()
        }
        self._excluded_lower = {
            k.strip().lower() for k in new_profile.excluded_keywords if k.strip()
        }
        self._preferred_lower = {
            k.strip().lower() for k in new_profile.preferred_keywords if k.strip()
        }

    @property
    def weights(self) -> MatchingWeights:
        """The current scoring weights."""
        return self._weights

    # ── Public API ───────────────────────────────────────────────────────

    def score_opportunity(self, opportunity: LeadOpportunity) -> dict[str, Any]:
        """Score a single opportunity against the target profile.

        Parameters
        ----------
        opportunity : LeadOpportunity
            The opportunity to score.

        Returns
        -------
        dict
            Scoring breakdown with keys:

            - ``overall_score`` (float): 0-100 weighted score.
            - ``skill_match_score`` (float): 0-100.
            - ``budget_fit_score`` (float): 0-100.
            - ``industry_score`` (float): 0-100.
            - ``keyword_score`` (float): 0-100.
            - ``freshness_score`` (float): 0-100.
            - ``disqualified`` (bool): *True* if excluded keywords found.
            - ``disqualification_reason`` (str or None): why disqualified.
            - ``diagnostics`` (dict): per-dimension raw values.

        """
        diagnostics: dict[str, Any] = {}
        disqualified = False
        disqualification_reason: str | None = None

        # ── Check excluded keywords ──────────────────────────────────────
        desc_lower = (opportunity.description or "").lower()
        title_lower = (opportunity.title or "").lower()
        combined_text = f"{title_lower} {desc_lower}"

        for kw in self._excluded_lower:
            if kw in combined_text:
                disqualified = True
                disqualification_reason = f"Contains excluded keyword: {kw}"
                logger.info(
                    "matcher.disqualified",
                    opportunity_id=opportunity.id,
                    reason=disqualification_reason,
                )
                break

        # ── Skill match (fuzzy) ──────────────────────────────────────────
        opportunity_skills_lower = {s.strip().lower() for s in opportunity.skills if s.strip()}
        # Also extract skills from title and description.
        text_skills = _extract_skill_mentions(combined_text, self._profile_skills_lower)
        all_opp_skills = opportunity_skills_lower | text_skills

        skill_match_score = self._compute_skill_match(all_opp_skills)
        diagnostics["matched_skills"] = sorted(
            s
            for s in self._profile_skills_lower
            if s in all_opp_skills or any(_fuzzy_match(s, opp_s) for opp_s in all_opp_skills)
        )
        diagnostics["opp_skills_raw"] = sorted(opportunity_skills_lower)
        diagnostics["text_skills_extracted"] = sorted(text_skills)

        # ── Budget fit ───────────────────────────────────────────────────
        budget_fit_score = self._compute_budget_fit(opportunity)
        diagnostics["budget_min"] = opportunity.budget_min
        diagnostics["budget_max"] = opportunity.budget_max
        diagnostics["profile_budget_min"] = self._profile.budget_range[0]
        diagnostics["profile_budget_max"] = self._profile.budget_range[1]

        # ── Industry relevance ───────────────────────────────────────────
        industry_score = self._compute_industry_relevance(combined_text)
        diagnostics["matched_industries"] = self._detect_industries(combined_text)
        diagnostics["profile_industries"] = list(self._profile_industries_lower)

        # ── Keyword match ────────────────────────────────────────────────
        keyword_score = self._compute_keyword_score(combined_text)
        diagnostics["found_preferred"] = sorted(
            k for k in self._preferred_lower if k in combined_text
        )
        diagnostics["found_excluded"] = sorted(
            k for k in self._excluded_lower if k in combined_text
        )

        # ── Freshness ────────────────────────────────────────────────────
        freshness_score = self._compute_freshness(opportunity)
        diagnostics["posted_date"] = (
            opportunity.posted_date.isoformat() if opportunity.posted_date else None
        )
        diagnostics["days_old"] = self._days_since_posted(opportunity)
        diagnostics["max_days"] = self._profile.max_days_since_posted

        # ── Overall weighted score ───────────────────────────────────────┐
        if self._total_weight == 0:
            overall_score = 0.0
        else:
            overall_score = (
                skill_match_score * self._weights.skill_match
                + budget_fit_score * self._weights.budget_fit
                + industry_score * self._weights.industry_relevance
                + keyword_score * self._weights.keyword_match
                + freshness_score * self._weights.freshness
            ) / self._total_weight

        # Round to integer for consistency with existing models.
        overall_score = round(overall_score)

        return {
            "overall_score": overall_score,
            "skill_match_score": round(skill_match_score),
            "budget_fit_score": round(budget_fit_score),
            "industry_score": round(industry_score),
            "keyword_score": round(keyword_score),
            "freshness_score": round(freshness_score),
            "disqualified": disqualified,
            "disqualification_reason": disqualification_reason,
            "diagnostics": diagnostics,
        }

    def score_batch(
        self,
        opportunities: list[LeadOpportunity],
    ) -> list[dict[str, Any]]:
        """Score multiple opportunities against the target profile.

        Parameters
        ----------
        opportunities : list of LeadOpportunity
            The opportunities to score.

        Returns
        -------
        list of dict
            One scoring result per opportunity, in the same order.

        """
        return [self.score_opportunity(opp) for opp in opportunities]

    # ── Dimension scoring ───────────────────────────────────────────────

    def _compute_skill_match(self, opp_skills: set[str]) -> float:
        """Compute skill match score (0-100).

        Uses both exact matches and fuzzy similarity.  A skill is "matched"
        if it appears exactly in the opportunity skills or if there is a
        fuzzy match (ratio >= 0.8).
        """
        if not self._profile_skills_lower:
            return 50.0  # Neutral — no profile skills defined.

        matched = 0
        for profile_skill in self._profile_skills_lower:
            if profile_skill in opp_skills or any(
                _fuzzy_match(profile_skill, opp_skill) for opp_skill in opp_skills
            ):
                matched += 1

        raw_ratio = matched / len(self._profile_skills_lower)

        # Boost for known high-value skill combinations.
        has_core_ai = bool(
            {"llm", "ai", "machine learning", "nlp", "rag", "fine-tuning"}
            & self._profile_skills_lower
            & opp_skills
        )
        has_backend = bool(
            {"python", "backend", "api development", "fastapi"}
            & self._profile_skills_lower
            & opp_skills
        )

        boost = 0.0
        if has_core_ai and has_backend:
            boost = 10.0  # Full-stack AI is highly valuable.

        return min(100.0, raw_ratio * 100.0 + boost)

    def _compute_budget_fit(self, opportunity: LeadOpportunity) -> float:
        """Compute budget fit score (0-100).

        Compares the opportunity's budget range with the profile's target
        range using interval overlap.
        """
        p_min, p_max = self._profile.budget_range
        o_min = opportunity.budget_min
        o_max = opportunity.budget_max

        # If neither profile nor opportunity specifies a budget, neutral.
        if p_min is None and p_max is None:
            return 50.0

        # If the opportunity has no budget info, partial score.
        if o_min is None and o_max is None:
            return 40.0

        # Resolve ranges: single value means both min and max are that value.
        opp_low = o_min if o_min is not None else (o_max or 0)
        opp_high = o_max if o_max is not None else (o_min or float("inf"))

        # Calculate interval overlap.
        overlap_min = max(p_min if p_min is not None else 0, opp_low)
        overlap_max = min(p_max if p_max is not None else float("inf"), opp_high)

        if overlap_min > overlap_max:
            return 0.0  # No overlap.

        # Compute overlap proportion relative to the opportunity's range.
        opp_range = opp_high - opp_low
        if opp_range <= 0:
            return 100.0  # Exact match with a single value.

        overlap = overlap_max - overlap_min
        ratio = overlap / opp_range
        return min(100.0, ratio * 100.0 + 10.0)  # +10 base for proximity

    def _compute_industry_relevance(self, combined_text: str) -> float:
        """Compute industry relevance score (0-100)."""
        if not self._profile_industries_lower:
            return 50.0

        matched_industries = self._detect_industries(combined_text)

        if not matched_industries:
            return 30.0  # Low but not zero — industry-neutral roles exist.

        ratio = len(matched_industries) / len(self._profile_industries_lower)
        return min(100.0, ratio * 100.0 + 20.0)

    def _compute_keyword_score(self, combined_text: str) -> float:
        """Compute keyword match score (0-100).

        Rewards presence of preferred keywords and penalises excluded ones.
        """
        preferred_found = sum(1 for k in self._preferred_lower if k in combined_text)
        excluded_found = sum(1 for k in self._excluded_lower if k in combined_text)

        preferred_ratio = (
            preferred_found / len(self._preferred_lower) if self._preferred_lower else 0.0
        )
        excluded_ratio = excluded_found / len(self._excluded_lower) if self._excluded_lower else 0.0

        score = 50.0 + (preferred_ratio * 40.0) - (excluded_ratio * 60.0)
        return max(0.0, min(100.0, score))

    def _compute_freshness(self, opportunity: LeadOpportunity) -> float:
        """Compute freshness score (0-100).

        A listing posted today scores 100; older listings decay linearly.
        Listings past *max_days_since_posted* score 0.
        """
        max_days = self._profile.max_days_since_posted
        if max_days is None:
            return 100.0  # Freshness disabled.

        days_old = self._days_since_posted(opportunity)
        if days_old is None:
            return 50.0  # Unknown age — neutral.

        if days_old >= max_days:
            return 0.0

        return max(0.0, 100.0 * (1.0 - days_old / max_days))

    # ── Internal helpers ─────────────────────────────────────────────────

    def _days_since_posted(self, opportunity: LeadOpportunity) -> int | None:
        """Return the number of days since the opportunity was posted."""
        if opportunity.posted_date is None:
            return None
        delta = datetime.now(UTC) - opportunity.posted_date
        return max(0, delta.days)

    def _detect_industries(self, text: str) -> set[str]:
        """Detect which of the profile's target industries are mentioned."""
        detected: set[str] = set()
        for industry in self._profile_industries_lower:
            if industry in text:
                detected.add(industry)
        return detected


# ── Fuzzy matching ─────────────────────────────────────────────────────────────


def _fuzzy_match(a: str, b: str, threshold: float = 0.8) -> bool:
    """Return *True* if *a* and *b* are similar enough via fuzzy matching.

    Uses :class:`difflib.SequenceMatcher` ratio and handles common
    abbreviations and synonyms.

    Parameters
    ----------
    a : str
        First string.
    b : str
        Second string.
    threshold : float
        Similarity threshold (0.0–1.0, default 0.8).

    Returns
    -------
    bool

    """
    # Direct substring containment is an easy win.
    if a in b or b in a:
        return True

    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= threshold:
        return True

    # Handle common abbreviations/synonyms.
    synonyms: dict[str, set[str]] = {
        "ml": {"machine learning", "machine-learning"},
        "ai": {"artificial intelligence"},
        "nlp": {"natural language processing"},
        "llm": {"large language model", "large language models"},
        "rag": {"retrieval augmented generation", "retrieval-augmented generation"},
        "fe": {"frontend", "front-end", "front end"},
        "be": {"backend", "back-end", "back end"},
        "db": {"database", "databases"},
        "k8s": {"kubernetes"},
        "aws": {"amazon web services"},
        "gcp": {"google cloud platform"},
        "orm": {"object relational mapping"},
    }

    for short, full_set in synonyms.items():  # noqa: N806
        if (a == short and b in full_set) or (b == short and a in full_set):
            return True

    return False


def _extract_skill_mentions(text: str, profile_skills: set[str]) -> set[str]:
    """Find profile skills mentioned in free text.

    Parameters
    ----------
    text : str
        The text to search (lowercased).
    profile_skills : set of str
        Profile skill keywords (lowercased).

    Returns
    -------
    set of str
        The subset of profile skills found in the text.

    """
    text_lower = text.lower()
    found: set[str] = set()
    for skill in profile_skills:
        if skill in text_lower:
            found.add(skill)
    return found
