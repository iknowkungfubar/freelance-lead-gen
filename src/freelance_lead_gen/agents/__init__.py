"""Orchestration agents for the freelance lead generation pipeline.

Phase 3 of the system — provides the central pipeline controller and all
processing agents that transform discovered opportunities into qualified,
personalised outreach drafts.

Agents
------
- :class:`LeadGenOrchestrator` — central pipeline state machine and controller.
- :class:`FilteringPipeline` — scores and qualifies discovered opportunities.
- :class:`ProfileMatcher` — compares opportunities against a target profile.
- :class:`PersonalizationAgent` — generates personalised outreach drafts.
- :class:`VerificationAgent` — quality-verifies generated drafts.

Supporting models
-----------------
- :class:`TargetProfile` — the freelancer's ideal opportunity description.
- :class:`TargetProfile` — default profile factory.
- :class:`ScoringThresholds` — score boundaries for qualification tiers.
- :class:`MatchingWeights` — configurable weights for profile comparison.

Reports
-------
- :class:`OrchestratorReport` — aggregated pipeline run results.
- :class:`FilteringReport` — filtering phase results.
- :class:`PersonalizationReport` — draft generation results.
- :class:`VerificationResult` — verification check results.
"""

from __future__ import annotations as _annotations

from freelance_lead_gen.agents.filtering_agent import (
    FilteringPipeline,
    FilteringReport,
    ScoringThresholds,
)
from freelance_lead_gen.agents.orchestrator import (
    LeadGenOrchestrator,
    OrchestratorReport,
    PipelinePhase,
)
from freelance_lead_gen.agents.personalization_agent import (
    PersonalizationAgent,
    PersonalizationReport,
)
from freelance_lead_gen.agents.profile_matcher import (
    MatchingWeights,
    ProfileMatcher,
    TargetProfile,
)
from freelance_lead_gen.agents.verification_agent import (
    VerificationAgent,
    VerificationResult,
)

__all__ = [
    # Orchestrator
    "LeadGenOrchestrator",
    "OrchestratorReport",
    "PipelinePhase",
    # Filtering
    "FilteringPipeline",
    "FilteringReport",
    "ScoringThresholds",
    # Profile matching
    "TargetProfile",
    "ProfileMatcher",
    "MatchingWeights",
    # Personalization
    "PersonalizationAgent",
    "PersonalizationReport",
    # Verification
    "VerificationAgent",
    "VerificationResult",
]
