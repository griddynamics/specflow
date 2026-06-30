"""
Minimal synchronous OpenRouter chat completions — one user turn, text in / text out.

Uses the OpenAI-compatible `/v1/chat/completions` endpoint and `OPENROUTER_API_KEY`.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import LLM_LOW_DEFAULT_FIRST_MODEL

# Same default as OpenRouterProvider.DEFAULT_BASE_URL (api root, no trailing slash on path join).
OPENROUTER_DEFAULT_API_BASE: str = "https://openrouter.ai/api"

_logger = logging.getLogger(__name__)


def openrouter_first_model_from_llm_low_csv(llm_low: str) -> str:
    """
    First model id from comma-separated LLM_LOW (OpenRouter `provider/model` names).

    On any error or empty first segment, returns ``LLM_LOW_DEFAULT_FIRST_MODEL`` (same as ``Settings.LLM_LOW`` default).
    """
    default = LLM_LOW_DEFAULT_FIRST_MODEL
    try:
        raw = "" if llm_low is None else str(llm_low)
        first = raw.split(",")[0].strip()
        if first:
            return first
    except Exception as e:
        _logger.warning(
            "open_router_api: could not derive first model from LLM_LOW %r: %s; using %s",
            llm_low,
            e,
            default,
        )
    return default


def _assistant_text_from_chat_payload(
    payload: dict,
    *,
    log: Optional[logging.Logger] = None,
) -> str:
    """Extract assistant text from OpenRouter/OpenAI-style chat completion JSON."""
    lg = log or _logger
    choices = payload.get("choices") or []
    if not choices:
        lg.warning(
            "open_router_api: chat completion has no choices (keys=%s)",
            list(payload.keys()),
        )
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts).strip()
    if not text:
        lg.warning(
            "open_router_api: chat completion returned empty assistant text (finish_reason=%s)",
            choices[0].get("finish_reason"),
        )
    return text


def openrouter_one_shot_text(
    *,
    api_key: str,
    model: str,
    user_message: str,
    system: Optional[str] = None,
    max_tokens: int = 2048,
    base_url: Optional[str] = None,
    app_name: Optional[str] = None,
    timeout_s: float = 120.0,
    log: Optional[logging.Logger] = None,
) -> str:
    """
    Single non-streaming chat completion; returns assistant message text.

    Raises httpx.HTTPError on HTTP failures.
    """
    lg = log or _logger
    root = (base_url or OPENROUTER_DEFAULT_API_BASE).rstrip("/")
    url = f"{root}/v1/chat/completions"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if app_name:
        headers["X-Title"] = app_name

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_message})

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }

    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()

    return _assistant_text_from_chat_payload(data, log=lg)
