"""Unit tests for OpenRouter pricing estimation."""

import pytest

import app.services.openrouter_pricing as pricing_module
from app.core.config import OPENROUTER_PRICING_REFRESH_INTERVAL_SECONDS
from app.services.openrouter_pricing import (
    _CACHE_TTL,
    OpenRouterModelPricing,
    OpenRouterPricingCache,
    pricing_from_catalog_row,
)


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    pricing_module.get_openrouter_pricing_cache().replace_all({})
    yield
    pricing_module.get_openrouter_pricing_cache().replace_all({})


def _one_model_catalog(_settings):
    return [{
        "id": "anthropic/claude-haiku-4.5",
        "pricing": {"prompt": "0.000001", "completion": "0.000005"},
    }]


def test_pricing_from_catalog_row_parses_strings():
    row = {
        "id": "openai/gpt-oss-120b",
        "pricing": {
            "prompt": "0.039",
            "completion": "0.19",
            "input_cache_read": "0.01",
            "input_cache_write": "0.05",
            "request": "0",
        },
    }
    p = pricing_from_catalog_row(row)
    assert p.model_id == "openai/gpt-oss-120b"
    assert p.prompt_per_token == 0.039


def test_estimate_matches_posthog_style_gpt_oss_example():
    """Regression for user-reported gpt-oss-120b event (~$0.0026 PostHog total)."""
    pricing = OpenRouterModelPricing(
        model_id="openai/gpt-oss-120b",
        prompt_per_token=0.039 / 1_000_000,
        completion_per_token=0.18 / 1_000_000,
    )
    # ~0.002565 + ~0.000051 — ignore cache read when rate unset (0)
    cost = pricing.estimate_usd(input_tokens=65775, output_tokens=284, cache_read_tokens=672)
    assert 0.0024 < cost < 0.0028


def test_haiku_dump_script_usage_matches_catalog_not_sdk():
    """Regression from dump_claude_sdk_messages haiku probe (Jun 2026)."""
    catalog = pricing_from_catalog_row(
        {
            "id": "anthropic/claude-haiku-4.5",
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000005",
                "input_cache_read": "0.0000001",
                "input_cache_write": "0.00000125",
            },
        }
    )
    est = catalog.estimate_usd(
        input_tokens=10, output_tokens=46, cache_write_tokens=18598
    )
    assert abs(est - 0.0234875) < 1e-6
    # SDK priced this run as Sonnet (~3× catalog)
    sdk_sonnet = (10 * 3 + 18598 * 3.75 + 46 * 15) / 1_000_000
    assert abs(sdk_sonnet - 0.0704625) < 1e-6
    assert est < sdk_sonnet


def test_haiku_vs_sonnet_gap_on_same_usage():
    """SDK often prices like Sonnet; catalog Haiku should be much lower."""
    haiku = OpenRouterModelPricing(
        model_id="anthropic/claude-haiku-4.5",
        prompt_per_token=0.80 / 1_000_000,
        completion_per_token=4.0 / 1_000_000,
        cache_read_per_token=0.08 / 1_000_000,
        cache_write_per_token=1.0 / 1_000_000,
    )
    sonnet = OpenRouterModelPricing(
        model_id="anthropic/claude-sonnet-4.5",
        prompt_per_token=3.0 / 1_000_000,
        completion_per_token=15.0 / 1_000_000,
        cache_read_per_token=0.30 / 1_000_000,
        cache_write_per_token=3.75 / 1_000_000,
    )
    haiku_cost = haiku.estimate_usd(
        input_tokens=32, output_tokens=3161, cache_write_tokens=31038, cache_read_tokens=106001
    )
    sonnet_cost = sonnet.estimate_usd(
        input_tokens=32, output_tokens=3161, cache_write_tokens=31038, cache_read_tokens=106001
    )
    assert sonnet_cost > haiku_cost * 2
    assert abs(sonnet_cost - 0.1957038) < 0.0001


# ---------------------------------------------------------------------------
# Scheduled refresh must renew the catalog *before* the TTL lapses.
# Regression: with the refresh interval == TTL and a validity short-circuit,
# the scheduled job skipped the refetch and the cache lapsed each cycle,
# silently falling back to inflated SDK pricing for OpenRouter queries.
# ---------------------------------------------------------------------------


def test_refresh_interval_is_strictly_below_cache_ttl():
    assert OPENROUTER_PRICING_REFRESH_INTERVAL_SECONDS < _CACHE_TTL.total_seconds()


@pytest.mark.asyncio
async def test_lazy_refresh_skips_when_cache_valid(monkeypatch):
    calls = {"n": 0}

    async def counting_fetch(settings):
        calls["n"] += 1
        return _one_model_catalog(settings)

    monkeypatch.setattr(pricing_module, "fetch_models", counting_fetch)
    cache = OpenRouterPricingCache()

    await cache.refresh(None)            # populate -> now valid
    await cache.refresh(None)            # lazy -> short-circuits on validity

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_force_refresh_refetches_even_when_cache_valid(monkeypatch):
    calls = {"n": 0}

    async def counting_fetch(settings):
        calls["n"] += 1
        return _one_model_catalog(settings)

    monkeypatch.setattr(pricing_module, "fetch_models", counting_fetch)
    cache = OpenRouterPricingCache()

    await cache.refresh(None)               # populate -> now valid
    await cache.refresh(None, force=True)   # proactive renewal -> must refetch

    assert calls["n"] == 2
    assert cache.is_valid()


@pytest.mark.asyncio
async def test_ensure_cache_force_refetches(monkeypatch):
    calls = {"n": 0}

    async def counting_fetch(settings):
        calls["n"] += 1
        return _one_model_catalog(settings)

    monkeypatch.setattr(pricing_module, "fetch_models", counting_fetch)

    # force=True first so the fetch happens regardless of the singleton's
    # prior validity (other tests may have populated it).
    await pricing_module.ensure_openrouter_pricing_cache(None, force=True)  # fetch -> now valid
    await pricing_module.ensure_openrouter_pricing_cache(None)              # valid + lazy -> skip
    await pricing_module.ensure_openrouter_pricing_cache(None, force=True)  # valid + force -> refetch

    assert calls["n"] == 2
    pricing_module.get_openrouter_pricing_cache().replace_all({})
