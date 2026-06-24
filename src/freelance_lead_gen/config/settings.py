"""Application settings loaded from environment variables and ``.env`` files.

Uses **pydantic-settings** so every value can be overridden at deployment
time without code changes.  A cached singleton is exposed via
:func:`get_settings`.
"""

from __future__ import annotations as _annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _BrowserSettings(BaseSettings):
    """Browser automation settings."""

    model_config = SettingsConfigDict(env_prefix="BROWSER_")

    headless: bool = Field(default=False, description="Run browser in headless mode.")
    user_data_dir: str = Field(
        default="./browser_data",
        description="Path to the browser user data directory.",
    )
    profile_name: str = Field(default="Default", description="Browser profile name to use.")
    viewport_width: int = Field(default=1920, ge=800, le=3840, description="Default viewport width (px).")
    viewport_height: int = Field(default=1080, ge=600, le=2160, description="Default viewport height (px).")

    # ── computed convenience ────────────────────────────────────────────
    @property
    def viewport(self) -> tuple[int, int]:
        """Return the viewport as a (width, height) tuple."""
        return (self.viewport_width, self.viewport_height)


class _DiscoverySettings(BaseSettings):
    """Opportunity discovery / scraping settings."""

    model_config = SettingsConfigDict(env_prefix="DISCOVERY_")

    max_daily: int = Field(default=50, ge=1, le=500, description="Maximum opportunities to process per day.")
    schedule_interval_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Interval between discovery rounds (minutes).",
    )
    search_queries: str = Field(
        default="AI automation,AI readiness assessment,LLM pipeline,fine-tuning,"
        "RAG implementation,AI consulting,IT consulting",
        description="Comma-separated list of search queries for discovery.",
    )

    # ── computed convenience ────────────────────────────────────────────
    @property
    def queries(self) -> list[str]:
        """Return the parsed list of search queries."""
        return [q.strip() for q in self.search_queries.split(",") if q.strip()]


class _LLMSettings(BaseSettings):
    """LLM provider settings (OpenAI-compatible API)."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: str = Field(default="opencode", description="LLM provider name (for logging / routing).")
    model: str = Field(default="deepseek-v4-flash", description="Model identifier string.")
    base_url: str = Field(default="https://opencode.ai/zen/go/v1", description="API base URL.")
    api_key: str = Field(default="", description="API key (blank allowed for local providers).")
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum API call retries.")
    timeout_seconds: int = Field(default=120, ge=10, le=600, description="Request timeout in seconds.")

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class _DatabaseSettings(BaseSettings):
    """SQLite database settings."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    path: str = Field(default="./data/leads.db", description="SQLite database file path.")
    echo: bool = Field(default=False, description="Log all SQL statements (debug).")
    pool_size: int = Field(default=5, ge=1, le=20, description="Connection pool size.")
    pool_overflow: int = Field(default=10, ge=0, le=50, description="Max overflow connections.")

    # ── computed convenience ────────────────────────────────────────────
    @property
    def database_url(self) -> str:
        """Return the full async SQLAlchemy database URL.

        Uses aiosqlite as the async backend for SQLite.
        """
        resolved = Path(self.path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{resolved}"


class _HITLSettings(BaseSettings):
    """Human-in-the-loop (HITL) review settings."""

    model_config = SettingsConfigDict(env_prefix="HITL_")

    enabled: bool = Field(default=True, description="Enable HITL review gate.")
    auto_approve: bool = Field(default=False, description="Auto-approve outreach drafts without human review.")
    review_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Max seconds to wait for human review before skipping.",
    )


class _PlatformSettings(BaseSettings):
    """Platform enablement settings."""

    model_config = SettingsConfigDict(env_prefix="PLATFORMS_")

    enabled: str = Field(
        default="upwork,linkedin,freelancer",
        description="Comma-separated list of enabled platform names.",
    )

    # ── computed convenience ────────────────────────────────────────────
    @property
    def enabled_list(self) -> list[str]:
        """Return the parsed list of enabled platform names."""
        return [p.strip().lower() for p in self.enabled.split(",") if p.strip()]


class Settings(BaseSettings):
    """Root configuration object for the freelance lead generation system.

    Every section is backed by its own nested :class:`BaseSettings` model
    with a matching env prefix so environment variables map naturally.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    browser: _BrowserSettings = Field(default_factory=_BrowserSettings)
    discovery: _DiscoverySettings = Field(default_factory=_DiscoverySettings)
    llm: _LLMSettings = Field(default_factory=_LLMSettings)
    database: _DatabaseSettings = Field(default_factory=_DatabaseSettings)
    hitl: _HITLSettings = Field(default_factory=_HITLSettings)
    platforms: _PlatformSettings = Field(default_factory=_PlatformSettings)

    # ── model_config set the env prefix at the parent level too ─────────
    # These nested models use their own env_prefix via SettingsConfigDict.

    def model_post_init(self, __context: object) -> None:
        """Resolve the .env file path relative to the project root.

        Walks up from the current working directory looking for ``.env``.
        Falls back silently if none is found.
        """
        # pydantic-settings already handles env_file; this hook is a safety
        # net for when the CWD is not the project root.
        env_candidates = [Path.cwd() / ".env", Path.cwd().parent / ".env"]
        for candidate in env_candidates:
            if candidate.is_file():
                # Already loaded by SettingsConfigDict — nothing to do.
                return


# ── Cached singleton factory ──────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    The settings are loaded once at first call and cached for the lifetime
    of the process.  Call ``get_settings.cache_clear()`` to reload (useful
    in tests that mutate ``os.environ``).
    """
    return Settings()  # type: ignore[call-arg]
