"""Unit tests for the provider-agnostic model catalog fetchers.

Mirrors the mocked-httpx style of test_openrouter_models.py. Covers the new
Anthropic live fetch (headers, pagination, permissive failure, caching), the
OpenRouter delegation, and the provider registry routing.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.core.enums import LLMProvider
from app.services.model_catalog import (
    AnthropicCatalogFetcher,
    OpenRouterCatalogFetcher,
    clear_catalog_caches,
    fetch_catalog_for_provider,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Each test starts with empty catalog caches."""
    clear_catalog_caches()
    yield
    clear_catalog_caches()


@pytest.fixture
def logger():
    return MagicMock(spec=logging.Logger)


def _anthropic_settings():
    return Settings(ANTHROPIC_API_KEY="sk-ant-test", DEFAULT_PROVIDER=LLMProvider.ANTHROPIC)


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_anthropic_fetch_success_parses_ids_and_sends_headers():
    settings = _anthropic_settings()
    fetcher = AnthropicCatalogFetcher()
    resp = _make_response(
        {
            "data": [
                {"id": "claude-opus-4-6-20260101"},
                {"id": "claude-sonnet-4-6-20260101"},
            ],
            "has_more": False,
        }
    )
    mock_get = AsyncMock(return_value=resp)
    with patch("app.services.model_catalog.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = mock_get
        models = await fetcher.fetch(settings)

    assert models == {"claude-opus-4-6-20260101", "claude-sonnet-4-6-20260101"}
    # URL + auth headers
    _, kwargs = mock_get.call_args
    headers = kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in headers


@pytest.mark.asyncio
async def test_anthropic_fetch_follows_pagination():
    settings = _anthropic_settings()
    fetcher = AnthropicCatalogFetcher()
    page1 = _make_response(
        {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"}
    )
    page2 = _make_response({"data": [{"id": "claude-b"}], "has_more": False})
    mock_get = AsyncMock(side_effect=[page1, page2])
    with patch("app.services.model_catalog.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = mock_get
        models = await fetcher.fetch(settings)

    assert models == {"claude-a", "claude-b"}
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_anthropic_fetch_api_failure_returns_empty_set():
    settings = _anthropic_settings()
    fetcher = AnthropicCatalogFetcher()
    with patch("app.services.model_catalog.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=Exception("boom")
        )
        models = await fetcher.fetch(settings)

    assert models == set()


@pytest.mark.asyncio
async def test_anthropic_fetch_uses_cache_on_second_call():
    settings = _anthropic_settings()
    fetcher = AnthropicCatalogFetcher()
    resp = _make_response({"data": [{"id": "claude-a"}], "has_more": False})
    mock_get = AsyncMock(return_value=resp)
    with patch("app.services.model_catalog.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = mock_get
        first = await fetcher.fetch(settings)
        second = await fetcher.fetch(settings)

    assert first == second == {"claude-a"}
    assert mock_get.call_count == 1  # second call served from cache


@pytest.mark.asyncio
async def test_anthropic_fetch_no_key_returns_empty():
    """No API key → cannot verify → permissive empty set (no HTTP call)."""
    settings = Settings(ANTHROPIC_API_KEY=None, DEFAULT_PROVIDER=LLMProvider.ANTHROPIC)
    fetcher = AnthropicCatalogFetcher()
    with patch("app.services.model_catalog.httpx.AsyncClient") as mock_client:
        models = await fetcher.fetch(settings)
        mock_client.assert_not_called()
    assert models == set()


@pytest.mark.asyncio
async def test_openrouter_fetcher_delegates_to_openrouter_models():
    settings = Settings(OPENROUTER_API_KEY="test")
    fetcher = OpenRouterCatalogFetcher()
    with patch(
        "app.services.model_catalog.fetch_available_models",
        new=AsyncMock(return_value={"anthropic/claude-opus-4.6"}),
    ) as delegate:
        models = await fetcher.fetch(settings)

    delegate.assert_awaited_once()
    assert models == {"anthropic/claude-opus-4.6"}


@pytest.mark.asyncio
async def test_fetch_catalog_for_provider_routes_by_enum():
    settings = Settings(OPENROUTER_API_KEY="test")
    with patch(
        "app.services.model_catalog.fetch_available_models",
        new=AsyncMock(return_value={"openai/gpt-5.4"}),
    ):
        models = await fetch_catalog_for_provider(LLMProvider.OPENROUTER, settings)
    assert models == {"openai/gpt-5.4"}
