"""
Minimal synchronous Anthropic Messages API — one user turn, text in / text out.

Used for small direct-Anthropic tasks where Claude Code SDK is unnecessary.
"""

from __future__ import annotations

from typing import Optional

from anthropic import Anthropic


def _message_assistant_text(message: object) -> str:
    parts: list[str] = []
    content = getattr(message, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


def anthropic_one_shot_text(
    *,
    api_key: str,
    model: str,
    user_message: str,
    system: Optional[str] = None,
    max_tokens: int = 2048,
    base_url: Optional[str] = None,
) -> str:
    """
    Single non-streaming messages.create; returns concatenated assistant text blocks.

    Raises anthropic API errors on failure (caller handles).
    """
    client = (
        Anthropic(api_key=api_key, base_url=base_url)
        if base_url
        else Anthropic(api_key=api_key)
    )
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
    }
    if system:
        kwargs["system"] = system
    message = client.messages.create(**kwargs)
    return _message_assistant_text(message)
