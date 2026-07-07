"""Convenience factory for ManagedBrowser."""

from __future__ import annotations as _annotations

from contextlib import asynccontextmanager
from typing import Any

from freelance_lead_gen.discovery.browser._client import ManagedBrowser


@asynccontextmanager
async def create_browser(**kwargs: Any) -> ManagedBrowser:
    """Context-manager factory for a :class:`ManagedBrowser`.

    Usage::

        async with create_browser(headless=False) as browser:
            await browser.navigate("https://upwork.com")
    """
    browser = ManagedBrowser(**kwargs)
    try:
        await browser.start()
        yield browser
    finally:
        await browser.stop()
