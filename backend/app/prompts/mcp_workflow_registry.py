"""
Central matrix: optional agent MCPs (playwright, figma) per harness workflow.

SSOT for "which user-toggleable MCP ids can attach where" and where prune keywords are read from.
Runtime enablement: resolve_enabled_mcps* → intersection with these allowlists → stdio builders in mcp_config.
Runtime dispatch: McpSelector (app.core.mcp_selection) is the single call-site for resolving per-step
server configs; it applies these allowlists, logs the selection, and records it in TelemetryContext.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, FrozenSet, Mapping, Tuple

from app.core.config import MCP_FIGMA, MCP_PLAYWRIGHT, SUPPORTED_MCPS, Settings

# --- User-requestable agent MCP ids (comma-separated MCP_SERVERS_ENABLED / generation parameters) ---
# Canonical ids; see config.SUPPORTED_MCPS.

# --- Per-workflow: which of SUPPORTED_MCPS may be wired for Claude Code in that step ---
# User-selectable MCPs (Playwright, Figma) are resolved at runtime from generation parameters.

SPEC_INDEX_AND_COMPLETENESS_OPTIONAL_MCPS: Final[FrozenSet[str]] = frozenset({MCP_FIGMA})
PLANNING_OPTIONAL_AGENT_MCPS: Final[FrozenSet[str]] = frozenset({MCP_FIGMA})
CODE_GENERATION_AND_DEPLOY_OPTIONAL_MCPS: Final[FrozenSet[str]] = SUPPORTED_MCPS

# MCP prune (optional LLM after keyword grep): no MCP servers attached to that agent session.
MCP_PRUNE_AGENT_ATTACHED_OPTIONAL_MCPS: Final[FrozenSet[str]] = frozenset()

# Keyword scan before / instead of LLM prune: same files as mcp_prune.scan_mcp_keyword_evidence
MCP_PRUNE_SCANS_SPEC_INDEX_AND_SPEC_TREE: Final[bool] = True

# Settings attributes holding comma-separated case-insensitive substrings (grep), per MCP id
PRUNE_KEYWORD_SETTINGS_FIELD_BY_MCP_ID: Final[dict[str, str]] = {
    MCP_FIGMA: "MCP_PRUNE_KEYWORDS_FIGMA",
    MCP_PLAYWRIGHT: "MCP_PRUNE_KEYWORDS_PLAYWRIGHT",
}

# Other prune-related settings (env / BaseSettings)
MCP_PRUNE_NUMERIC_CAPS_FIELDS: Final[Tuple[str, ...]] = (
    "MCP_PRUNE_GREP_MAX_LINES_TOTAL",
    "MCP_PRUNE_GREP_MAX_LINES_PER_MCP",
    "MCP_PRUNE_GREP_MAX_CHARS",
)
MCP_PRUNE_BOOL_FIELDS: Final[Tuple[str, ...]] = (
    "MCP_AUTO_PRUNE_ENABLED",
    "MCP_PRUNE_USE_LLM",
)

# Resolution precedence for "enabled" set; mirrors resolve_enabled_mcps_detailed(..., prefer_stored=False)
ENABLED_MCPS_RESOLUTION_ORDER: Final[Tuple[str, ...]] = (
    "form_value (API)",
    "generation_session_parameters.mcp_servers_enabled",
    "settings.MCP_SERVERS_ENABLED",
)
# Planning + POST /generation-sessions/run: prefer_stored=True — non-empty parameters.mcp_servers_enabled first
ENABLED_MCPS_RESOLUTION_ORDER_PLAN_AND_RUN: Final[Tuple[str, ...]] = (
    "generation_session_parameters.mcp_servers_enabled",
    "form_value (API)",
    "settings.MCP_SERVERS_ENABLED",
)

# Per-phase codegen/deploy: plan field applicable_agent_mcps ∩ generation enabled ∩ SUPPORTED_MCPS;
# omitted in plan → use full generation enabled set for that phase.

# --- MCP prune LLM agent: shared intro + per-id rules (see format_mcp_prune_llm_rules_section) ---
MCP_PRUNE_LLM_SHARED_LINES_BEFORE_MCP_RULES: Final[Tuple[str, ...]] = (
    "## MCP enablement rules (read carefully)",
    "",
    "You are choosing which **agent MCP servers** to attach to later Claude Code sessions.",
    "This is NOT about Playwright the npm test library used in generated repos — only about optional MCP tooling.",
    "",
)

_MCP_PRUNE_LLM_RULES_RAW: dict[str, str] = {
    MCP_FIGMA: (
        f"- Include **{MCP_FIGMA}** in `enabled` only if specifications or the index mention **Figma**, "
        "**Figma links**, or **figma.com** (or equivalent design-handoff references)."
    ),
    MCP_PLAYWRIGHT: (
        f"- Include **{MCP_PLAYWRIGHT}** only if specs describe a **user-facing web UI**, **frontend**, "
        "**browser**, **web app**, or similar surface where an **agent** might use browser-oriented MCP tools "
        "during implementation.\n"
        "- Do NOT enable it merely because tests or CI might use Playwright as a library."
    ),
}
MCP_PRUNE_LLM_RULES_BY_MCP_ID: Mapping[str, str] = MappingProxyType(_MCP_PRUNE_LLM_RULES_RAW)

MCP_PRUNE_LLM_OUTPUT_FORMAT_INSTRUCTION: Final[str] = (
    "Output **only** a single JSON object (no markdown outside it). "
    'Schema: {"enabled": ["..."], "reasons": {"mcp_id": "one sentence evidence"}}. '
    "Include one `reasons` entry per id in `enabled` (one sentence each). "
    "Every id in `enabled` must be from the candidate list below."
)


def format_mcp_prune_llm_rules_section(candidate: FrozenSet[str]) -> str:
    """Markdown block for the optional LLM MCP-prune agent (per-id rules from MCP_PRUNE_LLM_RULES_BY_MCP_ID)."""
    lines = list(MCP_PRUNE_LLM_SHARED_LINES_BEFORE_MCP_RULES)
    for mcp_id in sorted(candidate):
        rule_text = MCP_PRUNE_LLM_RULES_BY_MCP_ID.get(mcp_id)
        if not rule_text:
            continue
        lines.append(f"### {mcp_id}")
        for segment in rule_text.split("\n"):
            lines.append(segment)
        lines.append("")
    lines.append(MCP_PRUNE_LLM_OUTPUT_FORMAT_INSTRUCTION)
    return "\n".join(lines)


def prune_keywords_raw_from_settings(settings: Settings, mcp_id: str) -> str:
    """Return the raw comma-separated keyword string for grep (empty if unknown id)."""
    attr = PRUNE_KEYWORD_SETTINGS_FIELD_BY_MCP_ID.get(mcp_id)
    if not attr:
        return ""
    return str(getattr(settings, attr, "") or "")
