"""Browser automation package - Playwright wrapper with stealth and jitter.

Re-exports all public symbols from the original ``browser.py`` module
so existing imports continue to work unchanged.
"""

from __future__ import annotations as _annotations

from freelance_lead_gen.discovery.browser._client import ManagedBrowser
from freelance_lead_gen.discovery.browser._models import BrowserSessionInfo
from freelance_lead_gen.discovery.browser._exceptions import (
    BrowserError,
    BrowserNotStartedError,
    NavigationTimeoutError,
)
from freelance_lead_gen.discovery.browser._factory import create_browser

__all__ = [
    "BrowserError",
    "BrowserNotStartedError",
    "BrowserSessionInfo",
    "ManagedBrowser",
    "NavigationTimeoutError",
    "create_browser",
]
