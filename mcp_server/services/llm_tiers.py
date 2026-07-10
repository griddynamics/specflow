"""
LLM tier helpers for MCP services.

Single source of truth for the tier key list and env-var propagation used
by all MCP orchestrators.  Aligned with LLMTier.value in
backend/app/schemas/llm_tier.py — same name everywhere.
"""

import os
from typing import Dict

# All three tier keys — used as env var name, form param name, and override dict key.
LLM_TIER_KEYS = ["LLM_HIGH", "LLM_MEDIUM", "LLM_LOW"]

MCP_SERVERS_ENABLED_ENV = "MCP_SERVERS_ENABLED"
MCP_SERVERS_ENABLED_DEFAULT = "playwright"


def apply_mcp_servers_enabled_form(form_data: Dict[str, str]) -> None:
    """Add MCP_SERVERS_ENABLED from MCP process env to backend form data when set."""
    value = os.environ.get(MCP_SERVERS_ENABLED_ENV)
    if value and value.strip():
        form_data[MCP_SERVERS_ENABLED_ENV] = value.strip()


def llm_tier_overrides_from_env() -> Dict[str, str]:
    """Return the LLM tier overrides present in the process environment.

    Reads LLM_HIGH, LLM_MEDIUM, LLM_LOW (set via mcp.json) and returns only the
    ones actually set, so absent tiers let the backend fall back to its own
    defaults. Pure — no mutation — so callers can pass the result directly.
    """
    return {key: value for key in LLM_TIER_KEYS if (value := os.environ.get(key))}


def apply_llm_tier_overrides(form_data: Dict[str, str]) -> None:
    """In-place variant of :func:`llm_tier_overrides_from_env` for callers that
    accumulate several groups of fields into one ``form_data`` dict."""
    form_data.update(llm_tier_overrides_from_env())
