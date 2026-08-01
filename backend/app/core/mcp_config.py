"""MCP server configuration builders for Claude Code SDK agents.

Per-workflow optional MCP allowlists: see `app.prompts.mcp_workflow_registry`.
"""

from dataclasses import dataclass
import logging
import shlex
from typing import FrozenSet, List, Literal, Optional

from app.core.config import MCP_FIGMA, MCP_FIGMA_SERVER_KEY, MCP_PLAYWRIGHT, SUPPORTED_MCPS, Settings
from app.core.tool_usage import get_figma_mcp_tools, get_playwright_mcp_tools
from app.prompts.mcp_workflow_registry import (
    CODE_GENERATION_AND_DEPLOY_OPTIONAL_MCPS,
    PLANNING_OPTIONAL_AGENT_MCPS,
    SPEC_INDEX_AND_COMPLETENESS_OPTIONAL_MCPS,
)

logger = logging.getLogger(__name__)

McpConfigurationSource = Literal[
    "form",
    "generation_session_parameters",
    "backend_settings",
    "workspace_sync_params",
]


@dataclass(frozen=True)
class EnabledMcpsResolution:
    """Resolved MCP enablement plus where the raw string came from (for analytics)."""

    enabled: FrozenSet[str]
    source: McpConfigurationSource
    raw_request_string: str


def parse_mcp_servers_enabled_string(raw: str) -> FrozenSet[str]:
    """Split comma-separated MCP ids, normalize, keep only SUPPORTED_MCPS entries."""
    if not raw or not raw.strip():
        return frozenset()
    names = {p.strip().lower() for p in raw.split(",") if p.strip()}
    supported = frozenset(n for n in names if n in SUPPORTED_MCPS)
    unknown = names - supported
    if unknown:
        logger.warning("Ignoring unsupported MCP_SERVERS_ENABLED entries: %s", sorted(unknown))
    return supported


def resolve_enabled_mcps_detailed(
    *,
    form_value: Optional[str],
    generation_session_parameters: Optional[dict],
    settings: Settings,
    prefer_stored: bool = False,
) -> EnabledMcpsResolution:
    """Resolve enabled MCP set and record which input won.

    Default (``prefer_stored=False``): **form → stored parameters → backend default**
    (used e.g. for specification analyze so the client env can define the prune candidate set).

    When ``prefer_stored=True`` (planning / generation on an existing generation): if
    ``generation_session_parameters["mcp_servers_enabled"]`` is non-empty, it wins **before** the form.
    That preserves spec-driven prune and workspace-sync SSOT even when the MCP client always
    sends ``MCP_SERVERS_ENABLED``. If stored is absent or blank, resolution is **form → settings**
    (same as skipping the stored branch).
    """
    if prefer_stored and generation_session_parameters:
        stored_first = generation_session_parameters.get("mcp_servers_enabled")
        if stored_first is not None and str(stored_first).strip():
            raw = str(stored_first).strip()
            return EnabledMcpsResolution(
                enabled=parse_mcp_servers_enabled_string(raw),
                source="generation_session_parameters",
                raw_request_string=raw,
            )
    if form_value is not None and str(form_value).strip():
        raw = str(form_value).strip()
        return EnabledMcpsResolution(
            enabled=parse_mcp_servers_enabled_string(raw),
            source="form",
            raw_request_string=raw,
        )
    if generation_session_parameters:
        stored = generation_session_parameters.get("mcp_servers_enabled")
        if stored is not None and str(stored).strip():
            raw = str(stored).strip()
            return EnabledMcpsResolution(
                enabled=parse_mcp_servers_enabled_string(raw),
                source="generation_session_parameters",
                raw_request_string=raw,
            )
    raw = (settings.MCP_SERVERS_ENABLED or "").strip()
    return EnabledMcpsResolution(
        enabled=parse_mcp_servers_enabled_string(settings.MCP_SERVERS_ENABLED),
        source="backend_settings",
        raw_request_string=raw,
    )


def resolve_enabled_mcps(
    *,
    form_value: Optional[str],
    generation_session_parameters: Optional[dict],
    settings: Settings,
    prefer_stored: bool = False,
) -> FrozenSet[str]:
    """Resolve enabled MCP set. See ``resolve_enabled_mcps_detailed`` for precedence."""
    return resolve_enabled_mcps_detailed(
        form_value=form_value,
        generation_session_parameters=generation_session_parameters,
        settings=settings,
        prefer_stored=prefer_stored,
    ).enabled


def mcp_resolution_from_workspace_sync_parameters(
    parameters: dict,
    settings: Settings,
) -> EnabledMcpsResolution:
    """Resolution for POST /workspace/sync when creating a new generation (sync JSON params)."""
    raw = parameters.get("mcp_servers_enabled")
    if raw is not None and str(raw).strip():
        s = str(raw).strip()
        return EnabledMcpsResolution(
            enabled=parse_mcp_servers_enabled_string(s),
            source="workspace_sync_params",
            raw_request_string=s,
        )
    raw_default = (settings.MCP_SERVERS_ENABLED or "").strip()
    return EnabledMcpsResolution(
        enabled=parse_mcp_servers_enabled_string(settings.MCP_SERVERS_ENABLED),
        source="backend_settings",
        raw_request_string=raw_default,
    )


def enabled_mcps_to_parameter_string(enabled: FrozenSet[str]) -> str:
    """Canonical comma-separated string for Firestore parameters."""
    return ",".join(sorted(enabled))


def merge_mcp_server_dicts(*parts: Optional[dict[str, dict]]) -> dict[str, dict]:
    """Merge MCP server config dicts for ClaudeAgentOptions.mcp_servers (later keys win)."""
    out: dict[str, dict] = {}
    for p in parts:
        if p:
            out.update(p)
    return out


def _split_mcp_args(args_str: str) -> list[str]:
    return shlex.split(args_str) if args_str.strip() else []


def build_playwright_mcp_config(settings: Settings) -> dict[str, dict]:
    """Stdio Playwright MCP (Microsoft). Empty if command/args invalid."""
    cmd = (settings.PLAYWRIGHT_MCP_COMMAND or "").strip()
    if not cmd:
        return {}
    args = _split_mcp_args(settings.PLAYWRIGHT_MCP_ARGS)
    return {
        MCP_PLAYWRIGHT: {
            "command": cmd,
            "args": args,
        }
    }


def build_figma_mcp_config(settings: Settings) -> dict[str, dict]:
    """Framelink Figma MCP. Requires FIGMA_ACCESS_TOKEN or FIGMA_API_KEY in settings/env."""
    token = (settings.FIGMA_ACCESS_TOKEN or settings.FIGMA_API_KEY or "").strip()
    if not token:
        logger.warning(
            "Figma MCP requested but FIGMA_ACCESS_TOKEN / FIGMA_API_KEY not set in backend env — "
            "Figma MCP will be skipped for this run"
        )
        return {}
    cmd = (settings.FIGMA_MCP_COMMAND or "").strip()
    if not cmd:
        return {}
    args = _split_mcp_args(settings.FIGMA_MCP_ARGS)
    return {
        MCP_FIGMA_SERVER_KEY: {
            "command": cmd,
            "args": args,
            "env": {"FIGMA_API_KEY": token},
        }
    }


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------

def _playwright_pair(settings: Settings, enabled_mcps: FrozenSet[str]) -> tuple[dict, List[str]]:
    """Playwright MCP config + tools if MCP_PLAYWRIGHT is in enabled_mcps."""
    config = build_playwright_mcp_config(settings) if MCP_PLAYWRIGHT in enabled_mcps else {}
    return config, (get_playwright_mcp_tools() if config else [])


def _figma_pair(settings: Settings, enabled_mcps: FrozenSet[str]) -> tuple[dict, List[str]]:
    """Figma MCP config + tools if MCP_FIGMA is in enabled_mcps."""
    config = build_figma_mcp_config(settings) if MCP_FIGMA in enabled_mcps else {}
    return config, (get_figma_mcp_tools() if config else [])


def _combine_pairs(*pairs: tuple[dict, List[str]]) -> tuple[dict, List[str]]:
    """Merge N (config_dict, tools_list) pairs into one."""
    servers: dict = {}
    tools: List[str] = []
    for config, pair_tools in pairs:
        servers.update(config)
        tools.extend(pair_tools)
    return servers, tools


# ---------------------------------------------------------------------------
# Per-workflow public builders
# ---------------------------------------------------------------------------

def spec_mcp_servers_and_tools(
    settings: Settings,
    enabled_mcps: Optional[FrozenSet[str]],
) -> tuple[dict, List[str]]:
    """Figma only — used by spec indexing and completeness workflows.

    Accepts None to fall back to the backend default (settings.MCP_SERVERS_ENABLED).
    """
    effective = (
        enabled_mcps
        if enabled_mcps is not None
        else parse_mcp_servers_enabled_string(settings.MCP_SERVERS_ENABLED)
    )
    allowed = effective & SPEC_INDEX_AND_COMPLETENESS_OPTIONAL_MCPS
    return _combine_pairs(_figma_pair(settings, allowed))


def coding_mcp_servers_and_tools(
    settings: Settings,
    enabled_mcps: FrozenSet[str],
) -> tuple[dict, List[str]]:
    """Playwright + Figma — used by generation and deploy/QA phase agents."""
    allowed = enabled_mcps & CODE_GENERATION_AND_DEPLOY_OPTIONAL_MCPS
    return _combine_pairs(
        _playwright_pair(settings, allowed),
        _figma_pair(settings, allowed),
    )


def planning_mcp_servers_and_tools(
    settings: Settings,
    enabled_mcps: FrozenSet[str],
) -> tuple[dict, List[str]]:
    """Figma only — used by planning agents."""
    allowed = enabled_mcps & PLANNING_OPTIONAL_AGENT_MCPS
    return _combine_pairs(
        _figma_pair(settings, allowed),
    )


_MCP_PROMPT_HINTS: dict[str, str] = {
    MCP_PLAYWRIGHT: (
        "Frontend development can be verified using Playwright MCP."
        " Use screenshots only when required or to compare against the user spec."
    ),
    MCP_FIGMA: (
        "If specs contain Figma links, retrieve Figma contents using Figma MCP."
    ),
}


def mcp_prompt_hints(enabled_mcps: FrozenSet[str]) -> str:
    """Return a compact prompt section listing active MCP capabilities, or '' if none."""
    lines = [hint for key, hint in _MCP_PROMPT_HINTS.items() if key in enabled_mcps]
    if not lines:
        return ""
    return "\n## Available MCP Tools\n" + "\n".join(f"- {line}" for line in lines) + "\n"
