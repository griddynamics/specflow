"""
McpSelector — single dispatch point for MCP server/tool config per workflow step.

Enforces the policy of which steps attach which MCPs and logs every selection decision.
Instances may be short-lived (one per step or phase call) or held across a phase loop;
settings and enabled_mcps are fixed per instance.

Policy (Rosetta always on when ROSETTA_MCP_ENABLED; Playwright/Figma from enabled_mcps):
  KB_INIT            — Rosetta servers + kb_tools + rosetta write-allowed tools
  GENERATION         — Rosetta + Playwright + Figma (per-phase intersection)
  DEPLOY_AND_E2E     — Rosetta + Playwright + Figma (per-phase intersection)
  all other steps    — no MCP servers
"""

import logging
from dataclasses import dataclass
from typing import FrozenSet, List

from app.core.config import Settings
from app.core.mcp_config import (
    build_rosetta_mcp_config,
    coding_mcp_servers_and_tools,
)
from app.core.rosetta_kb import RosettaKbMode, resolve_rosetta_kb_mode
from app.core.telemetry_context import TelemetryContext
from app.core.tool_usage import (
    get_rosetta_allowed_tools,
    get_rosetta_kb_tools,
    get_rosetta_plugin_tools,
)
from app.schemas.generation_workflow_enums import WorkflowStepName


@dataclass(frozen=True)
class McpSelection:
    """MCP server config and tool allowlist for a single agent invocation.

    ``servers`` maps server-key → stdio config dict, matching the shape produced by
    ``build_*_mcp_config`` helpers and expected by ``ClaudeAgentOptions.mcp_servers``.
    Pass ``mcp.servers or None`` (not ``mcp.servers``) to SDK calls that treat ``None``
    as the "no servers" sentinel and may behave differently with an empty dict.
    """

    servers: dict[str, dict]
    allowed_tools: List[str]
    # Which Rosetta KB source this selection represents. Only meaningful for KB_INIT;
    # every other step leaves it DISABLED. PROVISIONED_PLUGIN means the bundled plugin
    # is vendored into the workspace ``.claude/`` (WorkspaceManager.provision_rosetta_plugin)
    # and discovered via setting_sources=["project"] — no MCP server, no SDK ``plugins=``
    # loader. Mutually exclusive with ``servers`` (live MCP wins over the bundled plugin).
    kb_mode: RosettaKbMode = RosettaKbMode.DISABLED

    @property
    def uses_provisioned_plugin(self) -> bool:
        """True when the KB comes from the provisioned (vendored) Rosetta plugin."""
        return self.kb_mode is RosettaKbMode.PROVISIONED_PLUGIN

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to attach — no MCP servers and no provisioned plugin."""
        return not self.servers and not self.uses_provisioned_plugin


class McpSelector:
    """Dispatch MCP selection per workflow step with structured logging.

    ``for_step`` covers PLANNING, GENERATION, DEPLOY_AND_E2E, and all no-MCP
    steps. ``for_kb_init`` handles the special case where the agent also needs
    write-access tools scoped to the rosetta output directory.

    Usage: create one instance per phase loop (e.g. ``execute_all_phases``) or
    per individual step call (e.g. ``run_kb_init_agent``).
    Both patterns are valid — the selector is stateless between calls.
    """

    def __init__(
        self,
        settings: Settings,
        enabled_mcps: FrozenSet[str],
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._enabled_mcps = enabled_mcps
        self._logger = logger

    def for_step(
        self,
        step: WorkflowStepName,
        *,
        phase_mcps: FrozenSet[str] | None = None,
    ) -> McpSelection:
        """Return MCP selection for a workflow step.

        For GENERATION and DEPLOY_AND_E2E pass ``phase_mcps`` — the per-phase
        intersection of ``plan.applicable_agent_mcps ∩ enabled_mcps ∩ SUPPORTED_MCPS``.
        When ``phase_mcps`` is None it falls back to ``enabled_mcps``.

        Raises ValueError for KB_INIT — use ``for_kb_init`` instead.
        """
        if step == WorkflowStepName.KB_INIT:
            raise ValueError(
                "Use for_kb_init(workspace_root, rosetta_dir) for the kb_init step — "
                "it needs workspace paths to scope rosetta write-allowed tools."
            )

        if step in (WorkflowStepName.GENERATION, WorkflowStepName.DEPLOY_AND_E2E):
            mcps = phase_mcps if phase_mcps is not None else self._enabled_mcps
            servers, tools = coding_mcp_servers_and_tools(self._settings, mcps)
        else:
            servers, tools = {}, []

        selection = McpSelection(servers=servers, allowed_tools=tools)
        self._log(step, selection, phase_mcps=phase_mcps)
        return selection

    def for_kb_init(self, workspace_root: str) -> McpSelection:
        """Return MCP selection for the kb_init step, dispatched on ``RosettaKbMode``.

        The mode is resolved once in ``app.core.rosetta_kb`` (the single source of truth)
        so this selection and the actions keyed on it — provisioning, ``CLAUDE_PLUGIN_ROOT``
        injection, unpack-skip, KB prompt shape — can never disagree:

          - ``LIVE_MCP``           -> attach the live Rosetta ``KnowledgeBase`` MCP server.
          - ``PROVISIONED_PLUGIN`` -> the default: the bundled plugin is vendored into the
            workspace ``.claude/`` (WorkspaceManager.provision_rosetta_plugin) and discovered
            via setting_sources=["project"]; the agent drives init via its Skills/commands.
            No MCP server, no SDK ``plugins=`` loader.
          - ``DISABLED``           -> empty selection; callers check ``is_empty`` and skip.

        Both non-empty modes need the rosetta write-scoped tools so the agent can populate
        the output dir. Callers prepend ``get_common_allowed_tools(workspace_root)`` for
        general file access. ``settings.ROSETTA_OUTPUT_DIR`` is only read when a mode is
        active, so it need not exist on the settings mock when KB is DISABLED.
        """
        mode = resolve_rosetta_kb_mode(self._settings)
        if mode is RosettaKbMode.LIVE_MCP:
            rosetta_dir = self._settings.ROSETTA_OUTPUT_DIR
            servers = build_rosetta_mcp_config(self._settings)
            tools = get_rosetta_kb_tools() + get_rosetta_allowed_tools(workspace_root, rosetta_dir)
            selection = McpSelection(servers=servers, allowed_tools=tools, kb_mode=mode)
        elif mode is RosettaKbMode.PROVISIONED_PLUGIN:
            rosetta_dir = self._settings.ROSETTA_OUTPUT_DIR
            tools = get_rosetta_plugin_tools() + get_rosetta_allowed_tools(workspace_root, rosetta_dir)
            selection = McpSelection(servers={}, allowed_tools=tools, kb_mode=mode)
        else:
            selection = McpSelection(servers={}, allowed_tools=[], kb_mode=mode)
        self._log(WorkflowStepName.KB_INIT, selection)
        return selection

    def _log(
        self,
        step: WorkflowStepName,
        selection: McpSelection,
        *,
        phase_mcps: FrozenSet[str] | None = None,
    ) -> None:
        server_keys = sorted(selection.servers)
        # Record per-call attachment in telemetry context so the next PostHog event
        # (e.g. $ai_generation) carries mcp_attached_step + mcp_attached_servers,
        # distinct from mcp_enabled_resolved_canonical (generation-level resolved set).
        TelemetryContext.set_mcp_attached(step.value, server_keys)
        if selection.is_empty:
            self._logger.debug("mcp_selection step=%s servers=none", step.value)
            return
        if phase_mcps is not None and phase_mcps != self._enabled_mcps:
            self._logger.info(
                "mcp_selection step=%s servers=%s phase_mcps=%s generation_mcps=%s",
                step.value,
                server_keys,
                sorted(phase_mcps),
                sorted(self._enabled_mcps),
            )
        else:
            self._logger.info(
                "mcp_selection step=%s servers=%s",
                step.value,
                server_keys,
            )
