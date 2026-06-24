"""Domain models for freelance opportunities, scoring results, and outreach drafts.

All models are **Pydantic v2** models and are JSON-serialisable by default
for CLI and API output.
"""

from __future__ import annotations as _annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ── Status Enum ──────────────────────────────────────────────────────────────


class LeadStatus(StrEnum):
    """Lifecycle status of a lead opportunity.

    Follows the pipeline progression:
        DISCOVERED → QUALIFIED → DRAFTED → REVIEWED → SUBMITTED
    with ARCHIVED and REJECTED as terminal states that can be entered from
    any non-terminal state.
    """

    DISCOVERED = "discovered"
    """Newly found opportunity — no processing has occurred yet."""

    QUALIFIED = "qualified"
    """Passed initial qualification checks (skill match, budget, etc.)."""

    DRAFTED = "drafted"
    """An outreach draft has been generated for this opportunity."""

    REVIEWED = "reviewed"
    """Human reviewed and approved the outreach draft."""

    SUBMITTED = "submitted"
    """Outreach was sent to the client / platform."""

    ARCHIVED = "archived"
    """Terminal — opportunity was set aside (e.g. duplicate, expired)."""

    REJECTED = "rejected"
    """Terminal — opportunity did not pass qualification or was declined."""


# ── Lead Opportunity ─────────────────────────────────────────────────────────


class LeadOpportunity(BaseModel):
    """A discovered freelance opportunity / job listing from any platform.

    This is the central domain entity of the system.  Every opportunity flows
    through the pipeline from discovery to submission (or rejection).
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Short unique identifier (12 hex chars, URL-safe)."""

    platform: str
    """Name of the source platform (e.g. ``"upwork"``, ``"linkedin"``)."""

    platform_job_id: str
    """Platform-native job/listing identifier (used for deduplication)."""

    title: str
    """Job title as displayed on the platform."""

    company: str | None = Field(default=None)
    """Hiring company or client name, if visible."""

    description: str
    """Full description text of the opportunity."""

    budget_min: float | None = Field(default=None, ge=0)
    """Minimum budget / rate in USD (if available)."""

    budget_max: float | None = Field(default=None, ge=0)
    """Maximum budget / rate in USD (if available)."""

    currency: str = Field(default="USD", min_length=3, max_length=3)
    """ISO 4217 currency code."""

    skills: list[str] = Field(default_factory=list)
    """List of skill keywords mentioned in the listing."""

    posted_date: datetime | None = Field(default=None)
    """When the opportunity was posted on the platform."""

    url: str | None = Field(default=None)
    """Direct URL to the listing on the source platform."""

    location: str | None = Field(default=None)
    """Location string — remote, city, country, or ``None``."""

    status: LeadStatus = Field(default=LeadStatus.DISCOVERED)
    """Current pipeline status."""

    score: int | None = Field(default=None)
    """Qualification score (0-100).  ``None`` until qualified."""

    notes: str | None = Field(default=None)
    """Free-text notes (human or system)."""

    raw_data: dict[str, Any] = Field(default_factory=dict)
    """Raw platform data preserved for debugging / reprocessing."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """Timestamp when this record was first created."""

    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """Timestamp of the most recent update."""

    # ── validators ──────────────────────────────────────────────────────

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            return max(0, min(100, v))
        return v

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return v.strip()

    # ── helpers ─────────────────────────────────────────────────────────

    def qualified(self) -> bool:
        """Return *True* if this opportunity has a qualification score >= 60."""
        return self.score is not None and self.score >= 60

    def is_terminal(self) -> bool:
        """Return *True* if the status is one of the terminal states."""
        return self.status in (LeadStatus.ARCHIVED, LeadStatus.REJECTED)

    def touch(self) -> None:
        """Update *updated_at* to the current UTC time."""
        self.updated_at = datetime.now(UTC)


# ── Scoring Result ───────────────────────────────────────────────────────────


class LeadScoringResult(BaseModel):
    """The structured output of a qualification / scoring evaluation.

    This model is typically returned by an LLM call and then merged into
    a :class:`LeadOpportunity` record.
    """

    qualified: bool
    """Whether this opportunity is worth pursuing."""

    score: int = Field(..., ge=0, le=100)
    """Overall qualification score (0-100)."""

    skill_match_score: int = Field(default=50, ge=0, le=100)
    """How well the required skills match the freelancer's profile."""

    budget_fit_score: int = Field(default=50, ge=0, le=100)
    """How well the budget aligns with expectations."""

    clarity_score: int = Field(default=50, ge=0, le=100)
    """How detailed and actionable the listing description is."""

    reasoning: str
    """1-2 sentence justification for the qualification decision."""

    risks: list[str] = Field(default_factory=list)
    """Potential issues to watch out for."""


# ── Outreach Draft ───────────────────────────────────────────────────────────


class OutboundDraft(BaseModel):
    """A generated outreach message (proposal / cover letter) for an opportunity.

    Supports versioning so the human-in-the-loop can iterate on drafts
    while preserving history.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Short unique identifier for this draft."""

    opportunity_id: str
    """The :attr:`LeadOpportunity.id` this draft belongs to."""

    versions: list[str] = Field(default_factory=list)
    """Ordered list of draft body texts — index 0 is the first generation."""

    current_version_index: int = Field(default=0, ge=0)
    """Index into *versions* indicating the current active draft."""

    subject: str | None = Field(default=None)
    """Message subject or first line (if applicable to the platform)."""

    approved: bool = Field(default=False)
    """Has the human-in-the-loop approved this draft?"""

    human_edited: bool = Field(default=False)
    """Did a human manually edit any version of this draft?"""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When this draft was first generated."""

    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When this draft was last modified."""

    # ── computed properties ─────────────────────────────────────────────

    @property
    def current_body(self) -> str | None:
        """Return the current active version of the draft body, if any."""
        if not self.versions:
            return None
        idx = min(self.current_version_index, len(self.versions) - 1)
        return self.versions[idx]

    @property
    def version_count(self) -> int:
        """Return the total number of versions stored."""
        return len(self.versions)

    # ── helpers ─────────────────────────────────────────────────────────

    def add_version(self, body: str, *, set_current: bool = True) -> int:
        """Append a new version of the draft body.

        Parameters
        ----------
        body : str
            The draft body text.
        set_current : bool, optional
            If *True* (default), advance *current_version_index* to this
            version.

        Returns
        -------
        int
            The index of the newly added version.

        """
        self.versions.append(body)
        idx = len(self.versions) - 1
        if set_current:
            self.current_version_index = idx
        self.updated_at = datetime.now(UTC)
        return idx

    def approve(self) -> None:
        """Mark the draft as approved."""
        self.approved = True
        self.updated_at = datetime.now(UTC)
