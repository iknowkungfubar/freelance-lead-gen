"""Application settings loaded from environment variables and ``.env`` files.

Uses **pydantic-settings** so every value can be overridden at deployment
time without code changes.  A cached singleton is exposed via
:func:`get_settings`.
"""

from __future__ import annotations as _annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
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
    viewport_width: int = Field(
        default=1920, ge=800, le=3840, description="Default viewport width (px)."
    )
    viewport_height: int = Field(
        default=1080, ge=600, le=2160, description="Default viewport height (px)."
    )

    # ── computed convenience ────────────────────────────────────────────
    @property
    def viewport(self) -> tuple[int, int]:
        """Return the viewport as a (width, height) tuple."""
        return (self.viewport_width, self.viewport_height)


class _DiscoverySettings(BaseSettings):
    """Opportunity discovery / scraping settings."""

    model_config = SettingsConfigDict(env_prefix="DISCOVERY_")

    max_daily: int = Field(
        default=50, ge=1, le=500, description="Maximum opportunities to process per day."
    )
    schedule_interval_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        description="Interval between discovery rounds (minutes).",
    )
    auto_screen: bool = Field(
        default=True,
        description=(
            "Run the full screening pipeline (filtering → drafting → "
            "verification) automatically after each discovery round that "
            "finds new leads.  Set false to keep scheduling discovery only."
        ),
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

    provider: str = Field(
        default="opencode", description="LLM provider name (for logging / routing)."
    )
    model: str = Field(default="deepseek-v4-flash", description="Model identifier string.")
    base_url: str = Field(default="https://opencode.ai/zen/go/v1", description="API base URL.")
    api_key: str = Field(default="", description="API key (blank allowed for local providers).")
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum API call retries.")
    timeout_seconds: int = Field(
        default=120, ge=10, le=600, description="Request timeout in seconds."
    )
    max_tokens_per_minute: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description=(
            "Provider token-per-minute budget. The LLM client throttles "
            "outgoing requests to stay under this ceiling (Groq's free tier "
            "allows 12,000)."
        ),
    )
    estimated_output_tokens: int = Field(
        default=2_048,
        ge=50,
        le=50_000,
        description=(
            "Assumed output token count used when reserving budget. Generous "
            "enough to cover long draft generation so bursts never overshoot "
            "the provider's TPM ceiling."
        ),
    )

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
    auto_approve: bool = Field(
        default=False, description="Auto-approve outreach drafts without human review."
    )
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


class _PlatformCredentialsSettings(BaseSettings):
    """Platform account credentials (kept out of :class:`Settings` logs).

    Credentials are read directly from environment variables / the ``.env``
    file and only ever passed to the platform extractors at discovery time.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    upwork_username: str = Field(default="", description="Upwork account email/username.")
    upwork_password: str = Field(default="", description="Upwork account password.")
    linkedin_username: str = Field(default="", description="LinkedIn account email/username.")
    linkedin_password: str = Field(default="", description="LinkedIn account password.")
    freelancer_username: str = Field(default="", description="Freelancer.com account username.")
    freelancer_password: str = Field(default="", description="Freelancer.com account password.")

    # ── computed convenience ────────────────────────────────────────────
    def for_platform(self, platform_name: str) -> dict[str, str]:
        """Return the ``{email/username, password}`` dict for *platform_name*.

        If no credentials are configured, an empty dict is returned and the
        extractor falls back to cookie/session-based auth or skips login.
        """
        mapping: dict[str, str] = {
            "upwork": "upwork",
            "linkedin": "linkedin",
            "freelancer": "freelancer",
        }
        key = mapping.get(platform_name.lower())
        if key is None:
            return {}
        username = getattr(self, f"{key}_username", "") or ""
        password = getattr(self, f"{key}_password", "") or ""
        if not username or not password:
            return {}
        return {"username": username, "password": password}


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
    platform_credentials: _PlatformCredentialsSettings = Field(
        default_factory=_PlatformCredentialsSettings
    )

    # ── model_config set the env prefix at the parent level too ─────────
    # These nested models use their own env_prefix via SettingsConfigDict.

    def model_post_init(self, __context: object, /) -> None:
        """Emit a warning if no ``.env`` file can be found.

        pydantic-settings already loads the ``.env`` file specified by
        ``model_config.env_file``; this hook checks that the file actually
        exists and warns if it doesn't, which helps with deployment issues.
        """
        env_file = self.model_config.get("env_file")
        if env_file:
            resolved = Path.cwd() / env_file  # type: ignore[operator]  # model_config.env_file is str|Path at runtime
            if not resolved.is_file():
                import structlog

                structlog.get_logger(__name__).warning(
                    "settings.env_file_not_found",
                    path=str(resolved),
                    hint="Create a .env file or set environment variables directly.",
                )


# ── Cached singleton factory ──────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    The settings are loaded once at first call and cached for the lifetime
    of the process.  Call ``get_settings.cache_clear()`` to reload (useful
    in tests that mutate ``os.environ``).
    """
    load_dotenv()
    return Settings()  # type: ignore[call-arg]
