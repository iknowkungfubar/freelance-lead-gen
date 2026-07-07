"""Data models for browser session tracking."""

from __future__ import annotations as _annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from freelance_lead_gen.utils.fingerprint import BrowserFingerprint


@dataclass
class BrowserSessionInfo:
    """Tracking info for an active browser session."""

    fingerprint: "BrowserFingerprint"
    """The fingerprint used for this session."""
    started_at: float
    """Unix timestamp when this session started."""
    pages_visited: int = 0
    """Counter of navigation events in this session."""
    errors: list[str] = field(default_factory=list)
    """Errors encountered during the session."""
