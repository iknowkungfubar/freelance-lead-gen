"""Response parsing utilities for LLM output."""

from __future__ import annotations as _annotations

import re


def extract_json_from_text(text: str) -> str:
    """Extract a JSON object or array from arbitrary text.

    Handles markdown code fences, leading/trailing text, and backticks.

    Parameters
    ----------
    text : str
        Raw text that may contain JSON somewhere.

    Returns
    -------
    str
        The extracted JSON string, or the original text if no JSON-like
        structure was found.

    """
    match = re.search(
        r"```(?:json)?\s*\\n?(.+?)\\n?```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip()
        return candidate

    for delim_char, other_char in (("{", "}"), ("[", "]")):
        start = text.find(delim_char)
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == delim_char:
                    depth += 1
                elif text[i] == other_char:
                    depth -= 1
                    if depth == 0:
                        return text[start : i + 1]

    return text.strip()


def parse_retry_after(error_text: str) -> float:
    """Extract the retry-after duration from a rate-limit error message.

    Parameters
    ----------
    error_text : str
        The error message from the API.

    Returns
    -------
    float
        Seconds to wait, defaulting to 5.0 if parsing fails.

    """
    patterns = [
        r"retry\s*(?:after|in)?\s*(\d+(?:\.\d+)?)\s*(?:seconds|secs|s)?",
        r"Retry-After:\s*(\d+)",
        r"RetryAfter\s*[:=]\s*(\d+)",
        r"try\s*again\s*in\s*(\d+(?:\.\d+)?)\s*s",
    ]
    for pattern in patterns:
        match = re.search(pattern, error_text, re.IGNORECASE)
        if match:
            return float(match.group(1))

    return 5.0
