"""LLM client wrapping OpenAI-compatible APIs with retry, rate limiting, and streaming.

Provides a single :class:`LLMClient` that handles all interaction with LLM
providers -- OpenAI, OpenCode, local endpoints -- through the OpenAI SDK.
Clients are configurable via :class:`Settings` and are safe for concurrent
async use.
"""

from __future__ import annotations as _annotations

import asyncio
import json
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

from .exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    SpendLimitExceeded,
)
from .parsing import extract_json_from_text, parse_retry_after
from .rate_limiter import SlidingWindowLimiter
from .tokenizer import count_message_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

logger = structlog.get_logger(__name__)


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
            max_retries=0,  # Retries are handled by LLMClient itself.
        )

        tpm_budget: int = llm_cfg.max_tokens_per_minute
        self._tpm_budget: int = tpm_budget
        self._estimated_output_tokens: int = llm_cfg.estimated_output_tokens
        self._rate_limiter: SlidingWindowLimiter = SlidingWindowLimiter(tpm_budget)

        self._max_retries: int = llm_cfg.max_retries
        self._default_model: str = llm_cfg.model
        self._default_temperature: float = 0.7
        self._max_tokens_per_run: int = 100_000
        self._total_tokens: int = 0

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
            Sampling temperature (0.0-2.0).  Defaults to 0.7.
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

        input_tokens = count_message_tokens(messages, model=model_id)

        if self._total_tokens >= self._max_tokens_per_run:
            raise SpendLimitExceeded(
                f"Token budget exhausted: {self._total_tokens} >= {self._max_tokens_per_run}. "
                "Call reset_spend() to start a new run or increase max_tokens_per_run."
            )

        last_error: Exception | None = None

        # max_retries=0 means 1 attempt (no retries), max_retries=1 means 2 attempts (1 retry), etc.
        max_attempts = self._max_retries + 1

        # Some providers/models don't support strict `json_schema` output.
        # When the API rejects it, we transparently retry with `json_object`
        # mode and parse the same Pydantic schema from the JSON payload.
        json_schema_fallback = False

        for attempt in range(1, max_attempts + 1):
            # Reserve budget for this attempt (input + an assumed output
            # estimate).  Settled against actual usage on success, cancelled
            # on failure so a broken provider doesn't stall the pipeline.
            reserved_tokens = input_tokens + self._estimated_output_tokens
            wait_time, reservation = await self._rate_limiter.acquire(reserved_tokens)
            if wait_time > 0:
                logger.debug("llm.rate_limit_wait", seconds=round(wait_time, 2), label=label)
                if wait_time > 1.0:
                    await asyncio.sleep(wait_time)

            try:
                kwargs: dict[str, Any] = {
                    "model": model_id,
                    "messages": messages,
                    "temperature": temp,
                }
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens

                if response_format is not None:
                    if (
                        isinstance(response_format, type)
                        and issubclass(response_format, BaseModel)
                        and not json_schema_fallback
                    ):
                        kwargs["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {
                                "name": response_format.__name__,
                                "schema": response_format.model_json_schema(),
                            },
                        }
                    elif response_format == "json_object" or (
                        isinstance(response_format, type)
                        and issubclass(response_format, BaseModel)
                        and json_schema_fallback
                    ):
                        kwargs["response_format"] = {"type": "json_object"}
                        if not any("json" in str(m.get("content", "")).lower() for m in messages):
                            messages = list(messages)
                            messages.append(
                                {
                                    "role": "system",
                                    "content": "You MUST respond with valid JSON only.",
                                }
                            )
                            kwargs["messages"] = messages

                if stream:
                    return self._stream_completion(  # type: ignore[return-value]
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

                if response.usage is not None:
                    total_tokens = response.usage.total_tokens or 0
                    self._stats["total_tokens"] += total_tokens
                    self._total_tokens += total_tokens

                    # Settle the reservation against actual usage so the
                    # sliding window reflects reality (not just estimates).
                    await self._rate_limiter.settle(reservation, total_tokens)

                content = response.choices[0].message.content or ""

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
                await self._rate_limiter.cancel(reservation)
                raise LLMAuthenticationError(
                    f"LLM authentication failed: {exc}",
                    original=exc,
                ) from exc

            except RateLimitError as exc:
                self._stats["total_errors"] += 1
                self._stats["total_retries"] += 1
                last_error = exc
                await self._rate_limiter.cancel(reservation)

                retry_after = parse_retry_after(str(exc))
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
                await self._rate_limiter.cancel(reservation)

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
                await self._rate_limiter.cancel(reservation)

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
                await self._rate_limiter.cancel(reservation)

                # Structured-output fallback: retry once with `json_object`
                # when the model rejects the strict `json_schema` format.
                if (
                    isinstance(response_format, type)
                    and issubclass(response_format, BaseModel)
                    and not json_schema_fallback
                    and getattr(exc, "status_code", None) == 400
                    and any(
                        needle in str(exc).lower()
                        for needle in (
                            "response format",
                            "json_schema",
                            "structured output",
                            "response_format",
                        )
                    )
                ):
                    json_schema_fallback = True
                    logger.warning(
                        "llm.json_schema_fallback",
                        label=label,
                        model=model_id,
                        error=str(exc),
                    )
                    continue

                self._stats["total_retries"] += 1
                last_error = exc

                if attempt < self._max_retries:
                    # Provider-side IP/account blocks (Groq's "Access denied.
                    # Please check your network settings.") are usually
                    # temporary abuse-protection cooldowns.  Back off longer
                    # so the pipeline can ride out the block instead of
                    # failing after a few seconds.
                    err_text = str(exc).lower()
                    access_denied = (
                        getattr(exc, "status_code", None) == 403
                        and "access denied" in err_text
                        and "network settings" in err_text
                    )
                    backoff = 60.0 if access_denied else 2.0**attempt
                    logger.warning(
                        "llm.api_error",
                        label=label,
                        attempt=attempt,
                        status_code=getattr(exc, "status_code", None),
                        backoff_seconds=round(backoff, 1),
                        access_denied=access_denied,
                        hint=(
                            "Provider-side block detected. Pausing longer "
                            "between retries; the block may be temporary."
                            if access_denied
                            else None
                        ),
                    )
                    await asyncio.sleep(backoff)
                    continue

                raise LLMError(
                    f"API error after {self._max_retries} retries: {exc}",
                    original=exc,
                ) from exc

            except BaseException:
                # Safety net: never leave the token budget drained by an
                # unexpected failure (covers asyncio.CancelledError too).
                await self._rate_limiter.cancel(reservation)
                raise

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
        ``self._max_retries`` times.
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
        # max_retries=0 means 1 attempt (no retries), max_retries=1 means 2 attempts (1 retry), etc.
        max_attempts = self._max_retries + 1

        for attempt in range(1, max_attempts + 1):
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
                retry_after = parse_retry_after(str(exc))
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
        """
        for attempt_fn in (
            lambda: json.loads(content),
            lambda: json.loads(extract_json_from_text(content)),
        ):
            try:
                data = attempt_fn()  # type: ignore[no-untyped-call]
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, TypeError):
                continue

        extracted = extract_json_from_text(content)
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
        """
        return await self.chat_completion(  # type: ignore[return-value]
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
        """Shortcut for structured classification."""
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
        """Reset the per-run token counter."""
        self._total_tokens = 0
        logger.debug("llm.spend_reset")

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        await self._http_client.aclose()
        logger.info(
            "llm.closed",
            total_requests=self._stats["total_requests"],
            total_tokens=self._stats["total_tokens"],
        )
