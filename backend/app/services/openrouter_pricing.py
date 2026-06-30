"""
OpenRouter model pricing cache and cost estimation from Claude SDK usage dicts.

When the workspace provider is OpenRouter, ``AgentQueryCostBreakdown.cost_usd`` is the
authoritative paid amount (catalog × usage buckets). SDK ``total_cost_usd`` is kept
only as ``sdk_reported_cost_usd`` for drift debugging.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from app.core.config import Settings
from app.services.openrouter_api import fetch_models

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=1)


class CostSource(str, Enum):
    OPENROUTER_CATALOG = "openrouter_catalog"
    SDK_FALLBACK = "sdk_fallback"
    SDK_DIRECT = "sdk_direct"


@dataclass(frozen=True)
class OpenRouterModelPricing:
    """Per-token USD rates from OpenRouter GET /v1/models ``pricing`` object."""

    model_id: str
    prompt_per_token: float
    completion_per_token: float
    cache_read_per_token: float = 0.0
    cache_write_per_token: float = 0.0
    request_per_call: float = 0.0

    def estimate_usd(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Estimate cost from SDK usage (cache buckets reported separately)."""
        return (
            (max(0, input_tokens) * self.prompt_per_token)
            + (max(0, output_tokens) * self.completion_per_token)
            + (max(0, cache_read_tokens) * self.cache_read_per_token)
            + (max(0, cache_write_tokens) * self.cache_write_per_token)
            + self.request_per_call
        )


@dataclass(frozen=True)
class AgentQueryCostBreakdown:
    """Resolved USD cost for one agent_query (OpenRouter catalog or SDK fallback)."""

    cost_usd: float
    source: CostSource
    sdk_reported_cost_usd: Optional[float]
    model_id: str = ""
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    cache_read_cost_usd: float = 0.0
    cache_write_cost_usd: float = 0.0
    request_cost_usd: float = 0.0
    has_cache_tokens: bool = False


def _parse_price(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def pricing_from_catalog_row(row: dict[str, Any]) -> OpenRouterModelPricing:
    """Build pricing from one OpenRouter /v1/models data[] element."""
    pricing = row.get("pricing") or {}
    return OpenRouterModelPricing(
        model_id=str(row.get("id") or ""),
        prompt_per_token=_parse_price(pricing.get("prompt")),
        completion_per_token=_parse_price(pricing.get("completion")),
        cache_read_per_token=_parse_price(pricing.get("input_cache_read")),
        cache_write_per_token=_parse_price(pricing.get("input_cache_write")),
        request_per_call=_parse_price(pricing.get("request")),
    )


def _usage_token_counts(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
    )


def breakdown_from_openrouter_catalog(
    pricing: OpenRouterModelPricing,
    usage: dict[str, Any],
    *,
    sdk_reported_usd: Optional[float],
) -> AgentQueryCostBreakdown:
    inp, out, cache_read, cache_write = _usage_token_counts(usage)
    input_cost = max(0, inp) * pricing.prompt_per_token
    output_cost = max(0, out) * pricing.completion_per_token
    cache_read_cost = max(0, cache_read) * pricing.cache_read_per_token
    cache_write_cost = max(0, cache_write) * pricing.cache_write_per_token
    return AgentQueryCostBreakdown(
        cost_usd=input_cost + output_cost + cache_read_cost + cache_write_cost + pricing.request_per_call,
        source=CostSource.OPENROUTER_CATALOG,
        sdk_reported_cost_usd=sdk_reported_usd,
        model_id=pricing.model_id,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        cache_read_cost_usd=cache_read_cost,
        cache_write_cost_usd=cache_write_cost,
        request_cost_usd=pricing.request_per_call,
        has_cache_tokens=(cache_read + cache_write) > 0,
    )


class OpenRouterPricingCache:
    """In-memory catalog keyed by model id; refreshed on TTL or startup."""

    def __init__(self, ttl: timedelta = _CACHE_TTL) -> None:
        self._ttl = ttl
        self._by_id: dict[str, OpenRouterModelPricing] = {}
        self._expires_at: Optional[datetime] = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    def is_valid(self) -> bool:
        return self._expires_at is not None and datetime.now(timezone.utc) < self._expires_at

    def get(self, model_id: str) -> Optional[OpenRouterModelPricing]:
        """Return pricing for model_id, or None if cache is stale/empty.

        Limitation: /v1/models returns one price entry per model using the
        default OpenRouter sub-provider. If a specific provider is forced via
        ``provider.order`` routing, the actual charged price may differ. For
        exact per-request cost, use GET /v1/generation?id={openrouter_id}.
        """
        if not self.is_valid():
            return None
        return self._by_id.get(model_id)

    def replace_all(self, rows: dict[str, OpenRouterModelPricing]) -> None:
        self._by_id = rows
        self._expires_at = datetime.now(timezone.utc) + self._ttl

    async def refresh(self, settings: Settings, *, force: bool = False) -> int:
        """Fetch catalog; returns number of models loaded. Concurrent calls share one HTTP request.

        ``force=True`` bypasses the validity short-circuit so the scheduled
        background refresher can renew the catalog *before* the TTL lapses
        (the lazy short-circuit would otherwise skip a still-valid cache and
        let it expire, leaving a stale window until the next tick).
        """
        async with self._refresh_lock:
            if not force and self.is_valid():
                return len(self._by_id)
            data = await fetch_models(settings)
            loaded: dict[str, OpenRouterModelPricing] = {}
            for row in data:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                loaded[str(row["id"])] = pricing_from_catalog_row(row)
            self.replace_all(loaded)
            logger.info("OpenRouter pricing cache refreshed: %s models", len(loaded))
            return len(loaded)

_pricing_cache = OpenRouterPricingCache()


def get_openrouter_pricing_cache() -> OpenRouterPricingCache:
    return _pricing_cache


async def ensure_openrouter_pricing_cache(settings: Settings, *, force: bool = False) -> None:
    """Refresh the catalog.

    ``force=False`` (lazy/startup): refresh only if missing or expired.
    ``force=True`` (scheduled refresher): always refetch so the cache is
    renewed ahead of its TTL and never lapses between ticks. Either way,
    failures are swallowed — callers fall back to SDK pricing gracefully.
    """
    cache = get_openrouter_pricing_cache()
    if not force and cache.is_valid():
        return
    try:
        await cache.refresh(settings, force=force)
    except Exception as exc:
        logger.warning("OpenRouter pricing refresh failed: %s", exc)


def resolve_agent_query_cost_usd(
    *,
    use_catalog_pricing: bool,
    model: str,
    usage: dict[str, Any],
    sdk_reported_usd: Optional[float],
) -> AgentQueryCostBreakdown:
    """Authoritative cost for one agent_query — pure cache lookup, no I/O.

    OpenRouter workspaces: catalog estimate (matches dashboard ``usage``).
    Direct Anthropic / cache miss: SDK ``total_cost_usd``.

    Cache is maintained by the background job started in main.py; if the cache
    is stale or empty at call time, falls back to SDK_FALLBACK gracefully.
    """
    sdk_val = float(sdk_reported_usd) if sdk_reported_usd is not None else None

    if not use_catalog_pricing:
        return AgentQueryCostBreakdown(
            cost_usd=sdk_val or 0.0,
            source=CostSource.SDK_DIRECT,
            sdk_reported_cost_usd=sdk_val,
            model_id=model,
        )

    pricing = get_openrouter_pricing_cache().get(model)
    if pricing is not None:
        return breakdown_from_openrouter_catalog(pricing, usage, sdk_reported_usd=sdk_val)

    logger.warning(
        "No OpenRouter pricing for model %r (cache miss or stale); using SDK total_cost_usd=%s",
        model,
        sdk_val,
    )
    return AgentQueryCostBreakdown(
        cost_usd=sdk_val or 0.0,
        source=CostSource.SDK_FALLBACK,
        sdk_reported_cost_usd=sdk_val,
        model_id=model,
    )


