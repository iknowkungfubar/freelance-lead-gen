"""Domain models for freelance opportunities, platforms, and pipeline state."""

from __future__ import annotations as _annotations

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
)
from freelance_lead_gen.models.platform import (
    Platform,
    PlatformConfig,
    PlatformCredentials,
)

__all__ = [
    "LeadOpportunity",
    "LeadScoringResult",
    "LeadStatus",
    "OutboundDraft",
    "PipelineContext",
    "PipelineResult",
    "PipelineState",
    "Platform",
    "PlatformConfig",
    "PlatformCredentials",
]
