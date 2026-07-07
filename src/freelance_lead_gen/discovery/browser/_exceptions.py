"""Browser automation exception classes."""

from __future__ import annotations as _annotations


class BrowserError(RuntimeError):
    """Generic browser automation error."""

    def __init__(self, message: str, original: Exception | None = None) -> None:
        self.original = original
        super().__init__(message)


class BrowserNotStartedError(RuntimeError):
    """Raised when an action is attempted before the browser is started."""

    def __init__(self) -> None:
        super().__init__(
            "Browser is not started -- call start() or use 'async with' first"
        )


class NavigationTimeoutError(BrowserError):
    """Raised when a navigation or page action times out."""
