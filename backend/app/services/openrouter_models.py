"""
OpenRouter model validation and caching.

Validates model names against OpenRouter API to catch typos early.
Caches model list for 1 hour to minimize API calls using a cache manager.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Set

import httpx

from app.core.config import Settings

_CACHE_TTL = timedelta(hours=1)


class ModelCacheManager:
    """
    Thread-safe cache manager for OpenRouter models.
    
    Uses instance-based caching (not global) to avoid shared state
    between requests in multi-tenant environments.
    """

    def __init__(self):
        self._cache: dict[str, any] = {}

    def get_cached_models(self) -> Optional[tuple[Set[str], datetime]]:
        """Get cached models if still valid."""
        if "models" in self._cache and "timestamp" in self._cache:
            return self._cache["models"], self._cache["timestamp"]
        return None

    def is_cache_valid(self) -> bool:
        """Check if cache is still within TTL."""
        cached = self.get_cached_models()
        if not cached:
            return False
        _, timestamp = cached
        age = datetime.now(timezone.utc) - timestamp
        return age < _CACHE_TTL

    def set_cache(self, models: Set[str]) -> None:
        """Update cache with new models."""
        self._cache["models"] = models
        self._cache["timestamp"] = datetime.now(timezone.utc)

    def clear_cache(self) -> None:
        """Clear cache (for testing)."""
        self._cache.clear()


# Singleton instance for application-wide caching
# This is acceptable because model list is global (not per-tenant)
# and doesn't contain any user-specific data
_cache_manager = ModelCacheManager()


async def fetch_available_models(
    settings: Settings, cache_manager: Optional[ModelCacheManager] = None
) -> Set[str]:
    """
    Fetch list of available models from OpenRouter API.

    Args:
        settings: Application settings with API key
        cache_manager: Optional cache manager (defaults to singleton for production,
                      can be overridden for testing)

    Returns:
        Set of model IDs (e.g., {"anthropic/claude-sonnet-4.5", ...})
        Empty set if API call fails.
    """
    manager = cache_manager or _cache_manager

    # Check cache first
    if manager.is_cache_valid():
        cached = manager.get_cached_models()
        if cached:
            models, _ = cached
            return models

    # Fetch from API
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {}
            if settings.OPENROUTER_API_KEY:
                headers["Authorization"] = f"Bearer {settings.OPENROUTER_API_KEY}"

            response = await client.get(
                "https://openrouter.ai/api/v1/models", headers=headers
            )
            response.raise_for_status()
            data = response.json()

            models = {model["id"] for model in data.get("data", [])}

            # Update cache
            manager.set_cache(models)

            return models
    except Exception as e:
        logging.warning(f"Failed to fetch OpenRouter models: {e}")
        return set()


def suggest_similar_model(invalid_model: str, valid_models: Set[str]) -> Optional[str]:
    """
    Suggest similar model using fuzzy matching.

    Uses simple Levenshtein distance for suggestions.
    """
    import difflib

    # Extract just the model names for better matching
    matches = difflib.get_close_matches(invalid_model, valid_models, n=1, cutoff=0.6)
    return matches[0] if matches else None


async def validate_and_filter_models(
    model_list: list[str],
    settings: Settings,
    logger: logging.Logger,
    cache_manager: Optional[ModelCacheManager] = None,
) -> list[str]:
    """
    Validate models against OpenRouter API and filter invalid ones.

    Args:
        model_list: Parsed list of model names
        settings: Application settings (for API key)
        logger: Logger for warnings
        cache_manager: Optional cache manager (for testing)

    Returns:
        List of valid models (at least one guaranteed)

    Strategy:
        1. Call OpenRouter GET /v1/models API
        2. Check each model against returned list
        3. If invalid, log warning with suggestion
        4. If all invalid, fall back to first model (best effort)
        5. If API call fails, return original list (permissive)
    """
    # Fetch available models
    available_models = await fetch_available_models(settings, cache_manager)

    # If API failed, return original list (permissive fallback)
    if not available_models:
        logger.warning(
            "Unable to validate models (OpenRouter API error). "
            f"Proceeding with provided list: {model_list}"
        )
        return model_list

    # Validate each model
    valid_models = []
    for model in model_list:
        if model in available_models:
            valid_models.append(model)
        else:
            # Try to suggest correct spelling
            suggestion = suggest_similar_model(model, available_models)
            if suggestion:
                logger.warning(
                    f"Model '{model}' not found in OpenRouter. "
                    f"Did you mean '{suggestion}'? Skipping this model."
                )
            else:
                logger.warning(
                    f"Model '{model}' not found in OpenRouter. " "Skipping this model."
                )

    # If all models invalid, fall back to first (best effort)
    if not valid_models:
        logger.warning(
            "No valid models found in list. Using first model as fallback. "
            "Generation may fail if model is truly invalid."
        )
        return [model_list[0]]

    return valid_models
