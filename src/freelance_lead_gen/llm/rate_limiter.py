"""Rate limiters for LLM API throttling.

Two strategies are provided:

- :class:`TokenBucket` — classic rate-based throttle (tokens per second).
- :class:`SlidingWindowLimiter` — rolling 60-second window that enforces an
  exact token-per-minute ceiling, settling reservations against *actual*
  usage once a request completes.
"""

from __future__ import annotations as _annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Simple token-bucket rate limiter.

    Maintains a bucket of *capacity* tokens, refilling at *rate* tokens per
    second.  Each request consumes one token by default; callers may also
    consume a *weighted* number of tokens (e.g. estimated output tokens) via
    :meth:`acquire_weighted`, and refund unused reserves via :meth:`credit`.
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
        """Wait for a single token and return the wait time in seconds."""
        return await self.acquire_weighted(1.0)

    async def acquire_weighted(self, weight: float) -> float:
        """Wait until *weight* tokens are available and consume them.

        Returns the wait time in seconds.
        """
        weight = max(weight, 0.0)
        async with self._lock:
            self._refill()
            if self._tokens >= weight:
                self._tokens -= weight
                return 0.0
            wait = (weight - self._tokens) / max(self.rate, 0.001)
            self._tokens = 0.0
            self._last_refill = time.monotonic()
            return wait

    async def credit(self, weight: float) -> None:
        """Return unused reserved tokens to the bucket."""
        weight = max(weight, 0.0)
        if weight <= 0.0:
            return
        async with self._lock:
            self._refill()
            self._tokens = min(float(self.capacity), self._tokens + weight)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last_refill = now


@dataclass
class _Reservation:
    """A single in-flight admission to the sliding window."""

    ts: float  # monotonic admission time
    tokens: float  # reserved (estimate) then settled (actual) token count


class SlidingWindowLimiter:
    """Rolling-window token-per-minute rate limiter.

    Admits a request only if the sum of reservations inside the last 60
    seconds (plus the new request's estimate) stays under
    ``max_tokens_per_minute``.  Once a request completes, :meth:`settle`
    replaces the reserved estimate with the actual token usage so the window
    reflects reality.  Failed requests are removed via :meth:`cancel`.

    This prevents burst overshoot on providers with hard TPM ceilings (e.g.
    Groq's free tier of 12,000) even when estimates are imperfect.
    """

    def __init__(self, max_tokens_per_minute: int) -> None:
        self._limit: float = float(max_tokens_per_minute)
        self._reservations: deque[_Reservation] = deque()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self, estimate: float) -> tuple[float, _Reservation]:
        """Admit a request reserving *estimate* tokens.

        Returns a tuple of ``(wait_seconds, reservation)``.  The caller must
        sleep for *wait_seconds* before issuing the request, and must
        eventually call :meth:`settle` (on success) or :meth:`cancel` (on
        failure) with the returned reservation.
        """
        estimate = max(float(estimate), 0.0)
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            total = self._window_tokens()

            wait = 0.0
            if total + estimate > self._limit:
                deficit = (total + estimate) - self._limit
                accrued = 0.0
                wait = 60.0
                for res in self._reservations:
                    accrued += res.tokens
                    if accrued >= deficit:
                        wait = max(0.0, res.ts + 60.0 - now)
                        break

            reservation = _Reservation(now, estimate)
            self._reservations.append(reservation)
            return wait, reservation

    async def settle(self, reservation: _Reservation, actual: float) -> None:
        """Record the *actual* token usage for a completed request."""
        async with self._lock:
            reservation.tokens = max(float(actual), 0.0)

    async def cancel(self, reservation: _Reservation) -> None:
        """Remove a failed request's reservation from the window."""
        async with self._lock:
            try:
                self._reservations.remove(reservation)
            except ValueError:
                pass

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._reservations and self._reservations[0].ts <= cutoff:
            self._reservations.popleft()

    def _window_tokens(self) -> float:
        return sum(res.tokens for res in self._reservations)
