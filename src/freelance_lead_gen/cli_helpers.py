"""CLI helper utilities for freelance-lead-gen.

Extracted from cli.py for independent testability.
"""

from __future__ import annotations

import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import yaml

from freelance_lead_gen.config.settings import AppConfig

logger = logging.getLogger(__name__)


def safe_error(msg: str, exc: Exception) -> None:
    """Log a safe error message (no exception details leaked)."""
    logger.error(msg)
    logger.debug("Error details", exc_info=exc)


def validate_settings(*, require_llm_key: bool = True) -> list[str]:
    """Validate required settings are present. Returns list of missing keys."""
    missing: list[str] = []
    config_path = Path("config.yaml")

    if not config_path.exists():
        missing.append("config.yaml")
        missing.append("LLM_API_KEY")
        return missing

    with Path(config_path).open() as f:
        config: dict[str, Any] = yaml.safe_load(f) or {}
        llm_key = config.get("llm", {}).get("api_key") or os.getenv("LLM_API_KEY")
        if require_llm_key and not llm_key:
            missing.append("LLM_API_KEY")

    return missing


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal health check HTTP handler."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def create_health_server(port: int = 8080) -> HTTPServer:
    """Create a health check HTTP server on the given port."""
    from functools import partial

    server = HTTPServer(("127.0.0.1", port), partial(HealthHandler, AppConfig))
    logger.info("Health server listening on port %d", port)
    return server


def write_dotenv(key: str, value: str, path: str = ".env") -> None:
    """Write or update a key=value in a .env file."""
    env_path = Path(path)

    if env_path.exists():
        lines = env_path.read_text().splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        env_path.write_text(f"{key}={value}\n")
