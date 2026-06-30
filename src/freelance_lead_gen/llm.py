"""LLM client wrapping OpenAI-compatible APIs with retry, rate limiting, and streaming.

Provides a single :class:`LLMClient` that handles all interaction with LLM
providers — OpenAI, OpenCode, local endpoints — through the OpenAI SDK.
Clients are configurable via :class:`Settings` and are safe for concurrent
async use.
"""

from __future__ import annotations as _annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog
from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from freelance_lead_gen.config.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

logger = structlog.get_logger(__name__)

# ── Exceptions ─────────────────────────────────────────────────────────────────


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


# ── Token counting ─────────────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Estimate token count for a string using a rough heuristic.

    Uses ``len(text) // 4`` as a fast approximation when tiktoken is not
    available or for models not in the tiktoken cache.  This is within
    ~20% of actual token counts for English text.
    """
    return max(1, len(text) // 4)


def _count_message_tokens(
    messages: list[ChatCompletionMessageParam],
    model: str = "gpt-4",
) -> int:
    """Count the total token usage for a list of chat messages.

    Attempts to use **tiktoken** if available and a known model is found,
    falling back to heuristic estimation otherwise.

    Parameters
    ----------
    messages : list of ChatCompletionMessageParam
        The messages to count.
    model : str
        The model identifier (used to select the tiktoken encoding).

    Returns
    -------
    int
        Estimated or exact token count.

    """
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        tokens_per_message = 3  # <|start|>role<|content|>
        tokens_per_name = 1

        total = 0
        for msg in messages:
            total += tokens_per_message
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multimodal content — flatten text parts.
                text_parts = [
                    p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = " ".join(text_parts)
            total += len(encoding.encode(str(role)))
            total += len(encoding.encode(str(content)))
            if msg.get("name"):
                total += tokens_per_name

        total += 3  # Every reply is primed with <|start|>assistant<|content|>
        return total
    except ImportError:
        # No tiktoken — rough estimate.
        total = 0
        for msg in messages:
            for v in msg.values():
                if isinstance(v, str):
                    total += _estimate_tokens(v)
                elif isinstance(v, list):
                    for part in v:
                        if isinstance(part, dict) and part.get("type") == "text":
                            total += _estimate_tokens(part.get("text", ""))
        return total


# ── Rate limiter ───────────────────────────────────────────────────────────────


@dataclass
class _TokenBucket:
    """Simple token-bucket rate limiter.

    Maintains a bucket of *capacity* tokens, refilling at *rate* tokens per
    second.  Each request consumes one token.
    """

    rate: float  # tokens per second
    capacity: int  # burst capacity
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    async def acquire(self) -> float:
        """Wait for a token and return the wait time in seconds."""
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            # Need to wait for the next token.
            wait = (1.0 - self._tokens) / max(self.rate, 0.001)
            self._tokens = 0.0
            self._last_refill = time.monotonic()
            return wait

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last_refill = now


# ── Response parsing ───────────────────────────────────────────────────────────


def _extract_json_from_text(text: str) -> str:
    """Extract a JSON object or array from arbitrary text.

    Handles markdown code fences, leading/trailing text, and backticks.

    Parameters
    ----------
    text : str
        Raw text that may contain JSON somewhere.

    Returns
    -------
    str
        The extracted JSON string, or the original text if no JSON-like
        structure was found.

    """
    # Try fenced code blocks first.
    match = re.search(
        r"```(?:json)?\s*\n?(.+?)\n?```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip()
        return candidate

    # Try to find a top-level object or array.
    for delim_char, other_char in (("{", "}"), ("[", "]")):
        start = text.find(delim_char)
        if start != -1:
            # Find the matching closing delimiter.
            depth = 0
            for i in range(start, len(text)):
                if text[i] == delim_char:
                    depth += 1
                elif text[i] == other_char:
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]

    return text.strip()


# ── LLM Client ─────────────────────────────────────────────────────────────────


class LLMClient:
    """Async HTTP client for OpenAI-compatible chat completion APIs.

    Wraps the ``openai`` AsyncOpenAI SDK with retry logic, rate limiting,
    token counting, and structured output parsing.

    Usage::

        client = LLMClient()
        result = await client.chat_completion([
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ])
        print(result)

    Parameters
    ----------
    settings : Settings or None
        Application settings.  When ``None``, loaded from the environment.

    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        llm_cfg = self._settings.llm

        # Build a shared httpx client for connection pooling.
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(llm_cfg.timeout_seconds),
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            ),
        )

        resolved_key: str = llm_cfg.api_key
        if not resolved_key or resolved_key == "***":
            logger.critical("LLM_API_KEY is not set")
            raise ValueError("LLM_API_KEY is not set. Set it in .env or environment variables.")

        self._client: AsyncOpenAI = AsyncOpenAI(
            api_key=resolved_key,
            base_url=llm_cfg.base_url,
            http_client=self._http_client,
        )

        # Rate limiter: default 30 RPM (tunable via env).
        max_rpm: int = 30
        self._rate_limiter: _TokenBucket = _TokenBucket(
            rate=max_rpm / 60.0,
            capacity=max(1, max_rpm // 6),
        )

        self._max_retries: int = llm_cfg.max_retries
        self._default_model: str = llm_cfg.model
        self._default_temperature: float = 0.7
        self._max_tokens_per_run: int = 100_000
        self._total_tokens: int = 0

        # Lifetime stats.
        self._stats: dict[str, Any] = {
            "total_requests": 0,
            "total_tokens": 0,
            "total_errors": 0,
            "total_retries": 0,
            "started_at": None,
        }

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Return a copy of the lifetime usage statistics."""
        return dict(self._stats)

    @property
    def default_model(self) -> str:
        """The model identifier configured as default."""
        return self._default_model

    # ── Public API ───────────────────────────────────────────────────────

    async def chat_completion(
        self,
        messages: list[ChatCompletionMessageParam],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | Literal["json_object"] | None = None,
        stream: bool = False,
        label: str | None = None,
    ) -> str | dict[str, Any]:
        """Send a chat completion request with retry and rate limiting.

        Parameters
        ----------
        messages : list of ChatCompletionMessageParam
            The conversation messages.
        model : str or None
            Model identifier.  Defaults to ``settings.llm.model``.
        temperature : float or None
            Sampling temperature (0.0–2.0).  Defaults to 0.7.
        max_tokens : int or None
            Maximum tokens in the response.  ``None`` lets the model decide.
        response_format : type[BaseModel] or ``"json_object"`` or None
            If a Pydantic model class is provided, the response is parsed into
            that model.  If ``"json_object"``, the response is parsed as a
            generic dict.  ``None`` returns raw text.
        stream : bool
            If *True*, returns an async iterator of text chunks instead of the
            full response.  Not compatible with ``response_format``.
        label : str or None
            Optional label for logging (e.g. ``"qualification"``).

        Returns
        -------
        str or dict or AsyncIterator[str]
            The response content.  When ``response_format`` is a Pydantic
            model class, returns a dict suitable for model construction.
            When ``stream=True``, returns an async iterator of text chunks.

        Raises
        ------
        LLMAuthenticationError
            If authentication fails.
        LLMRateLimitError
            If rate limited after all retries.
        LLMTimeoutError
            If the request times out.
        LLMError
            For other unrecoverable errors.

        """
        if stream and response_format is not None:
            msg = "stream=True is not compatible with structured output (response_format)"
            raise ValueError(msg)

        model_id = model or self._default_model
        temp = temperature if temperature is not None else self._default_temperature

        if self._stats["started_at"] is None:
            self._stats["started_at"] = datetime.now(UTC).isoformat()

        # Estimate input tokens for logging.
        input_tokens = _count_message_tokens(messages, model=model_id)

        # Rate-limit wait.
        wait_time = await self._rate_limiter.acquire()
        if wait_time > 0:
            logger.debug("llm.rate_limit_wait", seconds=round(wait_time, 2), label=label)
            if wait_time > 1.0:
                await asyncio.sleep(wait_time)

        # Spend-cap check — raise early if we've exhausted the budget.
        if self._total_tokens >= self._max_tokens_per_run:
            raise SpendLimitExceeded(
                f"Token budget exhausted: {self._total_tokens} >= {self._max_tokens_per_run}. "
                "Call reset_spend() to start a new run or increase max_tokens_per_run."
            )

        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model_id,
                    "messages": messages,
                    "temperature": temp,
                }
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens

                if response_format is not None:
                    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
                        # Use OpenAI's structured output (JSON mode with schema).
                        kwargs["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {
                                "name": response_format.__name__,
                                "schema": response_format.model_json_schema(),
                            },
                        }
                    elif response_format == "json_object":
                        kwargs["response_format"] = {"type": "json_object"}
                        # Instruct the model to produce JSON.
                        if not any("json" in str(m.get("content", "")).lower() for m in messages):
                            messages = list(messages)
                            messages.append(
                                {
                                    "role": "system",
                                    "content": "You MUST respond with valid JSON only.",
                                }
                            )
                            # Update kwargs so the modified messages are sent.
                            kwargs["messages"] = messages

                if stream:
                    return self._stream_completion(
                        messages=messages,
                        model=model_id,
                        temperature=temp,
                        max_tokens=max_tokens,
                        label=label,
                    )

                response: ChatCompletion = await self._client.chat.completions.create(
                    **kwargs,
                )

                self._stats["total_requests"] += 1

                # Track token usage from the response.
                if response.usage is not None:
                    total_tokens = response.usage.total_tokens or 0
                    self._stats["total_tokens"] += total_tokens
                    self._total_tokens += total_tokens

                content = response.choices[0].message.content or ""

                # Parse structured output if requested.
                if response_format is not None:
                    return self._parse_structured(content, response_format)

                logger.debug(
                    "llm.completion_success",
                    label=label,
                    model=model_id,
                    input_tokens=input_tokens,
                    output_tokens=len(content),
                    attempt=attempt,
                )

                return content

            except AuthenticationError as exc:
                self._stats["total_errors"] += 1
                raise LLMAuthenticationError(
                    f"LLM authentication failed: {exc}",
                    original=exc,
                ) from exc

            except RateLimitError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc

                retry_after = _parse_retry_after(str(exc))
                logger.warning(
                    "llm.rate_limited",
                    label=label,
                    attempt=attempt,
                    retry_after_seconds=retry_after,
                )

                if attempt < self._max_retries:
                    await asyncio.sleep(retry_after)
                    continue

                raise LLMRateLimitError(
                    f"Rate limited after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except APITimeoutError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc

                if attempt < self._max_retries:
                    backoff = 2.0**attempt + 1.0
                    logger.warning(
                        "llm.timeout",
                        label=label,
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue

                raise LLMTimeoutError(
                    f"Request timed out after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except InternalServerError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc

                if attempt < self._max_retries:
                    backoff = 3.0**attempt
                    logger.warning(
                        "llm.server_error",
                        label=label,
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue

                raise LLMError(
                    f"Server error after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except APIError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc

                if attempt < self._max_retries:
                    backoff = 2.0**attempt
                    logger.warning(
                        "llm.api_error",
                        label=label,
                        attempt=attempt,
                        status_code=getattr(exc, "status_code", None),
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue

                raise LLMError(
                    f"API error after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

        # Shouldn't normally reach here, but just in case.
        raise LLMError(
            f"Request failed after {self._max_retries} attempts",
            original=last_error,
        )

    async def _stream_completion(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str,
        temperature: float,
        max_tokens: int | None,
        label: str | None,
    ) -> AsyncIterator[str]:
        """Stream a chat completion token by token with retry handling.

        Yields text chunks as they arrive from the API.  Retries on
        transient errors (rate limits, timeouts, server errors) up to
        ``self._max_retries`` times.  Authentication errors are raised
        immediately as non-recoverable.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                stream = await self._client.chat.completions.create(**kwargs)

                async for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        yield delta.content

                self._stats["total_requests"] += 1
                logger.debug("llm.stream_complete", label=label, model=model)
                return

            except AuthenticationError as exc:
                self._stats["total_errors"] += 1
                raise LLMAuthenticationError(
                    f"LLM authentication failed: {exc}",
                    original=exc,
                ) from exc

            except RateLimitError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc
                retry_after = _parse_retry_after(str(exc))
                logger.warning(
                    "llm.stream_rate_limited",
                    label=label,
                    attempt=attempt,
                    retry_after_seconds=retry_after,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(retry_after)
                    continue
                raise LLMRateLimitError(
                    f"Rate limited after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except APITimeoutError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 2.0**attempt + 1.0
                    logger.warning(
                        "llm.stream_timeout",
                        label=label,
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise LLMTimeoutError(
                    f"Request timed out after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except InternalServerError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 3.0**attempt
                    logger.warning(
                        "llm.stream_server_error",
                        label=label,
                        attempt=attempt,
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise LLMError(
                    f"Server error after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except APIError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc
                if attempt < self._max_retries:
                    backoff = 2.0**attempt
                    logger.warning(
                        "llm.stream_api_error",
                        label=label,
                        attempt=attempt,
                        status_code=getattr(exc, "status_code", None),
                        backoff_seconds=round(backoff, 1),
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise LLMError(
                    f"API error after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

        # Shouldn't normally reach here, but just in case.
        raise LLMError(
            f"Stream request failed after {self._max_retries} attempts",
            original=last_error,
        )

    def _parse_structured(
        self,
        content: str,
        response_format: type[BaseModel] | Literal["json_object"],
    ) -> dict[str, Any]:
        """Parse raw LLM output into structured data.

        Attempts JSON parsing directly, with fallbacks for markdown-fenced
        JSON and malformed responses.

        Parameters
        ----------
        content : str
            Raw LLM output.
        response_format : type[BaseModel] or ``"json_object"``
            Expected output structure.

        Returns
        -------
        dict
            Parsed structured data.

        Raises
        ------
        LLMError
            If the content cannot be parsed as valid JSON.

        """
        # Try direct parse first.
        for attempt_fn in (
            lambda: json.loads(content),
            lambda: json.loads(_extract_json_from_text(content)),
        ):
            try:
                data = attempt_fn()
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue

        # Last resort: try to find any JSON-like structure.
        extracted = _extract_json_from_text(content)
        try:
            data = json.loads(extracted)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass

        raise LLMError(f"Failed to parse structured output. Content preview: {content[:200]}...")

    # ── Convenience methods ──────────────────────────────────────────────

    async def classify(
        self,
        system_prompt: str,
        user_content: str,
        *,
        model: str | None = None,
        temperature: float = 0.3,
    ) -> str:
        """Shortcut for a single-turn classification call.

        Uses a lower temperature (0.3) by default for more deterministic
        classification.

        Parameters
        ----------
        system_prompt : str
            The system prompt with instructions.
        user_content : str
            The user's input to classify.
        model : str or None
            Model override.
        temperature : float
            Sampling temperature (default 0.3).

        Returns
        -------
        str
            The model's response text.

        """
        return await self.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            model=model,
            temperature=temperature,
            label="classify",
        )

    async def structured_classify(
        self,
        system_prompt: str,
        user_content: str,
        response_model: type[BaseModel],
        *,
        model: str | None = None,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """Shortcut for structured classification.

        Parameters
        ----------
        system_prompt : str
            System prompt.
        user_content : str
            User input.
        response_model : type[BaseModel]
            Pydantic model defining the expected output schema.
        model : str or None
            Model override.
        temperature : float
            Sampling temperature (default 0.3).

        Returns
        -------
        dict
            Parsed structured data matching the response model.

        """
        result = await self.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            model=model,
            temperature=temperature,
            response_format=response_model,
            label="structured_classify",
        )
        if not isinstance(result, dict):
            raise ValueError(f"Expected dict from structured_classify, got {type(result).__name__}")
        return result

    def reset_spend(self) -> None:
        """Reset the per-run token counter.

        Call this before starting a new pipeline run to clear the spend cap
        counter accumulated from the previous run::

            client.reset_spend()
            result = await client.chat_completion(...)
        """
        self._total_tokens = 0
        logger.debug("llm.spend_reset")

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client and release resources.

        Call this when the client is no longer needed::

            await client.close()
        """
        await self._http_client.aclose()
        logger.info(
            "llm.closed",
            total_requests=self._stats["total_requests"],
            total_tokens=self._stats["total_tokens"],
        )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _parse_retry_after(error_text: str) -> float:
    """Extract the retry-after duration from a rate-limit error message.

    Parameters
    ----------
    error_text : str
        The error message from the API.

    Returns
    -------
    float
        Seconds to wait, defaulting to 5.0 if parsing fails.

    """
    # Look for common patterns: "retry after X seconds", "RetryAfter: X", etc.
    patterns = [
        r"retry\s*(?:after|in)?\s*(\d+(?:\.\d+)?)\s*(?:seconds|secs|s)?",
        r"Retry-After:\s*(\d+)",
        r"RetryAfter\s*[:=]\s*(\d+)",
        r"try\s*again\s*in\s*(\d+(?:\.\d+)?)\s*s",
    ]
    for pattern in patterns:
        match = re.search(pattern, error_text, re.IGNORECASE)
        if match:
            return float(match.group(1))

    return 5.0
