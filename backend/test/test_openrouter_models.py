"""
Unit tests for OpenRouter model validation.

Tests caching, filtering, and fallback logic.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.openrouter_models import (
    ModelCacheManager,
    fetch_available_models,
    suggest_similar_model,
    validate_and_filter_models,
)


@pytest.fixture
def settings():
    """Mock settings with OpenRouter API key."""
    return Settings(OPENROUTER_API_KEY="test-api-key")


@pytest.fixture
def logger():
    """Mock logger."""
    return MagicMock(spec=logging.Logger)


@pytest.fixture
def cache_manager():
    """Fresh cache manager for each test."""
    return ModelCacheManager()


@pytest.mark.asyncio
async def test_fetch_available_models_success(settings, cache_manager):
    """Successful API call returns model IDs."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"id": "anthropic/claude-sonnet-4.5"},
            {"id": "openai/gpt-4o"},
            {"id": "google/gemini-2.5-pro"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("app.services.openrouter_models.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        models = await fetch_available_models(settings, cache_manager)

        assert len(models) == 3
        assert "anthropic/claude-sonnet-4.5" in models
        assert "openai/gpt-4o" in models
        assert "google/gemini-2.5-pro" in models


@pytest.mark.asyncio
async def test_fetch_available_models_api_failure(settings, cache_manager):
    """API failure returns empty set (permissive)."""
    with patch("app.services.openrouter_models.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("API error")
        )

        models = await fetch_available_models(settings, cache_manager)

        assert models == set()


@pytest.mark.asyncio
async def test_validate_filters_invalid_models(settings, logger, cache_manager):
    """Invalid models filtered, valid ones kept."""
    models = [
        "anthropic/claude-sonnet-4.5",
        "invalid/model",
        "openai/gpt-4o",
    ]

    # Mock fetch_available_models
    with patch(
        "app.services.openrouter_models.fetch_available_models"
    ) as mock_fetch:
        mock_fetch.return_value = {
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
        }

        result = await validate_and_filter_models(models, settings, logger, cache_manager)

        assert result == ["anthropic/claude-sonnet-4.5", "openai/gpt-4o"]
        assert logger.warning.called


@pytest.mark.asyncio
async def test_validate_all_invalid_fallback(settings, logger, cache_manager):
    """All invalid → use first model as fallback."""
    models = ["invalid1/model", "invalid2/model"]

    with patch(
        "app.services.openrouter_models.fetch_available_models"
    ) as mock_fetch:
        mock_fetch.return_value = {"anthropic/claude-sonnet-4.5"}

        result = await validate_and_filter_models(models, settings, logger, cache_manager)

        assert result == ["invalid1/model"]  # Best-effort fallback
        assert logger.warning.called


@pytest.mark.asyncio
async def test_validate_api_failure_returns_original_list(settings, logger, cache_manager):
    """API failure → return original list (permissive fallback)."""
    models = [
        "anthropic/claude-sonnet-4.5",
        "google/gemini-2.5-pro",
    ]

    with patch(
        "app.services.openrouter_models.fetch_available_models"
    ) as mock_fetch:
        mock_fetch.return_value = set()  # API failed

        result = await validate_and_filter_models(models, settings, logger, cache_manager)

        assert result == models  # Original list unchanged
        assert logger.warning.called


def test_suggest_similar_model():
    """Fuzzy matching suggests correct model."""
    valid = {
        "google/gemini-2.5-pro",
        "google/gemini-3.0-pro-preview",
        "anthropic/claude-sonnet-4.5",
    }

    # Typo: gemni instead of gemini
    suggestion = suggest_similar_model("google/gemni-2.5-pro", valid)
    assert suggestion == "google/gemini-2.5-pro"

    # No close match
    suggestion = suggest_similar_model("totally/different-model", valid)
    assert suggestion is None


@pytest.mark.asyncio
async def test_cache_works(settings):
    """Second call within TTL uses cache."""
    cache_manager = ModelCacheManager()
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [{"id": "anthropic/claude-sonnet-4.5"}]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("app.services.openrouter_models.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        # First call - should hit API
        models1 = await fetch_available_models(settings, cache_manager)
        call_count_1 = mock_client.return_value.__aenter__.return_value.get.call_count

        # Second call - should use cache
        models2 = await fetch_available_models(settings, cache_manager)
        call_count_2 = mock_client.return_value.__aenter__.return_value.get.call_count

        assert models1 == models2
        assert call_count_2 == call_count_1  # No additional API call


def test_cache_manager_clear():
    """Cache manager can be cleared."""
    manager = ModelCacheManager()
    manager.set_cache({"model1", "model2"})
    
    assert manager.is_cache_valid()
    
    manager.clear_cache()
    assert not manager.is_cache_valid()


def test_cache_manager_isolation():
    """Each cache manager instance is independent."""
    manager1 = ModelCacheManager()
    manager2 = ModelCacheManager()
    
    manager1.set_cache({"model1"})
    manager2.set_cache({"model2"})
    
    cached1 = manager1.get_cached_models()
    cached2 = manager2.get_cached_models()
    
    assert cached1[0] == {"model1"}
    assert cached2[0] == {"model2"}
