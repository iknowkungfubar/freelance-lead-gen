"""Exception hierarchy for LLM client operations."""


class LLMError(RuntimeError):
    """Base exception for LLM client errors."""

    def __init__(self, message: str, original: Exception | None = None) -> None:
        self.original = original
        super().__init__(message)


class LLMAuthenticationError(LLMError):
    """Raised when the API key is invalid or missing."""


class LLMRateLimitError(LLMError):
    """Raised when the API rate limit is exceeded."""


class LLMTimeoutError(LLMError):
    """Raised when a request times out."""


class LLMContentFilterError(LLMError):
    """Raised when the response was filtered by content moderation."""


class SpendLimitExceeded(LLMError):
    """Raised when the per-run token budget is exhausted.

    Triggered when :attr:`LLMClient._total_tokens` reaches
    :attr:`LLMClient._max_tokens_per_run`.  Call :meth:`LLMClient.reset_spend`
    to clear the counter for a new pipeline run.
    """
