"""Token estimation and message token counting.

Provides fast token counting with optional tiktoken integration.
"""

from __future__ import annotations as _annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string using a rough heuristic.

    Uses ``len(text) // 4`` as a fast approximation when tiktoken is not
    available or for models not in the tiktoken cache.  This is within
    ~20% of actual token counts for English text.
    """
    return max(1, len(text) // 4)


def count_message_tokens(
    messages: list[ChatCompletionMessageParam],
    model: str = "gpt-4",
) -> int:
    """Count the total token usage for a list of chat messages.

    Attempts to use **tiktoken** if available and a known model is found,
    falling back to heuristic estimation otherwise.

    Parameters
    ----------
    messages : list of ChatCompletionMessageParam
        The messages to count.
    model : str
        The model identifier (used to select the tiktoken encoding).

    Returns
    -------
    int
        Estimated or exact token count.

    """
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        tokens_per_message = 3
        tokens_per_name = 1

        total = 0
        for msg in messages:
            total += tokens_per_message
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = " ".join(text_parts)
            total += len(encoding.encode(str(role)))
            total += len(encoding.encode(str(content)))
            if msg.get("name"):
                total += tokens_per_name

        total += 3
        return total
    except ImportError:
        total = 0
        for msg in messages:
            for v in msg.values():
                if isinstance(v, str):
                    total += estimate_tokens(v)
                elif isinstance(v, list):
                    for part in v:
                        if isinstance(part, dict) and part.get("type") == "text":
                            total += estimate_tokens(part.get("text", ""))
        return total
