"""Shared provider credential resolution from Settings.

Single source of truth for "which API key / base URL does provider X use".
Reused by ``WorkspaceManager`` (which raises when the key is required for a run)
and by model validation (which tolerates a missing key — the transform that
validation needs does not require one).
"""
from __future__ import annotations

from typing import Optional

from app.core.config import Settings
from app.core.enums import LLMProvider


def resolve_provider_api_key(provider: str, settings: Settings) -> Optional[str]:
    """Return the configured API key for ``provider`` (None if unset).

    OpenRouter and Anthropic have dedicated fields; any other provider follows the
    generic ``{PROVIDER}_API_KEY`` settings attribute convention. ``LLMProvider`` is a
    ``StrEnum``, so these comparisons also match plain string ``provider`` arguments.
    """
    if provider == LLMProvider.ANTHROPIC:
        return settings.ANTHROPIC_API_KEY
    if provider == LLMProvider.OPENROUTER:
        return getattr(settings, "OPENROUTER_API_KEY", None)
    return getattr(settings, f"{provider.upper()}_API_KEY", None)


def resolve_provider_base_url(provider: str, settings: Settings) -> Optional[str]:
    """Return the configured base URL override for ``provider`` (None to use default)."""
    base_url = getattr(settings, f"{provider.upper()}_BASE_URL", None)
    return base_url if base_url else None
