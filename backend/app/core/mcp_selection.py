"""
McpSelector — single dispatch point for MCP server/tool config per workflow step.

Enforces the policy of which steps attach which MCPs and logs every selection decision.
Instances may be short-lived (one per step or phase call) or held across a phase loop;
settings and enabled_mcps are fixed per instance.

Policy (Playwright/Figma from enabled_mcps):
  GENERATION         — Playwright + Figma (per-phase intersection)
  DEPLOY_AND_E2E     — Playwright + Figma (per-phase intersection)
  all other steps    — no MCP servers
"""

import logging
from dataclasses import dataclass
from typing import FrozenSet, List

from app.core.config import Settings
from app.core.mcp_config import coding_mcp_servers_and_tools
from app.core.telemetry_context import TelemetryContext
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

    @property
    def is_empty(self) -> bool:
        """True when there are no MCP servers or tools to attach."""
        return not self.servers and not self.allowed_tools


class McpSelector:
    """Dispatch MCP selection per workflow step with structured logging.

    Usage: create one instance per phase loop (e.g. ``execute_all_phases``) or per
    individual workflow step.
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

        """
        if step in (WorkflowStepName.GENERATION, WorkflowStepName.DEPLOY_AND_E2E):
            mcps = phase_mcps if phase_mcps is not None else self._enabled_mcps
            servers, tools = coding_mcp_servers_and_tools(self._settings, mcps)
        else:
            servers, tools = {}, []

        selection = McpSelection(servers=servers, allowed_tools=tools)
        self._log(step, selection, phase_mcps=phase_mcps)
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
