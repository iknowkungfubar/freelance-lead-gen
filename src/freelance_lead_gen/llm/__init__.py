"""LLM client package -- OpenAI-compatible API wrapper with retry and rate limiting."""

from .client import LLMClient
from .exceptions import (
    LLMAuthenticationError,
    LLMContentFilterError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    SpendLimitExceeded,
)
from .parsing import extract_json_from_text, parse_retry_after
from .rate_limiter import TokenBucket
from .tokenizer import count_message_tokens, estimate_tokens

__all__ = [
    "LLMAuthenticationError",
    "LLMClient",
    "LLMContentFilterError",
    "LLMError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "SpendLimitExceeded",
    "TokenBucket",
    "count_message_tokens",
    "estimate_tokens",
    "extract_json_from_text",
    "parse_retry_after",
]
