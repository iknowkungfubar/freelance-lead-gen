"""Platform definitions — enumerations, configuration, and credential models.

Each platform the system integrates with has a :class:`PlatformConfig` that
controls *how* it is interacted with (rate limits, auth requirements, bot
countermeasures) and an optional :class:`PlatformCredentials` model that
holds authentication secrets (never logged or serialised).
"""

from __future__ import annotations as _annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_serializer, model_validator

# ── Platform Enum ────────────────────────────────────────────────────────────


class Platform(StrEnum):
    """Supported freelance / job platforms."""

    UPWORK = "upwork"
    LINKEDIN = "linkedin"
    FREELANCER = "freelancer"
    REMOTE_OK = "remote_ok"
    YC_WORK = "yc_work"
    CUSTOM = "custom"

    def __str__(self) -> str:
        return self.value

    @property
    def display_name(self) -> str:
        """Return a human-readable display name (e.g. ``"Remote OK"``)."""
        return _DISPLAY_NAMES[self]


_DISPLAY_NAMES: dict[Platform, str] = {
    Platform.UPWORK: "Upwork",
    Platform.LINKEDIN: "LinkedIn",
    Platform.FREELANCER: "Freelancer",
    Platform.REMOTE_OK: "Remote OK",
    Platform.YC_WORK: "Y Combinator (Work at a Startup)",
    Platform.CUSTOM: "Custom",
}


# ── Platform Config ──────────────────────────────────────────────────────────


_ANTI_BOT_PROFILES: dict[str, dict[str, Any]] = {
    "upwork": {
        "stealth": True,
        "humanize_mouse": True,
        "random_delay_range": (0.5, 3.0),
        "avoid_webdriver_detect": True,
        "mock_canvas": True,
    },
    "linkedin": {
        "stealth": True,
        "humanize_mouse": True,
        "random_delay_range": (1.0, 5.0),
        "avoid_webdriver_detect": True,
        "rotate_user_agent": True,
    },
    "freelancer": {
        "stealth": True,
        "humanize_mouse": True,
        "random_delay_range": (1.0, 4.0),
        "avoid_webdriver_detect": True,
    },
    "default": {
        "stealth": True,
        "humanize_mouse": False,
        "random_delay_range": (1.0, 3.0),
        "avoid_webdriver_detect": False,
    },
}


class PlatformConfig(BaseModel):
    """Configuration for a single freelance platform.

    Each platform the system interacts with is fully described by one of
    these models — from search entry-points to rate-limit behaviour to
    anti-bot countermeasures.
    """

    platform: Platform
    """Which platform this config applies to."""

    enabled: bool = Field(default=True)
    """Should this platform be actively scraped?"""

    search_url: str
    """Base URL or URL template for searching / browsing listings."""

    auth_required: bool = Field(default=True)
    """Does this platform require authentication to browse?"""

    rate_limit_delay: float = Field(default=3.0, ge=0.5, le=30.0)
    """Minimum delay (seconds) between requests to this platform."""

    max_pages_per_session: int = Field(default=10, ge=1, le=100)
    """Maximum pages to scrape in a single session before rotating."""

    anti_bot_profile: dict[str, Any] = Field(default_factory=dict)
    """Anti-bot / stealth configuration overrides for this platform."""

    # ── model-level validators ──────────────────────────────────────────

    @model_validator(mode="after")
    def _apply_default_anti_bot(self) -> PlatformConfig:
        if not self.anti_bot_profile:
            self.anti_bot_profile = dict(
                _ANTI_BOT_PROFILES.get(self.platform.value, _ANTI_BOT_PROFILES["default"])
            )
        return self

    # ── serialisation ───────────────────────────────────────────────────

    @field_serializer("platform")
    def _serialise_platform(self, value: Platform) -> str:
        return value.value


# ── Platform Credentials ─────────────────────────────────────────────────────


class PlatformCredentials(BaseModel):
    """Authentication credentials for a freelance platform.

    .. warning::
        Credential secrets are **never** logged, serialised to disk, or
        included in exception messages.  Use the :meth:`redacted` helper
        for safe display.
    """

    platform: Platform
    """Which platform these credentials are for."""

    username: str | None = Field(default=None)
    """Username / email for login."""

    password: str | None = Field(default=None)
    """Password for login (redacted in output)."""

    api_key: str | None = Field(default=None)
    """API key if the platform offers one (redacted in output)."""

    token: str | None = Field(default=None)
    """OAuth / session token (redacted in output)."""

    cookies: dict[str, str] | None = Field(default=None)
    """Serialised cookie dictionary (never logged)."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """Any additional platform-specific auth fields."""

    # ── serialisation ───────────────────────────────────────────────────

    @field_serializer("password", "api_key", "token", "cookies")
    def _redact_secrets(self, value: object, _info: Any) -> str | None:
        """Always redact secret fields when serialising."""
        if value is None:
            return None
        return "********"

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Override default dump to ensure secrets are always redacted.

        Parameters are passed through to :meth:`BaseModel.model_dump`.
        """
        return super().model_dump(**kwargs)

    def redacted(self) -> dict[str, Any]:
        """Return a safe-to-log dictionary with all secrets redacted."""
        return self.model_dump()
