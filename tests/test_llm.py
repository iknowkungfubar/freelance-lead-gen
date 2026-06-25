"""Tests for the LLM client — retry logic, error handling, rate limiting, and spend tracking.

Covers the core :class:`LLMClient` contract: retry-on-error behaviour for
transient failures, spend-cap enforcement, stats tracking, and structured
output parsing.  All network calls are mocked so tests are deterministic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APITimeoutError, InternalServerError, RateLimitError

from freelance_lead_gen.config.settings import Settings
from freelance_lead_gen.llm import (
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    SpendLimitExceeded,
)

# ── Helpers ─────────────────────────────────────────────────────────────────────


def _make_rate_limit_error(
    message: str = "rate limit: retry after 0.01 seconds",
) -> RateLimitError:
    """Build a realistic :class:`RateLimitError` for mock side-effects.

    Uses a short retry-after string so tests that exercise the sleep path
    complete quickly (or instantly when ``asyncio.sleep`` is also mocked).
    """
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return RateLimitError(message, response=response, body=None)


def _make_server_error(
    message: str = "internal server error",
) -> InternalServerError:
    """Build a realistic :class:`InternalServerError` for mock side-effects."""
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    response = httpx.Response(500, request=request)
    return InternalServerError(message, response=response, body=None)


def _chat_completion_response(
    content: str = "Hello, world!",
    total_tokens: int = 15,
) -> MagicMock:
    """Build a mock ``ChatCompletion`` response suitable for the LLM client.

    Parameters
    ----------
    content : str
        The text content of the assistant's reply.
    total_tokens : int
        Reported token usage (stored in ``response.usage.total_tokens``).

    Returns
    -------
    MagicMock
        A mock object that quacks like ``ChatCompletion`` enough for the
        client's consumption.
    """
    mock = MagicMock()
    mock.usage = MagicMock(total_tokens=total_tokens)
    message = MagicMock()
    message.content = content
    mock.choices = [MagicMock(message=message)]
    return mock


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def llm_client() -> LLMClient:
    """Provide a :class:`LLMClient` with a mocked OpenAI SDK client.

    The real ``AsyncOpenAI`` instance **is not created** — ``_client`` is
    replaced with a plain ``AsyncMock`` so all ``chat.completions.create``
    calls are under test control.  The internal ``httpx.AsyncClient`` for
    connection pooling is still created (it doesn't make network requests on
    its own).
    """
    settings = Settings()
    settings.llm.api_key = "test-key-123"
    settings.llm.base_url = "http://test.local/v1"
    settings.llm.max_retries = 3
    client = LLMClient(settings=settings)
    client._client = AsyncMock()
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# Retry logic — transient errors should trigger retries
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryLogic:
    """The client should retry on transient errors and succeed eventually."""

    @pytest.mark.asyncio
    async def test_rate_limit_retry_then_succeeds(self, llm_client: LLMClient) -> None:
        """RateLimitError triggers up to ``max_retries`` retries, then returns."""
        mock_create = AsyncMock()
        mock_create.side_effect = [
            _make_rate_limit_error(),
            _make_rate_limit_error(),
            _chat_completion_response(),
        ]
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            result = await llm_client.chat_completion(
                [{"role": "user", "content": "hello"}],
            )

        assert result == "Hello, world!"
        # 2 failures + 1 success = 3 total calls (matches max_retries)
        assert mock_create.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_retry_then_succeeds(self, llm_client: LLMClient) -> None:
        """Timeout triggers exponential-backoff retries, then succeeds."""
        mock_create = AsyncMock()
        mock_create.side_effect = [
            APITimeoutError("request timed out"),
            APITimeoutError("request timed out"),
            _chat_completion_response(),
        ]
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            result = await llm_client.chat_completion(
                [{"role": "user", "content": "hello"}],
            )

        assert result == "Hello, world!"
        assert mock_create.call_count == 3

    @pytest.mark.asyncio
    async def test_internal_server_error_retry_then_succeeds(
        self,
        llm_client: LLMClient,
    ) -> None:
        """InternalServerError triggers retries, then succeeds."""
        mock_create = AsyncMock()
        mock_create.side_effect = [
            _make_server_error(),
            _make_server_error(),
            _chat_completion_response(),
        ]
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            result = await llm_client.chat_completion(
                [{"role": "user", "content": "hello"}],
            )

        assert result == "Hello, world!"
        assert mock_create.call_count == 3

    @pytest.mark.asyncio
    async def test_stats_retry_count(self, llm_client: LLMClient) -> None:
        """Stats reflect retries and total tokens."""
        mock_create = AsyncMock()
        mock_create.side_effect = [
            _make_rate_limit_error(),
            _chat_completion_response(content="OK", total_tokens=42),
        ]
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            await llm_client.chat_completion(
                [{"role": "user", "content": "count me"}],
            )

        stats = llm_client.stats
        assert stats["total_retries"] >= 1
        assert stats["total_requests"] == 1  # only the successful call counts
        assert stats["total_tokens"] == 42


# ═══════════════════════════════════════════════════════════════════════════════
# Max-retries exhaustion — each error type raises the correct exception
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaxRetriesExhausted:
    """When all retries are exhausted the client should raise a typed exception."""

    @pytest.mark.asyncio
    async def test_rate_limit_exhausted_raises_typed(self, llm_client: LLMClient) -> None:
        """All retries consumed by RateLimitError → ``LLMRateLimitError`` with original chained."""
        mock_create = AsyncMock(side_effect=_make_rate_limit_error())
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(LLMRateLimitError) as exc_info:
                await llm_client.chat_completion(
                    [{"role": "user", "content": "hello"}],
                )

        # All retry slots consumed
        assert mock_create.call_count == llm_client._max_retries  # 3
        # Original exception is chained
        assert isinstance(exc_info.value.original, RateLimitError)
        assert "Rate limited" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_timeout_exhausted_raises_typed(self, llm_client: LLMClient) -> None:
        """All retries consumed by Timeout → ``LLMTimeoutError`` with original chained."""
        mock_create = AsyncMock(side_effect=APITimeoutError("request timed out"))
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(LLMTimeoutError) as exc_info:
                await llm_client.chat_completion(
                    [{"role": "user", "content": "hello"}],
                )

        assert mock_create.call_count == llm_client._max_retries
        assert isinstance(exc_info.value.original, APITimeoutError)
        assert "timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_server_error_exhausted_raises_llm_error(self, llm_client: LLMClient) -> None:
        """All retries consumed by InternalServerError → ``LLMError`` with original chained."""
        mock_create = AsyncMock(side_effect=_make_server_error())
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(LLMError) as exc_info:
                await llm_client.chat_completion(
                    [{"role": "user", "content": "hello"}],
                )

        assert mock_create.call_count == llm_client._max_retries
        assert isinstance(exc_info.value.original, InternalServerError)
        assert "Server error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_error_paths_are_logged(self, llm_client: LLMClient) -> None:
        """Errors are recorded in stats when retries are exhausted."""
        mock_create = AsyncMock(side_effect=_make_rate_limit_error())
        llm_client._client.chat.completions.create = mock_create

        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(LLMRateLimitError):
                await llm_client.chat_completion(
                    [{"role": "user", "content": "hello"}],
                )

        stats = llm_client.stats
        assert stats["total_errors"] == llm_client._max_retries  # 3 failures
        assert stats["total_retries"] == llm_client._max_retries  # 3 retries
        assert stats["total_requests"] == 0  # no successful requests


# ═══════════════════════════════════════════════════════════════════════════════
# Successful calls & structured output parsing
# ═══════════════════════════════════════════════════════════════════════════════


class TestSuccessfulCalls:
    """Normal (non-error) responses should return parsed content."""

    @pytest.mark.asyncio
    async def test_plain_text_response(self, llm_client: LLMClient) -> None:
        """Without ``response_format`` the raw text string is returned."""
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(content="Hello from the LLM!"),
        )

        result = await llm_client.chat_completion(
            [{"role": "user", "content": "say hi"}],
        )

        assert result == "Hello from the LLM!"

    @pytest.mark.asyncio
    async def test_json_object_response(self, llm_client: LLMClient) -> None:
        """With ``response_format="json_object"`` the response is parsed into a dict."""
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(
                content='{"qualified": true, "score": 85}',
            ),
        )

        result = await llm_client.chat_completion(
            [{"role": "user", "content": "Classify this lead"}],
            response_format="json_object",
        )

        assert isinstance(result, dict)
        assert result["qualified"] is True
        assert result["score"] == 85

    @pytest.mark.asyncio
    async def test_markdown_fenced_json(self, llm_client: LLMClient) -> None:
        """JSON inside markdown code fences is still parsed correctly."""
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(
                content="""Here is the result:
```json
{"category": "AI", "confidence": 0.92}
```
""",
            ),
        )

        result = await llm_client.chat_completion(
            [{"role": "user", "content": "Categorise"}],
            response_format="json_object",
        )

        assert result["category"] == "AI"
        assert result["confidence"] == 0.92

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_extraction(self, llm_client: LLMClient) -> None:
        """A response with surrounding text is recovered by ``_extract_json_from_text``."""
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(
                content='The result is: {"match": true, "score": 72} and that is final.',
            ),
        )

        result = await llm_client.chat_completion(
            [{"role": "user", "content": "check"}],
            response_format="json_object",
        )

        assert result["match"] is True
        assert result["score"] == 72

    @pytest.mark.asyncio
    async def test_stats_after_multiple_calls(self, llm_client: LLMClient) -> None:
        """Stats accumulate correctly after 3+ successful calls."""
        mock_create = AsyncMock(
            return_value=_chat_completion_response(content="ok", total_tokens=10),
        )
        llm_client._client.chat.completions.create = mock_create

        for i in range(3):
            await llm_client.chat_completion(
                [{"role": "user", "content": f"call {i}"}],
            )

        stats = llm_client.stats
        assert stats["total_requests"] == 3
        assert stats["total_tokens"] == 30  # 3 calls x 10 tokens each
        assert stats["total_errors"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Spend limit & token budget
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpendLimit:
    """Per-run token budget enforcement."""

    @pytest.mark.asyncio
    async def test_spend_limit_exceeded_raises(self, llm_client: LLMClient) -> None:
        """Once ``_total_tokens >= _max_tokens_per_run``, subsequent calls raise."""
        llm_client._max_tokens_per_run = 10
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(content="ok", total_tokens=15),
        )

        # First call pushes _total_tokens past the limit (15 >= 10).
        await llm_client.chat_completion([{"role": "user", "content": "use tokens"}])

        # Second call should be blocked before making an API request.
        with pytest.raises(SpendLimitExceeded) as exc_info:
            await llm_client.chat_completion([{"role": "user", "content": "should fail"}])

        assert "Token budget" in str(exc_info.value)
        # The API should not have been called a second time.
        assert llm_client._client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_reset_spend_allows_new_calls(self, llm_client: LLMClient) -> None:
        """Calling ``reset_spend()`` clears the counter and allows new requests."""
        llm_client._max_tokens_per_run = 10
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(content="ok", total_tokens=15),
        )

        await llm_client.chat_completion([{"role": "user", "content": "use tokens"}])

        # Reset the spend counter.
        llm_client.reset_spend()

        # Now a new call should succeed.
        result = await llm_client.chat_completion([{"role": "user", "content": "new run"}])
        assert result == "ok"
        # Two API calls total (first + after reset).
        assert llm_client._client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_spend_limit_zero_tokens_blocks_immediately(self, llm_client: LLMClient) -> None:
        """With ``_max_tokens_per_run=0``, the very first call is blocked."""
        llm_client._max_tokens_per_run = 0

        with pytest.raises(SpendLimitExceeded):
            await llm_client.chat_completion([{"role": "user", "content": "blocked"}])

        # No API call should have been attempted.
        llm_client._client.chat.completions.create.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience methods
# ═══════════════════════════════════════════════════════════════════════════════


class TestConvenienceMethods:
    """Shortcut methods wrap ``chat_completion`` correctly."""

    @pytest.mark.asyncio
    async def test_classify_returns_text(self, llm_client: LLMClient) -> None:
        """:meth:`classify` returns the raw text response."""
        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(content="positive"),
        )

        result = await llm_client.classify(
            system_prompt="Classify the sentiment.",
            user_content="Great project!",
        )

        assert result == "positive"

    @pytest.mark.asyncio
    async def test_structured_classify_returns_dict(self, llm_client: LLMClient) -> None:
        """:meth:`structured_classify` returns a parsed dict."""
        from pydantic import BaseModel

        class Result(BaseModel):
            qualified: bool
            score: int

        llm_client._client.chat.completions.create = AsyncMock(
            return_value=_chat_completion_response(
                content='{"qualified": true, "score": 92}',
            ),
        )

        result = await llm_client.structured_classify(
            system_prompt="Evaluate this lead.",
            user_content="A lead about AI consulting",
            response_model=Result,
        )

        assert result["qualified"] is True
        assert result["score"] == 92


# ═══════════════════════════════════════════════════════════════════════════════
# Error handling — non-retriable errors
# ═══════════════════════════════════════════════════════════════════════════════


class TestNonRetriableErrors:
    """Authentication errors should fail immediately with no retry."""

    @pytest.mark.asyncio
    async def test_authentication_error_no_retry(self, llm_client: LLMClient) -> None:
        """An ``AuthenticationError`` is raised immediately without retrying."""
        from openai import AuthenticationError

        request = httpx.Request("POST", "http://test.local/v1/chat/completions")
        response = httpx.Response(401, request=request)
        auth_error = AuthenticationError("invalid key", response=response, body=None)

        llm_client._client.chat.completions.create = AsyncMock(
            side_effect=auth_error,
        )

        with pytest.raises(LLMError):
            await llm_client.chat_completion([{"role": "user", "content": "hello"}])

        # Only one attempt — authentication errors don't retry.
        llm_client._client.chat.completions.create.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle & edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    """Client lifecycle and edge-case behaviours."""

    @pytest.mark.asyncio
    async def test_stream_and_response_format_are_mutually_exclusive(
        self,
        llm_client: LLMClient,
    ) -> None:
        """Passing ``stream=True`` with ``response_format`` raises ``ValueError``."""
        from pydantic import BaseModel

        class Dummy(BaseModel):
            x: int

        with pytest.raises(ValueError, match="stream"):
            await llm_client.chat_completion(
                [{"role": "user", "content": "test"}],
                stream=True,
                response_format=Dummy,
            )

    @pytest.mark.asyncio
    async def test_stats_contains_expected_keys(self, llm_client: LLMClient) -> None:
        """The ``stats`` property returns a dict with all expected counters."""
        stats = llm_client.stats
        assert "total_requests" in stats
        assert "total_tokens" in stats
        assert "total_errors" in stats
        assert "total_retries" in stats
        assert "started_at" in stats

    def test_default_model_property(self, llm_client: LLMClient) -> None:
        """The ``default_model`` property matches the configured model."""
        assert llm_client.default_model == "deepseek-v4-flash"

    @pytest.mark.asyncio
    async def test_close_idempotent(self, llm_client: LLMClient) -> None:
        """:meth:`close` can be called multiple times without error."""
        await llm_client.close()
        # Second close should also succeed.
        await llm_client.close()
