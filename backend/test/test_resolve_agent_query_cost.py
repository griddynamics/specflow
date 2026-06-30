"""Tests for resolve_agent_query_cost_usd."""

import pytest

from app.services.openrouter_pricing import (
    AgentQueryCostBreakdown,
    CostSource,
    OpenRouterModelPricing,
    breakdown_from_openrouter_catalog,
    get_openrouter_pricing_cache,
    resolve_agent_query_cost_usd,
)


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Isolate the module-level singleton between tests."""
    get_openrouter_pricing_cache().replace_all({})
    yield
    get_openrouter_pricing_cache().replace_all({})


# ---------------------------------------------------------------------------
# CostSource enum contract
# ---------------------------------------------------------------------------


def test_cost_source_values_are_stable_strings():
    """CostSource members must serialize to the exact strings used in telemetry/Langfuse."""
    assert CostSource.OPENROUTER_CATALOG == "openrouter_catalog"
    assert CostSource.SDK_FALLBACK == "sdk_fallback"
    assert CostSource.SDK_DIRECT == "sdk_direct"


def test_cost_source_is_string_subtype():
    assert isinstance(CostSource.OPENROUTER_CATALOG, str)


def test_agent_query_cost_breakdown_source_is_cost_source():
    b = AgentQueryCostBreakdown(
        cost_usd=0.01,
        source=CostSource.SDK_DIRECT,
        sdk_reported_cost_usd=0.01,
    )
    assert isinstance(b.source, CostSource)


# ---------------------------------------------------------------------------
# resolve_agent_query_cost_usd
# ---------------------------------------------------------------------------


def test_resolve_openrouter_catalog():
    get_openrouter_pricing_cache().replace_all({
        "anthropic/claude-haiku-4.5": OpenRouterModelPricing(
            model_id="anthropic/claude-haiku-4.5",
            prompt_per_token=0.000001,
            completion_per_token=0.000005,
            cache_read_per_token=0.0000001,
            cache_write_per_token=0.00000125,
        ),
    })
    # Turn 2 cache-probe (dashboard usage 0.00414685)
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 1069,
        "cache_read_input_tokens": 24456,
        "output_tokens": 71,
    }
    b = resolve_agent_query_cost_usd(
        use_catalog_pricing=True,
        model="anthropic/claude-haiku-4.5",
        usage=usage,
        sdk_reported_usd=0.012,
    )
    assert b.source is CostSource.OPENROUTER_CATALOG
    assert abs(b.cost_usd - 0.00414685) < 1e-6
    assert b.sdk_reported_cost_usd == 0.012
    assert b.has_cache_tokens is True


def test_resolve_sdk_direct():
    b = resolve_agent_query_cost_usd(
        use_catalog_pricing=False,
        model="claude-sonnet-4-5",
        usage={},
        sdk_reported_usd=0.5,
    )
    assert b.source is CostSource.SDK_DIRECT
    assert b.cost_usd == 0.5


def test_resolve_sdk_fallback_when_model_not_in_catalog():
    """When the catalog has no entry for the model, fall back to SDK value."""
    # autouse fixture already cleared the cache; no entry for this model
    b = resolve_agent_query_cost_usd(
        use_catalog_pricing=True,
        model="unknown/exotic-model",
        usage={"input_tokens": 100, "output_tokens": 50},
        sdk_reported_usd=0.003,
    )
    assert b.source is CostSource.SDK_FALLBACK
    assert b.cost_usd == 0.003


def test_breakdown_component_costs_sum_to_total():
    pricing = OpenRouterModelPricing(
        model_id="test/model",
        prompt_per_token=0.000001,
        completion_per_token=0.000005,
        cache_write_per_token=0.00000125,
    )
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 18598,
        "cache_read_input_tokens": 0,
        "output_tokens": 46,
    }
    b = breakdown_from_openrouter_catalog(pricing, usage, sdk_reported_usd=0.07)
    assert b.source is CostSource.OPENROUTER_CATALOG
    assert abs(
        b.cost_usd
        - (
            b.input_cost_usd
            + b.output_cost_usd
            + b.cache_read_cost_usd
            + b.cache_write_cost_usd
            + b.request_cost_usd
        )
    ) < 1e-9


def test_sdk_direct_breakdown_fields():
    """SDK_DIRECT breakdown carries correct source and cost."""
    b = AgentQueryCostBreakdown(
        cost_usd=0.042,
        source=CostSource.SDK_DIRECT,
        sdk_reported_cost_usd=0.042,
        model_id="anthropic/claude-sonnet-4.6",
    )
    assert b.cost_usd == 0.042
    assert b.source is CostSource.SDK_DIRECT
    assert b.sdk_reported_cost_usd == 0.042
