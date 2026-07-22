"""Token-bucket rate limiter for API request throttling."""

from __future__ import annotations as _annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
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
            wait = (1.0 - self._tokens) / max(self.rate, 0.001)
            self._tokens = 0.0
            self._last_refill = time.monotonic()
            return wait

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last_refill = now
