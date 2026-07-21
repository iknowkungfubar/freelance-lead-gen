"""Structured logging configuration using structlog.

Provides a pre-configured logger factory with JSON-friendly output,
context-variable tracing for request/opportunity IDs, and console rendering
for interactive use.
"""

from __future__ import annotations as _annotations

import os
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# ── Context variable for tracing ─────────────────────────────────────────────

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
"""Context variable holding the current trace / opportunity ID for correlation."""


def get_trace_id() -> str | None:
    """Return the active trace ID, or *None* if no trace is active."""
    return trace_id_var.get()


def set_trace_id(trace_id: str | None) -> None:
    """Set the active trace ID for the current async context."""
    if trace_id is None:
        trace_id_var.set(None)
    else:
        trace_id_var.set(trace_id)


# ── Processor helpers ────────────────────────────────────────────────────────


def _add_trace_id(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Inject the active *trace_id* into every log event, if present."""
    tid = get_trace_id()
    if tid is not None:
        event_dict["trace_id"] = tid
    return event_dict


def _drop_debug_keys(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Drop internal structlog keys so output stays clean."""
    event_dict.pop("_record", None)
    event_dict.pop("_from_structlog", None)
    return event_dict


# ── Public API ───────────────────────────────────────────────────────────────


def configure_logging(*, level: str | None = None, json: bool | None = None) -> None:
    """Configure structlog once at application startup.

    Parameters
    ----------
    level : str, optional
        Log level name (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``).
        Defaults to the *LOG_LEVEL* environment variable or ``INFO``.
    json : bool, optional
        If *True*, emit newline-delimited JSON (ideal for production).
        If *False*, emit coloured console output (ideal for development).
        Defaults to ``False`` unless the *JSON_LOGS* env var is set to ``true``.

    """
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if json is None:
        json = os.environ.get("JSON_LOGS", "false").lower() == "true"

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _add_trace_id,
        _drop_debug_keys,
    ]

    if json:
        # Production: structured JSON lines.
        processors: list[structlog.types.Processor] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: coloured console output.
        processors = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(
                exception_formatter=structlog.dev.rich_traceback,
                sort_keys=False,
                force_colors=sys.stdout.isatty(),
            ),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Tune the standard-library ``logging`` root logger to match so that
    # third-party libraries using plain ``logging`` also respect our level.
    import logging as stdlib_logging

    stdlib_logging.basicConfig(handlers=[], force=True, level=getattr(stdlib_logging, level, stdlib_logging.INFO))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to *name* (default: caller's module).

    Typical usage::

        from freelance_lead_gen.utils.logging import get_logger

        logger = get_logger(__name__)
        logger.info("opportunity.discovered", platform="upwork", title="...")
    """
    return structlog.get_logger(name)  # type: ignore[return-type]


def bind_opportunity_context(
    logger: structlog.stdlib.BoundLogger,
    opportunity_id: str | None = None,
    platform: str | None = None,
    platform_job_id: str | None = None,
) -> structlog.stdlib.BoundLogger:
    """Return a logger with opportunity-specific context pre-bound.

    This is a convenience wrapper — the same effect can be achieved by
    passing key/value pairs to individual log calls.
    """
    kwargs: dict[str, Any] = {}
    if opportunity_id is not None:
        kwargs["opportunity_id"] = opportunity_id
    if platform is not None:
        kwargs["platform"] = platform
    if platform_job_id is not None:
        kwargs["platform_job_id"] = platform_job_id
    return logger.bind(**kwargs)
