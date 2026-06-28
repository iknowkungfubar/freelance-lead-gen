"""Mock OpenAI-compatible chat completions server for integration testing.

Provides a lightweight :class:`MockLLMServer` that starts on a random port
and responds to ``POST /v1/chat/completions`` with realistic JSON responses.
Supports configurable latency, error simulation (rate limit, server error),
and request counting so tests can verify retry behaviour.
"""

from __future__ import annotations as _annotations

import asyncio
import json
import time

# ── Default response payloads ──────────────────────────────────────────────────

# Qualification response (used by FilteringPipeline / _LLMClassification).
_DEFAULT_QUALIFICATION_CONTENT = json.dumps({
    "qualified": True,
    "score": 85,
    "skill_match_score": 90,
    "budget_fit_score": 75,
    "clarity_score": 80,
    "reasoning": "Strong match: skills and experience align well with the target profile.",
    "risks": [],
})

# Draft-generation response (used by PersonalizationAgent / _DraftGeneration).
_DEFAULT_DRAFT_CONTENT = json.dumps({
    "subject": "AI proposal for your RAG pipeline",
    "body": (
        "Hi there,\n\n"
        "I noticed your post about building a RAG pipeline and wanted to reach out. "
        "I have been working with LangChain and vector databases for the past few "
        "years, helping teams set up production-grade retrieval systems.\n\n"
        "One project that seems relevant: I built a customer-support RAG system "
        "that cut response times by 60%% using Pinecone and GPT-4. Happy to share "
        "more details if that sounds useful.\n\n"
        "What kind of data sources are you planning to index first?"
    ),
    "version": 1,
    "platform_adaptations": [],
})

_DEFAULT_CHAT_COMPLETION_BODY: dict = {
    "id": "chatcmpl-mock-0000000000000",
    "object": "chat.completion",
    "created": 0,  # Filled at response time.
    "model": "mock-model",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": _DEFAULT_QUALIFICATION_CONTENT,
            },
            "finish_reason": "stop",
            "logprobs": None,
        }
    ],
    "usage": {
        "prompt_tokens": 25,
        "completion_tokens": 25,
        "total_tokens": 50,
    },
    "system_fingerprint": None,
}

_RATE_LIMIT_BODY = json.dumps({
    "error": {
        "message": "Rate limit exceeded for test-key on model mock-model. Please retry after 0.1 seconds.",
        "type": "rate_limit_error",
        "code": "rate_limit",
        "param": None,
    }
}).encode()

_SERVER_ERROR_BODY = json.dumps({
    "error": {
        "message": "The server encountered an internal error and was unable to complete your request.",
        "type": "server_error",
        "code": "internal_error",
        "param": None,
    }
}).encode()

_NOT_FOUND_BODY = json.dumps({"error": "Not found"}).encode()

_STATUS_TEXTS = {
    200: "OK",
    429: "Too Many Requests",
    500: "Internal Server Error",
    404: "Not Found",
}


# ── Mock server ─────────────────────────────────────────────────────────────────


class MockLLMServer:
    """A lightweight mock OpenAI-compatible API server for testing.

    Starts an :class:`asyncio.Server` on ``127.0.0.1`` with port ``0`` (random
    available port).  Responds to ``POST /v1/chat/completions`` with a standard
    chat completion JSON response by default.

    Use :meth:`set_error_mode` to simulate API failures and :meth:`set_latency`
    to add artificial delay.

    Parameters
    ----------
    host : str
        Bind address (default ``127.0.0.1``).
    port : int
        Bind port (``0`` = random available port).

    Attributes
    ----------
    request_count : int
        Number of HTTP requests received (resets on :meth:`start`).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._error_mode: str | None = None
        self._latency: float = 0.0
        self.request_count: int = 0

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """Return the full base URL, e.g. ``http://127.0.0.1:54321/v1``.

        Raises :class:`RuntimeError` if the server is not running.
        """
        if self._server is None or not self._server.sockets:
            msg = "MockLLMServer is not running — call start() first"
            raise RuntimeError(msg)
        sock = self._server.sockets[0]
        port = sock.getsockname()[1]
        return f"http://{self._host}:{port}/v1"

    @property
    def port(self) -> int:
        """Return the actual port the server is bound to."""
        if self._server is None or not self._server.sockets:
            msg = "MockLLMServer is not running"
            raise RuntimeError(msg)
        return self._server.sockets[0].getsockname()[1]

    # ── Configuration ──────────────────────────────────────────────────────

    def set_error_mode(self, mode: str | None) -> None:
        """Configure error simulation for subsequent requests.

        Parameters
        ----------
        mode : str or None
            One of ``"rate_limit"``, ``"server_error"``, or ``None`` (normal).
        """
        allowed = {"rate_limit", "server_error", None}
        if mode not in allowed:
            msg = f"error_mode must be one of {allowed}, got {mode!r}"
            raise ValueError(msg)
        self._error_mode = mode

    def set_latency(self, seconds: float) -> None:
        """Add an artificial delay before responding.

        Parameters
        ----------
        seconds : float
            Delay in seconds (0 disables latency).
        """
        self._latency = max(0.0, seconds)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the server on the configured host and port.

        The server runs in the background until :meth:`stop` is called.
        """
        self.request_count = 0
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Stop the server and close all connections."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> MockLLMServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # ── Connection handler ─────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single HTTP connection."""
        self.request_count += 1

        try:
            # ── Simulate configurable latency ──────────────────────────────
            if self._latency > 0:
                await asyncio.sleep(self._latency)

            # ── Read the request line ──────────────────────────────────────
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=30.0,
            )
            if not request_line:
                return

            try:
                method, path, _ = request_line.decode("utf-8", errors="replace").strip().split(" ", 2)
            except ValueError:
                return  # Malformed request line.

            # ── Read headers ───────────────────────────────────────────────
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=30.0,
                )
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    key, value = decoded.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            # ── Read body ──────────────────────────────────────────────────
            content_length = int(headers.get("content-length", 0))
            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=30.0,
                )

            # ── Route the request ──────────────────────────────────────────
            if method == "POST" and path.rstrip("/").endswith("/chat/completions"):
                await self._handle_chat_completion(writer, body=body)
            else:
                await self._send_response(writer, 404, _NOT_FOUND_BODY)

        except (TimeoutError, ConnectionError, BrokenPipeError):
            pass  # Client disconnected or timed out — nothing to do.
        except Exception:
            pass  # Swallow unexpected errors; the server must stay up.
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Request handlers ───────────────────────────────────────────────────

    async def _handle_chat_completion(
        self,
        writer: asyncio.StreamWriter,
        body: bytes = b"",
    ) -> None:
        """Respond to ``POST /v1/chat/completions``.

        Reads the request body to determine whether the client expects a
        qualification response or a draft-generation response, and returns
        the appropriate payload.
        """
        if self._error_mode == "rate_limit":
            await self._send_response(writer, 429, _RATE_LIMIT_BODY)
        elif self._error_mode == "server_error":
            await self._send_response(writer, 500, _SERVER_ERROR_BODY)
        else:
            # Determine response type based on request content.
            content = _DEFAULT_QUALIFICATION_CONTENT
            if body:
                try:
                    req = json.loads(body)
                    messages = req.get("messages", [])
                    all_text = " ".join(
                        m.get("content", "") for m in messages if isinstance(m, dict)
                    ).lower()
                    if "draft" in all_text or "outreach" in all_text:
                        content = _DEFAULT_DRAFT_CONTENT
                except (json.JSONDecodeError, TypeError):
                    pass  # Fall through to default qualification response.
            body = _build_completion_response(content=content)
            await self._send_response(writer, 200, body)

    # ── Response writer ────────────────────────────────────────────────────

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        body: bytes,
    ) -> None:
        """Write an HTTP/1.1 response."""
        status_text = _STATUS_TEXTS.get(status_code, "Unknown")
        header_lines = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
            f"\r\n"
        )
        writer.write(header_lines.encode("utf-8"))
        writer.write(body)
        await writer.drain()


# ── Response builder ────────────────────────────────────────────────────────────


def _build_completion_response(
    content: str | None = None,
    model: str = "mock-model",
) -> bytes:
    """Build a serialised ChatCompletion JSON response.

    Parameters
    ----------
    content : str or None
        The ``choices[0].message.content`` value.  Defaults to a realistic
        qualification response (``qualified=True, score=85``).
    model : str
        The model identifier in the response.

    Returns
    -------
    bytes
        UTF-8-encoded JSON.
    """
    body = dict(_DEFAULT_CHAT_COMPLETION_BODY)
    body["created"] = int(time.time())
    body["model"] = model
    if content is not None:
        body["choices"] = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ]
    return json.dumps(body).encode("utf-8")
