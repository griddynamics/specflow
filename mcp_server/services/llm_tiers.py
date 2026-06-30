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


def apply_llm_tier_overrides(form_data: Dict[str, str]) -> None:
    """Inject LLM tier overrides from the MCP environment into form_data (in-place).

    Reads LLM_HIGH, LLM_MEDIUM, LLM_LOW from the process environment (set via
    mcp.json) and adds any that are present to the form_data dict that will be
    sent to the backend API.  Keys not set in the environment are left out so
    the backend falls back to its own defaults.
    """
    for key in LLM_TIER_KEYS:
        value = os.environ.get(key)
        if value:
            form_data[key] = value
