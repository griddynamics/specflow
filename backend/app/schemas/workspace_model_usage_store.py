"""Firestore layout for per-workspace LLM usage + accumulated USD cost.

Per-workspace documents live in a subcollection of ``generation_sessions``:

``generation_sessions/{generation_id}/workspace_model_usage/{workspace_id}``

Each document holds the same token buckets as top-level ``model_usage`` (flat shape)
plus ``total_usd_cost`` — cumulative provider-reported spend for that workspace.

The parent generation document also stores ``total_usd_cost`` (sum across
workspaces) for O(1) reads in reports and notifications.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.schemas.model_token_usage import FLAT_AGGREGATE_MODEL_NAME, ModelTokenUsage

WORKSPACE_MODEL_USAGE_SUBCOLLECTION = "workspace_model_usage"

# Cumulative LLM API spend (USD) on the generation session document
SESSION_TOTAL_USD_COST_FIELD = "total_usd_cost"

# Per-workspace subdocument field
WORKSPACE_TOTAL_USD_COST_FIELD = "total_usd_cost"


def token_fields_from_workspace_subdoc(row: Dict[str, Any]) -> Dict[str, Any]:
    """Subset of a workspace subdoc suitable for :class:`ModelTokenUsage.from_dict`."""
    return {
        "model_name": FLAT_AGGREGATE_MODEL_NAME,
        "num_turns": int(row.get("num_turns") or 0),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
        "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
    }


def merge_workspace_model_usage_subdoc(
    existing: Optional[Dict[str, Any]],
    delta: ModelTokenUsage,
    cost_usd: float,
) -> Dict[str, Any]:
    """Return the full subdocument payload after applying ``delta`` and ``cost_usd``."""
    prev = ModelTokenUsage.from_dict(token_fields_from_workspace_subdoc(existing or {}))
    mtu = prev + delta if not delta.is_empty() else prev
    prev_cost = float((existing or {}).get(WORKSPACE_TOTAL_USD_COST_FIELD) or 0.0)
    out = {**mtu.to_flat_dict(), WORKSPACE_TOTAL_USD_COST_FIELD: prev_cost + float(cost_usd or 0.0)}
    return out
