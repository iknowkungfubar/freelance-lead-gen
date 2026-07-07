"""Behavioural jitter constants and module-level configuration."""

from __future__ import annotations as _annotations

# -- Behavioural jitter constants --

_DEFAULT_JITTER_MEAN: float = 3.0
"""Mean delay (seconds) between automated actions."""

_DEFAULT_JITTER_SIGMA: float = 1.2
"""Standard deviation of the Gaussian delay."""

_MIN_JITTER: float = 0.3
"""Floor for any individual delay - never go faster than this."""

_MAX_JITTER: float = 12.0
"""Ceiling for any individual delay - cap for safety."""

_RETRY_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
"""HTTP status codes that trigger a retry."""
