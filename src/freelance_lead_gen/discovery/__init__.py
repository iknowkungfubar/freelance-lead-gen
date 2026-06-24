"""Discovery layer — browser automation, platform extraction, and scheduling.

Phase 2 of the freelance lead generation system.  Handles finding and
extracting opportunities from freelance platforms through a mix of
Playwright-based browser automation and direct HTTP access.
"""

from __future__ import annotations as _annotations

from freelance_lead_gen.discovery.browser import ManagedBrowser
from freelance_lead_gen.discovery.discovery_agent import DiscoveryAgent
from freelance_lead_gen.discovery.extractor import RawLead
from freelance_lead_gen.discovery.scheduler import DiscoveryScheduler

__all__ = [
    "DiscoveryAgent",
    "DiscoveryScheduler",
    "ManagedBrowser",
    "RawLead",
]
