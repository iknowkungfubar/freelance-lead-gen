"""Platform-specific extractors for freelance / job platforms.

Each platform has a dedicated extractor that knows the platform's
DOM structure, login flow, and search URL pattern.  The
:data:`PLATFORM_EXTRACTORS` registry maps platform names to extractor
classes for dynamic lookup.
"""

from __future__ import annotations as _annotations

from typing import TYPE_CHECKING

from freelance_lead_gen.discovery.platforms.freelancer import FreelancerExtractor
from freelance_lead_gen.discovery.platforms.job_boards import (
    AggregatorExtractor,
    RemoteOKExtractor,
    YCWorkExtractor,
)
from freelance_lead_gen.discovery.platforms.linkedin import LinkedInExtractor
from freelance_lead_gen.discovery.platforms.upwork import UpworkExtractor

if TYPE_CHECKING:
    from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor

# ── Platform extractor registry ────────────────────────────────────────────────

PLATFORM_EXTRACTORS: dict[str, type[BasePlatformExtractor]] = {
    "upwork": UpworkExtractor,
    "linkedin": LinkedInExtractor,
    "freelancer": FreelancerExtractor,
    "remote_ok": RemoteOKExtractor,
    "yc_work": YCWorkExtractor,
    "custom": AggregatorExtractor,
}
"""Mapping of platform names (matching :class:`~freelance_lead_gen.models.platform.Platform`
enum values) to their corresponding extractor classes."""

__all__ = [
    "PLATFORM_EXTRACTORS",
    "AggregatorExtractor",
    "BasePlatformExtractor",
    "FreelancerExtractor",
    "LinkedInExtractor",
    "RemoteOKExtractor",
    "UpworkExtractor",
    "YCWorkExtractor",
]


# Re-export the base class so consumers can reference it.
from freelance_lead_gen.discovery.platforms.base import BasePlatformExtractor  # noqa: E402
