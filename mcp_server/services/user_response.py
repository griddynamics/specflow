"""Shape MCP tool JSON for chat: short ``message``, rich ``details``."""

from __future__ import annotations

import json
import re
from typing import Any


def brief_sentences(text: str, *, max_sentences: int = 2) -> str:
    """Return at most ``max_sentences`` sentences from ``text``."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    kept = [p.strip() for p in parts if p.strip()][:max_sentences]
    return " ".join(kept) if kept else cleaned


def chat_payload(
    message: str,
    *,
    details: dict[str, Any] | None = None,
    **top_level: Any,
) -> dict[str, Any]:
    """Build a tool response dict with a brief user-facing ``message`` and optional ``details``."""
    payload: dict[str, Any] = {"message": brief_sentences(message)}
    payload.update(top_level)
    if details is not None:
        payload["details"] = details
    return payload


def chat_json(
    message: str,
    *,
    details: dict[str, Any] | None = None,
    **top_level: Any,
) -> str:
    """Serialize :func:`chat_payload` for MCP tool return values."""
    return json.dumps(
        chat_payload(message, details=details, **top_level),
        indent=2,
    )
