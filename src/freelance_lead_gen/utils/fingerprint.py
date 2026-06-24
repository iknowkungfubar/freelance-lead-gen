"""Browser fingerprint generation utilities.

Generates realistic, varied browser fingerprints to reduce the likelihood of
bot detection when automating platform interactions.  Each fingerprint
includes viewport dimensions, user-agent, timezone, locale, and WebGL
properties that form a self-consistent profile.
"""

from __future__ import annotations as _annotations

import contextlib
import random
from dataclasses import dataclass
from typing import Literal

# ── Constants ────────────────────────────────────────────────────────────────

# Realistic Linux Chrome user-agent strings, sampled from recent versions.
_CHROME_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
)

# Realistic Linux Firefox user-agent strings.
_FIREFOX_UAS: tuple[str, ...] = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
)

# Common English-language locales that pair with en-US / en-GB.
_LOCALES: tuple[str, ...] = (
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-CA,en;q=0.7",
    "en-AU,en;q=0.6",
    "en-US,en;q=0.9,es;q=0.7",
    "en-US,en;q=0.9,de;q=0.8",
)

# Timezone identifiers commonly assigned to Linux machines.
_TIMEZONES: tuple[str, ...] = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Toronto",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Australia/Sydney",
    "Asia/Singapore",
    "Pacific/Auckland",
)

# Common WebGL vendor/renderer pairs (Linux, real GPUs).
_WEBGL_PROFILES: tuple[dict[str, str], ...] = (
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon RX 7900 XT Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "WebKit", "renderer": "Mesa DRI Intel(R) Graphics (ADL GT2)"},
    {"vendor": "WebKit", "renderer": "Mesa DRI NVIDIA GeForce RTX 3080 PCIe SS Sel (NVIDIA)"},
)

# ── Data class ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrowserFingerprint:
    """A self-consistent browser fingerprint.

    All generated fingerprints contain internally consistent properties —
    for example, a Chrome UA always pairs with a Chrome-compatible WebGL
    profile and appropriate viewport ranges.
    """

    user_agent: str
    viewport_width: int
    viewport_height: int
    timezone: str
    locale: str
    webgl_vendor: str
    webgl_renderer: str
    browser_type: Literal["chrome", "firefox"]

    def as_dict(self) -> dict[str, str | int]:
        """Return the fingerprint as a plain dictionary (JSON-serialisable)."""
        return {
            "user_agent": self.user_agent,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "timezone": self.timezone,
            "locale": self.locale,
            "webgl_vendor": self.webgl_vendor,
            "webgl_renderer": self.webgl_renderer,
            "browser_type": self.browser_type,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _gaussian_viewport(
    mean_w: int = 1920,
    std_w: int = 120,
    mean_h: int = 1080,
    std_h: int = 80,
) -> tuple[int, int]:
    """Sample a viewport size from a Gaussian distribution.

    Values are clamped to realistic modern-monitor ranges.
    """
    w = max(1024, min(2560, int(random.gauss(mean_w, std_w))))
    h = max(768, min(1600, int(random.gauss(mean_h, std_h))))
    return w, h


def _padded_ua_fuzz(ua: str) -> str:
    """Add minor, realistic whitespace / casing variations to a UA string.

    Some bot detectors fingerprint the exact UA-to-header ratio, so a tiny
    amount of noise helps with profile diversity.
    """
    if random.random() < 0.15:
        # Slightly alter the Chrome version number by ±1 in the last segment.
        parts = ua.split(" ")
        for i, p in enumerate(parts):
            if p.startswith(("Chrome/", "Firefox/")):
                base, version = p.split("/")
                vernums = version.split(".")
                with contextlib.suppress(ValueError, IndexError):
                    vernums[-1] = str(max(0, int(vernums[-1]) + random.choice([-1, 0, 1])))
                parts[i] = f"{base}/{'.'.join(vernums)}"
                break
        ua = " ".join(parts)
    return ua


def _random_locale() -> str:
    """Pick a random locale from the predefined set."""
    return random.choice(_LOCALES)


def _random_timezone() -> str:
    """Pick a random timezone from the predefined set."""
    return random.choice(_TIMEZONES)


def _random_webgl() -> dict[str, str]:
    """Pick a random WebGL vendor/renderer profile."""
    return dict(random.choice(_WEBGL_PROFILES))


# ── Public API ───────────────────────────────────────────────────────────────


def generate_fingerprint(
    browser: Literal["chrome", "firefox"] | None = None,
    *,
    viewport: tuple[int, int] | None = None,
    timezone: str | None = None,
    locale: str | None = None,
) -> BrowserFingerprint:
    """Generate a random, internally-consistent browser fingerprint.

    Parameters
    ----------
    browser : str or None
        ``"chrome"`` or ``"firefox"``.  When *None* (default) one is chosen
        at random.
    viewport : tuple of (int, int) or None
        Explicit (width, height).  When *None* a Gaussian-sampled viewport
        is used.
    timezone : str or None
        Explicit IANA timezone.  When *None* one is chosen at random.
    locale : str or None
        Explicit ``Accept-Language`` string.  When *None* one is chosen at
        random.

    Returns
    -------
    BrowserFingerprint
        A frozen dataclass with all fingerprint attributes.

    """
    if browser is None:
        browser = random.choice(["chrome", "firefox"])

    # Pick a user-agent for the selected browser family.
    if browser == "chrome":
        ua = _padded_ua_fuzz(random.choice(_CHROME_UAS))
    else:
        ua = _padded_ua_fuzz(random.choice(_FIREFOX_UAS))

    # Viewport.
    if viewport is not None:
        vp_w, vp_h = viewport
    else:
        vp_w, vp_h = _gaussian_viewport()

    tz = timezone or _random_timezone()
    loc = locale or _random_locale()
    webgl = _random_webgl()

    return BrowserFingerprint(
        user_agent=ua,
        viewport_width=vp_w,
        viewport_height=vp_h,
        timezone=tz,
        locale=loc,
        webgl_vendor=webgl["vendor"],
        webgl_renderer=webgl["renderer"],
        browser_type=browser,
    )


def fingerprint_to_playwright_kwargs(fp: BrowserFingerprint) -> dict:
    """Map a :class:`BrowserFingerprint` to Playwright ``new_context`` kwargs.

    This produces the *extra_http_headers*, *viewport*, *locale*, and
    *timezone_id* arguments that make a Playwright browser context resemble
    the fingerprint.
    """
    return {
        "user_agent": fp.user_agent,
        "viewport": {"width": fp.viewport_width, "height": fp.viewport_height},
        "locale": fp.locale.split(",")[0] if "," in fp.locale else fp.locale,
        "timezone_id": fp.timezone,
        "extra_http_headers": {
            "Accept-Language": fp.locale,
            "Sec-CH-UA": (
                '"Google Chrome";v="125", "Chromium";v="125", "Not_A Brand";v="24"'
                if fp.browser_type == "chrome"
                else '"Firefox";v="127", "Firefox";v="127"'
            ),
        },
    }
