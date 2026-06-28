"""Pipeline state machine — lifecycle models for opportunity processing.

The pipeline orchestrates the flow of opportunities through discovery,
qualification, drafting, human review, and submission.  Each stage is
represented by a :class:`PipelineState` value, and the full context of
a processing run is captured in :class:`PipelineContext`.
"""

from __future__ import annotations as _annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from freelance_lead_gen.models.opportunity import LeadOpportunity


class PipelineState(StrEnum):
    """States of the opportunity processing pipeline.

    These mirror the :class:`~freelance_lead_gen.models.opportunity.LeadStatus`
    lifecycle but at a *pipeline-run* granularity — a pipeline run tracks
    one batch of opportunities through a specific processing stage.
    """

    # ── Pre-processing ──────────────────────────────────────────────────
    PENDING = "pending"
    """Pipeline run is queued and waiting to start."""

    INITIALISED = "initialised"
    """Pipeline run has been set up and is ready to begin."""

    # ── Discovery ────────────────────────────────────────────────────────
    DISCOVERING = "discovering"
    """Actively searching platforms for new opportunities."""

    DISCOVERED = "discovered"
    """New opportunities have been found."""

    DISCOVERY_FAILED = "discovery_failed"
    """Discovery encountered a non-recoverable error."""

    # ── Classification ───────────────────────────────────────────────────
    CLASSIFYING = "classifying"
    """Classifying raw listings into structured :class:`LeadOpportunity` records."""

    CLASSIFIED = "classified"
    """Raw listings have been classified."""

    CLASSIFICATION_FAILED = "classification_failed"
    """Classification step encountered an error."""

    # ── Qualification ────────────────────────────────────────────────────
    QUALIFYING = "qualifying"
    """Evaluating opportunities against qualification criteria."""

    QUALIFIED = "qualified"
    """Oppotunities have been scored and qualified (or rejected)."""

    QUALIFICATION_FAILED = "qualification_failed"
    """Qualification step encountered an error."""

    # ── Drafting ─────────────────────────────────────────────────────────
    DRAFTING = "drafting"
    """Generating personalised outreach drafts."""

    DRAFTED = "drafted"
    """Outreach drafts have been generated."""

    DRAFTING_FAILED = "drafting_failed"
    """Draft generation encountered an error."""

    # ── Human Review ─────────────────────────────────────────────────────
    AWAITING_REVIEW = "awaiting_review"
    """Waiting for human to review and approve (or reject)."""

    REVIEWED = "reviewed"
    """Human has reviewed and approved the draft."""

    REVIEW_SKIPPED = "review_skipped"
    """Human review was skipped (auto-approve or HITL disabled)."""

    REVIEW_TIMEOUT = "review_timeout"
    """Human review timed out."""

    REVIEW_REJECTED = "review_rejected"
    """Human rejected the draft."""

    # ── Submission ───────────────────────────────────────────────────────
    SUBMITTING = "submitting"
    """Submitting the outreach / proposal to the platform."""

    SUBMITTED = "submitted"
    """Outreach was successfully submitted."""

    SUBMISSION_FAILED = "submission_failed"
    """Submission encountered an error."""

    # ── Terminal ─────────────────────────────────────────────────────────
    COMPLETED = "completed"
    """Pipeline run completed successfully (all eligible opps processed)."""

    FAILED = "failed"
    """Pipeline run failed with a fatal error."""

    CANCELLED = "cancelled"
    """Pipeline run was manually cancelled."""


# ── Transition helpers ───────────────────────────────────────────────────────

# Valid transitions expressed as (from_state, to_state) pairs.
# This is used for runtime validation but can also drive a state-machine
# visualisation or documentation generator.
_VALID_TRANSITIONS: frozenset[tuple[PipelineState, PipelineState]] = frozenset(
    {
        (PipelineState.PENDING, PipelineState.INITIALISED),
        (PipelineState.INITIALISED, PipelineState.DISCOVERING),
        (PipelineState.DISCOVERING, PipelineState.DISCOVERED),
        (PipelineState.DISCOVERING, PipelineState.DISCOVERY_FAILED),
        (PipelineState.DISCOVERED, PipelineState.CLASSIFYING),
        (PipelineState.DISCOVERY_FAILED, PipelineState.FAILED),
        (PipelineState.CLASSIFYING, PipelineState.CLASSIFIED),
        (PipelineState.CLASSIFYING, PipelineState.CLASSIFICATION_FAILED),
        (PipelineState.CLASSIFIED, PipelineState.QUALIFYING),
        (PipelineState.CLASSIFICATION_FAILED, PipelineState.FAILED),
        (PipelineState.QUALIFYING, PipelineState.QUALIFIED),
        (PipelineState.QUALIFYING, PipelineState.QUALIFICATION_FAILED),
        (PipelineState.QUALIFIED, PipelineState.DRAFTING),
        (PipelineState.QUALIFICATION_FAILED, PipelineState.FAILED),
        (PipelineState.DRAFTING, PipelineState.DRAFTED),
        (PipelineState.DRAFTING, PipelineState.DRAFTING_FAILED),
        (PipelineState.DRAFTED, PipelineState.AWAITING_REVIEW),
        (PipelineState.DRAFTING_FAILED, PipelineState.FAILED),
        (PipelineState.AWAITING_REVIEW, PipelineState.REVIEWED),
        (PipelineState.AWAITING_REVIEW, PipelineState.REVIEW_SKIPPED),
        (PipelineState.AWAITING_REVIEW, PipelineState.REVIEW_TIMEOUT),
        (PipelineState.AWAITING_REVIEW, PipelineState.REVIEW_REJECTED),
        (PipelineState.REVIEWED, PipelineState.SUBMITTING),
        (PipelineState.REVIEW_SKIPPED, PipelineState.SUBMITTING),
        (PipelineState.REVIEW_TIMEOUT, PipelineState.FAILED),
        (PipelineState.REVIEW_REJECTED, PipelineState.COMPLETED),
        (PipelineState.SUBMITTING, PipelineState.SUBMITTED),
        (PipelineState.SUBMITTING, PipelineState.SUBMISSION_FAILED),
        (PipelineState.SUBMITTED, PipelineState.COMPLETED),
        (PipelineState.SUBMISSION_FAILED, PipelineState.FAILED),
        # Cancellation allowed from any non-terminal state.
        (PipelineState.PENDING, PipelineState.CANCELLED),
        (PipelineState.INITIALISED, PipelineState.CANCELLED),
        (PipelineState.DISCOVERING, PipelineState.CANCELLED),
        (PipelineState.DISCOVERED, PipelineState.CANCELLED),
        (PipelineState.CLASSIFYING, PipelineState.CANCELLED),
        (PipelineState.CLASSIFIED, PipelineState.CANCELLED),
        (PipelineState.QUALIFYING, PipelineState.CANCELLED),
        (PipelineState.QUALIFIED, PipelineState.CANCELLED),
        (PipelineState.DRAFTING, PipelineState.CANCELLED),
        (PipelineState.DRAFTED, PipelineState.CANCELLED),
        (PipelineState.AWAITING_REVIEW, PipelineState.CANCELLED),
        (PipelineState.REVIEWED, PipelineState.CANCELLED),
        (PipelineState.SUBMITTING, PipelineState.CANCELLED),
        (PipelineState.SUBMITTED, PipelineState.CANCELLED),
        (PipelineState.FAILED, PipelineState.CANCELLED),
        (PipelineState.CANCELLED, PipelineState.CANCELLED),
    }
)


def is_valid_transition(current: PipelineState, next_state: PipelineState) -> bool:
    """Check whether transitioning from *current* to *next_state* is valid.

    Parameters
    ----------
    current : PipelineState
        The current state.
    next_state : PipelineState
        The desired next state.

    Returns
    -------
    bool
        *True* if the transition is defined in the state machine.

    """
    return (current, next_state) in _VALID_TRANSITIONS


# ── Status History Entry ─────────────────────────────────────────────────────


class StatusChange(BaseModel):
    """A record of a single state change in the pipeline."""

    from_state: PipelineState | None
    """Previous state, or ``None`` if this is the initial state."""

    to_state: PipelineState
    """The new state after the transition."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When the transition occurred."""

    reason: str | None = Field(default=None)
    """Optional explanation for the transition (e.g. error message)."""


# ── Pipeline Context ─────────────────────────────────────────────────────────


class PipelineContext(BaseModel):
    """Full context for a single pipeline processing run.

    This model carries everything a pipeline stage needs to do its work
    and report its results — the current opportunity, status history,
    processing metadata, and error information.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Unique identifier for this pipeline run."""

    opportunity: LeadOpportunity
    """The opportunity being processed."""

    state: PipelineState = Field(default=PipelineState.PENDING)
    """Current pipeline state."""

    history: list[StatusChange] = Field(default_factory=list)
    """Ordered list of state transitions this run has undergone."""

    current_draft_id: str | None = Field(default=None)
    """ID of the current outreach draft associated with this run."""

    errors: list[str] = Field(default_factory=list)
    """Error messages accumulated during processing."""

    warnings: list[str] = Field(default_factory=list)
    """Non-fatal warning messages."""

    started_at: datetime | None = Field(default=None)
    """When this pipeline run started processing."""

    completed_at: datetime | None = Field(default=None)
    """When this pipeline run reached a terminal state."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Arbitrary metadata passed between pipeline stages."""

    # ── helpers ─────────────────────────────────────────────────────────

    def transition_to(self, state: PipelineState, *, reason: str | None = None) -> None:
        """Transition to *state*, recording the change in history.

        Parameters
        ----------
        state : PipelineState
            The target state.
        reason : str, optional
            Optional explanation for the transition.

        Raises
        ------
        ValueError
            If the transition is not valid according to
            :func:`is_valid_transition`.

        """
        if not is_valid_transition(self.state, state):
            msg = f"Invalid transition: {self.state.value} -> {state.value}"
            raise ValueError(msg)

        change = StatusChange(
            from_state=self.state,
            to_state=state,
            reason=reason,
        )
        self.history.append(change)
        self.state = state

        if self.started_at is None:
            self.started_at = change.timestamp

        if state in (
            PipelineState.COMPLETED,
            PipelineState.FAILED,
            PipelineState.CANCELLED,
        ):
            self.completed_at = change.timestamp

    @property
    def elapsed_seconds(self) -> float | None:
        """Return the elapsed processing time in seconds, or ``None`` if not started."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    @property
    def is_running(self) -> bool:
        """Return *True* if the pipeline run is still in progress."""
        return self.state not in (
            PipelineState.COMPLETED,
            PipelineState.FAILED,
            PipelineState.CANCELLED,
        )


# ── Pipeline Result ──────────────────────────────────────────────────────────


class PipelineResult(BaseModel):
    """Aggregated result of a pipeline run that processed multiple opportunities."""

    success: bool
    """Did the pipeline run complete without fatal errors?"""

    pipeline_contexts: list[PipelineContext] = Field(default_factory=list)
    """Per-opportunity pipeline contexts from this run."""

    opportunities: list[LeadOpportunity] = Field(default_factory=list)
    """All opportunities processed in this run (convenience view)."""

    errors: list[str] = Field(default_factory=list)
    """Global errors not attributable to a single opportunity."""

    stats: dict[str, int] = Field(default_factory=dict)
    """Aggregated statistics (e.g. ``{"discovered": 12, "qualified": 8}``)."""

    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """When the pipeline run started."""

    completed_at: datetime | None = Field(default=None)
    """When the pipeline run completed."""

    def compute_stats(self) -> None:
        """Recompute *stats* from the current *pipeline_contexts*.

        Call this after all contexts have been added to refresh the
        aggregated counts.
        """
        counts: dict[str, int] = {}
        for ctx in self.pipeline_contexts:
            state_key = ctx.state.value
            counts[state_key] = counts.get(state_key, 0) + 1
        counts["total"] = len(self.pipeline_contexts)
        self.stats = counts
