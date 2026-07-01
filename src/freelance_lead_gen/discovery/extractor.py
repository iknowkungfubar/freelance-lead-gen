"""Base extraction logic — raw leads and extractor interface.

Defines the :class:`RawLead` dataclass used throughout the pipeline and the
:class:`Extractor` abstract base class for platform-specific implementations.
"""

from __future__ import annotations as _annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ── Detection constants (used by BasePlatformExtractor) ────────────────────────


_LOGIN_REDIRECT_INDICATORS: list[str] = [
    "/login",
    "/signin",
    "/auth",
    "account/login",
    "account/signin",
    "login-page",
]
"""URL substrings that indicate a redirect to a login/authentication page."""

_CAPTCHA_INDICATORS: list[str] = [
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "cf-turnstile",
    "challenge",
    "verify you are human",
    "security check",
    "cf-challenge",
]
"""Text or URL substrings that indicate a CAPTCHA or challenge page."""


# ── RawLead dataclass ──────────────────────────────────────────────────────────


@dataclass
class RawLead:
    """A raw, unprocessed lead extracted from a freelance platform.

    This is the output of the extraction layer — a lightly-normalised
    representation of a job listing.  It gets classified into a proper
    :class:`~freelance_lead_gen.models.opportunity.LeadOpportunity` by
    a later pipeline step.

    .. note::
        All dates are stored as ISO-format strings for simplicity at
        the extraction level.  Parsing into :class:`datetime` objects
        happens during classification.
    """

    platform: str
    """Source platform name (e.g. ``"upwork"``, ``"linkedin"``)."""

    platform_job_id: str
    """Platform-native identifier for this listing (used for deduplication)."""

    title: str
    """Job or project title."""

    company: str | None = None
    """Hiring company or client name, if visible."""

    description: str = ""
    """Full text of the listing description."""

    url: str | None = None
    """Direct URL to the listing."""

    posted_date: str | None = None
    """ISO-format date string (e.g. ``"2026-06-24"`` or ``"2026-06-24T10:30:00Z"``)."""

    budget_min: float | None = None
    """Minimum budget / hourly rate in USD."""

    budget_max: float | None = None
    """Maximum budget / hourly rate in USD."""

    currency: str = "USD"
    """ISO 4217 currency code."""

    skills: list[str] = field(default_factory=list)
    """List of skill keywords mentioned in the listing."""

    location: str | None = None
    """Location string — remote, city, country, or ``None``."""

    raw_html: str | None = None
    """Raw HTML snippet of the listing card (for debugging / re-parsing)."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Extra fields extracted via extra_selectors (free-form key/value pairs)."""

    extracted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    """ISO-format timestamp of extraction."""


# ── Base extractor ─────────────────────────────────────────────────────────────


class Extractor(abc.ABC):
    """Abstract base for all platform extractors.

    Subclasses implement :meth:`extract` which returns a list of
    :class:`RawLead` instances discovered by searching the platform.
    """

    @abc.abstractmethod
    async def extract(
        self,
        platform: str,
        search_query: str,
    ) -> list[RawLead]:
        """Execute extraction for the given *platform* and *search_query*.

        Parameters
        ----------
        platform : str
            Lowercase platform name.
        search_query : str
            Search term (e.g. ``"AI automation"``, ``"RAG pipeline"``).

        Returns
        -------
        list of RawLead
            All leads found during this extraction run.

        """
        ...
