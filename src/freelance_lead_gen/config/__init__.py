"""Configuration package — application settings and LLM prompt templates."""

from __future__ import annotations as _annotations

from freelance_lead_gen.config.prompts import (
    ANTI_AI_PROMPT,
    CLASSIFICATION_PROMPT,
    OUTREACH_DRAFT_PROMPT,
    QUALIFICATION_PROMPT,
    get_platform_prompt_modifier,
)
from freelance_lead_gen.config.settings import Settings, get_settings

__all__ = [
    "ANTI_AI_PROMPT",
    "CLASSIFICATION_PROMPT",
    "OUTREACH_DRAFT_PROMPT",
    "QUALIFICATION_PROMPT",
    "Settings",
    "get_platform_prompt_modifier",
    "get_settings",
]
