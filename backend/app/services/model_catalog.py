"""Provider-agnostic model catalog fetching.

Single entry point for "which models does the active provider offer right now".
Each provider has its own fetcher with a 1-hour cache; results feed
``app.services.model_validation`` which decides whether a configured model is
usable.

OpenRouter fetching already exists in ``openrouter_models`` — the OpenRouter
fetcher here delegates to it rather than duplicating the HTTP call. The Anthropic
fetcher is new: it calls ``GET {base}/v1/models`` with ``x-api-key`` +
``anthropic-version`` headers and follows pagination.

All fetchers are **permissive on failure**: any error (or a missing key) yields an
empty set, which downstream code treats as ``UNVERIFIED`` rather than invalid, so a
transient catalog outage never blocks a generation run.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, Set

import httpx

from app.core.config import Settings
from app.core.enums import LLMProvider
from app.services.openrouter_models import ModelCacheManager, fetch_available_models

logger = logging.getLogger(__name__)

# Anthropic Messages API version pin (same value the SDK uses).
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
# Safety cap on pagination loops so a misbehaving endpoint can't spin forever.
_MAX_PAGES = 20


class ProviderCatalogFetcher(ABC):
    """Fetches the set of available model ids for one provider, with caching."""

    def __init__(self) -> None:
        self._cache = ModelCacheManager()

    def clear_cache(self) -> None:
        self._cache.clear_cache()

    async def fetch(self, settings: Settings) -> Set[str]:
        """Return the provider's model-id set (cached 1h). Empty set on failure."""
        if self._cache.is_cache_valid():
            cached = self._cache.get_cached_models()
            if cached:
                return cached[0]
        models = await self._fetch_uncached(settings)
        if models:
            self._cache.set_cache(models)
        return models

    @abstractmethod
    async def _fetch_uncached(self, settings: Settings) -> Set[str]:
        """Provider-specific fetch. Must be permissive (return set() on failure)."""


class OpenRouterCatalogFetcher(ProviderCatalogFetcher):
    """Delegates to ``openrouter_models.fetch_available_models`` (already cached + permissive)."""

    async def _fetch_uncached(self, settings: Settings) -> Set[str]:
        # openrouter_models owns its own cache; this wrapper keeps the unified
        # interface without a second HTTP implementation.
        return await fetch_available_models(settings)


class AnthropicCatalogFetcher(ProviderCatalogFetcher):
    """Fetches the live Anthropic model catalog via GET /v1/models."""

    async def _fetch_uncached(self, settings: Settings) -> Set[str]:
        api_key = (settings.ANTHROPIC_API_KEY or "").strip()
        if not api_key:
            # Without a key we cannot verify — stay permissive (UNVERIFIED downstream).
            logger.debug("No ANTHROPIC_API_KEY set; skipping Anthropic catalog fetch")
            return set()

        base = (settings.ANTHROPIC_BASE_URL or _ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        models: Set[str] = set()
        params: dict[str, str] = {"limit": "1000"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for _ in range(_MAX_PAGES):
                    response = await client.get(
                        f"{base}/v1/models", headers=headers, params=params
                    )
                    response.raise_for_status()
                    data = response.json()
                    models.update(
                        item["id"] for item in data.get("data", []) if item.get("id")
                    )
                    if not data.get("has_more"):
                        break
                    last_id = data.get("last_id")
                    if not last_id:
                        break
                    params = {"limit": "1000", "after_id": last_id}
            return models
        except Exception as e:  # permissive: never block on a catalog outage
            logger.warning(f"Failed to fetch Anthropic models: {e}")
            return set()


_FETCHER_REGISTRY: Dict[LLMProvider, ProviderCatalogFetcher] = {
    LLMProvider.OPENROUTER: OpenRouterCatalogFetcher(),
    LLMProvider.ANTHROPIC: AnthropicCatalogFetcher(),
}


async def fetch_catalog_for_provider(
    provider: LLMProvider, settings: Settings
) -> Set[str]:
    """Fetch the live model-id set for ``provider`` (empty set if unknown/unavailable)."""
    fetcher = _FETCHER_REGISTRY.get(provider)
    if fetcher is None:
        logger.warning(f"No catalog fetcher registered for provider '{provider}'")
        return set()
    return await fetcher.fetch(settings)


def clear_catalog_caches() -> None:
    """Clear all provider catalog caches (used by tests)."""
    for fetcher in _FETCHER_REGISTRY.values():
        fetcher.clear_cache()
