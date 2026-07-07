"""OpenRouter HTTP client — base URL, auth headers, and catalog requests.

Single place for all OpenRouter API I/O so other modules stay free of httpx and
URL/auth construction.
"""

from __future__ import annotations

import httpx

from app.core.config import Settings, is_key_valid


def base_url(settings: Settings) -> str:
    return (settings.OPENROUTER_BASE_URL or "https://openrouter.ai/api").rstrip("/")


def auth_headers(settings: Settings) -> dict[str, str]:
    if not is_key_valid(settings.OPENROUTER_API_KEY):
        return {}
    return {"Authorization": f"Bearer {settings.OPENROUTER_API_KEY.strip()}"}


async def fetch_models(settings: Settings) -> list[dict]:
    """GET /v1/models — returns the raw ``data[]`` array.

    Raises ``httpx.HTTPStatusError`` on non-2xx responses so callers can
    decide whether to retry or fall back.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{base_url(settings)}/v1/models",
            headers=auth_headers(settings),
        )
        resp.raise_for_status()
        return resp.json().get("data") or []
