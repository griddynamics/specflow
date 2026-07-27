"""
Telemetry context for propagating user context across async boundaries.

Uses contextvars to store request-level context (user_email, generation_id, session_id)
that can be accessed from anywhere in the request processing flow, including
deep in service layers where Request objects are not available.

**Concurrency**: The value bound in the ContextVar is an immutable *snapshot* dict —
updates must use copy-on-write (``{**existing, ...}``). In-place mutation of the
dict returned from ``get()`` is unsafe: asyncio copies execution contexts shallowly,
so concurrent Tasks can otherwise share one dict and overwrite workspace_name,
workflow, and MCP fields (symptom: wrong PostHog dimensions).
"""

import contextvars
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from app.core.mcp_config import EnabledMcpsResolution, enabled_mcps_to_parameter_string
from app.schemas.agent_error_events import AgentErrorEvent, WorkspaceAgentState
from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel

# Context variables for telemetry
_user_context: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    'telemetry_user_context', default=None
)

AgentQueryTotalsHandler = Callable[
    [str, str, str, ModelTokenUsage, float],
    Awaitable[None],
]
_agent_query_totals_handler: contextvars.ContextVar[Optional[AgentQueryTotalsHandler]] = (
    contextvars.ContextVar("agent_query_totals_handler", default=None)
)

AgentErrorEventHandler = Callable[[str, AgentErrorEvent], Awaitable[None]]
_agent_error_event_handler: contextvars.ContextVar[Optional[AgentErrorEventHandler]] = (
    contextvars.ContextVar("agent_error_event_handler", default=None)
)

WorkspaceAgentStateHandler = Callable[
    [str, str, Optional[WorkspaceAgentState]],
    Awaitable[None],
]
_workspace_agent_state_handler: contextvars.ContextVar[Optional[WorkspaceAgentStateHandler]] = (
    contextvars.ContextVar("workspace_agent_state_handler", default=None)
)


class TelemetryContext:
    """
    Context manager for telemetry user context.
    
    Stores user context in contextvars so it can be accessed from anywhere
    in the async call stack without passing Request objects through layers.
    """
    
    @staticmethod
    def set_user_context(
        user_email: Optional[str] = None,
        generation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_ids: Optional[list[str]] = None,
        workflow: Optional[Union[str, TelemetryWorkflowLabel]] = None,
        workspace_name: Optional[str] = None,
    ) -> None:
        """
        Merge provided fields into the current async context.

        Only fields that are explicitly passed (non-None) are updated; omitted
        fields are preserved from the existing context (including MCP keys set
        by set_mcp_resolution, set_workflow, etc.).

        Args:
            user_email: User email identifier
            generation_id: Current generation ID
            session_id: Current session ID
            workspace_ids: List of workspace IDs
            workflow: Plain name or ``TelemetryWorkflowLabel``; strings are wrapped with ``plain()``.
            workspace_name: Current workspace name/identifier
        """
        existing = _user_context.get() or {}
        wf: Optional[TelemetryWorkflowLabel] = None
        if workflow is not None:
            wf = (
                TelemetryWorkflowLabel.plain(workflow)
                if isinstance(workflow, str)
                else workflow
            )
        updates = {k: v for k, v in {
            "user_email": user_email,
            "generation_id": generation_id,
            "session_id": session_id,
            "workspace_ids": workspace_ids,
            "workflow": wf,
            "workspace_name": workspace_name,
        }.items() if v is not None}
        _user_context.set({**existing, **updates})
    
    @staticmethod
    def get_user_context() -> Optional[Dict[str, Any]]:
        """
        Get current user context.
        
        Returns:
            Dict with user_email, generation_id, session_id, workspace_ids,
            ``workflow`` (``TelemetryWorkflowLabel`` if set), or None
        """
        return _user_context.get()
    
    @staticmethod
    def get_user_email() -> Optional[str]:
        """Get user email from context."""
        context = _user_context.get()
        return context.get("user_email") if context else None
    
    @staticmethod
    def get_generation_id() -> Optional[str]:
        """Get generation ID from context."""
        context = _user_context.get()
        return context.get("generation_id") if context else None
    
    @staticmethod
    def get_session_id() -> Optional[str]:
        """Get session ID from context."""
        context = _user_context.get()
        return context.get("session_id") if context else None
    
    @staticmethod
    def get_workspace_ids() -> list[str]:
        """Get workspace IDs from context."""
        context = _user_context.get()
        return context.get("workspace_ids", []) if context else []
    
    @staticmethod
    def get_workflow() -> Optional[TelemetryWorkflowLabel]:
        """Get structured workflow label from context."""
        context = _user_context.get()
        if not context:
            return None
        raw = context.get("workflow")
        if isinstance(raw, TelemetryWorkflowLabel):
            return raw
        return None
    
    @staticmethod
    def set_workflow(workflow: Optional[TelemetryWorkflowLabel]) -> None:
        """
        Set workflow label for current async context.

        - ``set_workflow(None)`` — clear the workflow key (tests / reset).
        - ``set_workflow(TelemetryWorkflowLabel.plain("planning"))`` — plain name.
        - ``set_workflow(TelemetryWorkflowLabel.phase(PhaseKind.GENERATION, 1, "coding"))`` —
          phase-scoped label (see ``TelemetryWorkflowLabel.to_stored_string()``).

        ``get_workflow()`` returns the same ``TelemetryWorkflowLabel``; ``get_user_context()``
        stores it under the ``workflow`` key.
        """
        if workflow is None:
            existing = _user_context.get() or {}
            new_ctx = {k: v for k, v in existing.items() if k != "workflow"}
            _user_context.set(new_ctx if new_ctx else None)
            return
        existing = _user_context.get() or {}
        _user_context.set({**existing, "workflow": workflow})

    @staticmethod
    def get_workspace_name() -> Optional[str]:
        """Get workspace name from context."""
        context = _user_context.get()
        return context.get("workspace_name") if context else None

    @staticmethod
    def merge_generation_id(generation_id: str) -> None:
        """Merge generation_id into the current user context (preserves other keys)."""
        context = dict(_user_context.get() or {})
        context["generation_id"] = generation_id
        _user_context.set(context)

    @staticmethod
    def set_agent_query_totals_handler(handler: Optional[AgentQueryTotalsHandler]) -> None:
        """Register async callback for usage persistence.

        Signature:
        ``(generation_id, workflow_key, workspace_key, delta: ModelTokenUsage, cost_usd: float) -> Awaitable[None]``
        """
        _agent_query_totals_handler.set(handler)

    @staticmethod
    def get_agent_query_totals_handler() -> Optional[AgentQueryTotalsHandler]:
        return _agent_query_totals_handler.get()

    @staticmethod
    def set_agent_error_event_handler(handler: Optional[AgentErrorEventHandler]) -> None:
        """Register async callback for durable agent error/warning events.

        Signature: ``(generation_id, event: AgentErrorEvent) -> Awaitable[None]``
        """
        _agent_error_event_handler.set(handler)

    @staticmethod
    def get_agent_error_event_handler() -> Optional[AgentErrorEventHandler]:
        return _agent_error_event_handler.get()

    @staticmethod
    def set_workspace_agent_state_handler(handler: Optional[WorkspaceAgentStateHandler]) -> None:
        """Register async callback for the per-workspace agent-state badge.

        Signature: ``(generation_id, workspace_id, state: WorkspaceAgentState | None) -> Awaitable[None]``
        """
        _workspace_agent_state_handler.set(handler)

    @staticmethod
    def get_workspace_agent_state_handler() -> Optional[WorkspaceAgentStateHandler]:
        return _workspace_agent_state_handler.get()

    @staticmethod
    def set_workspace_name(workspace_name: str) -> None:
        """
        Set workspace name for current async context.

        Args:
            workspace_name: Workspace identifier (e.g., "workspace-abc123")
        """
        existing = _user_context.get() or {}
        _user_context.set({**existing, "workspace_name": workspace_name})

    @staticmethod
    def set_retry_count(retry_count: int) -> None:
        """Set the durable session-level retry counter for the current async context.

        Sourced from the `retry_count` field on the generation_session Firestore doc
        (incremented by GenerationSessionStateMachine.reset_for_retry). Stamped onto
        Langfuse trace metadata so a trace can be tied to attempt N of a session.
        """
        existing = _user_context.get() or {}
        _user_context.set({**existing, "retry_count": retry_count})

    @staticmethod
    def get_retry_count() -> Optional[int]:
        """Return the current retry_count, or None if not set."""
        context = _user_context.get()
        return context.get("retry_count") if context else None

    @staticmethod
    def set_phase_name(phase_name: str) -> None:
        """Set the human-readable phase name for the current coding/deploy phase.

        Included in ``$ai_generation`` PostHog events so events can be filtered by
        what the phase actually did (e.g. "Database setup") rather than only by number.
        Call before each ``run_phase_agent`` / ``agent_query`` in ``execute_all_phases``.
        Clear after the phase with ``set_phase_name("")`` to avoid leaking into
        subsequent non-phase calls (e.g. validators).
        """
        existing = _user_context.get() or {}
        _user_context.set({**existing, "phase_name": phase_name})

    @staticmethod
    def get_phase_name() -> Optional[str]:
        """Return the current phase name, or None if not set."""
        context = _user_context.get()
        return context.get("phase_name") if context else None

    @staticmethod
    def set_mcp_resolution(resolution: EnabledMcpsResolution) -> None:
        """Store MCP resolution so subsequent events include MCP properties."""
        existing = _user_context.get() or {}
        _user_context.set({
            **existing,
            "mcp_configuration_source": resolution.source,
            "mcps_user_requested": resolution.raw_request_string,
            "mcp_enabled_resolved_canonical": enabled_mcps_to_parameter_string(resolution.enabled),
            "mcps_used": sorted(resolution.enabled),
        })

    @staticmethod
    def get_mcp_props() -> Dict[str, Any]:
        """Return MCP properties to merge into event props; empty dict if not set."""
        context = _user_context.get()
        if not context:
            return {}
        props: Dict[str, Any] = {}
        for key in (
            "mcp_configuration_source",
            "mcps_user_requested",
            "mcp_enabled_resolved_canonical",
            "mcps_used",
            "mcp_attached_step",
            "mcp_attached_servers",
            "mcp_spec_prune_before",
            "mcp_spec_prune_after",
            "mcp_spec_prune_reasons",
        ):
            if key in context:
                props[key] = context[key]
        return props

    @staticmethod
    def set_mcp_attached(step: str, server_keys: list[str]) -> None:
        """Record which MCP servers were actually wired to the current agent call.

        Called by McpSelector before each agent_query so the subsequent
        $ai_generation / kb_init_completed / planning_completed PostHog events
        carry the per-call attachment rather than only the generation-level
        resolved set (mcp_enabled_resolved_canonical / mcps_used).
        """
        existing = _user_context.get() or {}
        _user_context.set({
            **existing,
            "mcp_attached_step": step,
            "mcp_attached_servers": server_keys,
        })

    @staticmethod
    def set_mcp_spec_prune_result(
        before_canonical: str,
        after_canonical: str,
        reasons_compact: str,
    ) -> None:
        """Record MCP prune outcome after spec analysis (before → after + compact reasons JSON)."""
        existing = _user_context.get() or {}
        _user_context.set({
            **existing,
            "mcp_spec_prune_before": before_canonical,
            "mcp_spec_prune_after": after_canonical,
            "mcp_spec_prune_reasons": reasons_compact,
        })

    @staticmethod
    def clear_context() -> None:
        """Clear user context (called after request completes)."""
        _user_context.set(None)
        _agent_query_totals_handler.set(None)
        _agent_error_event_handler.set(None)
        _workspace_agent_state_handler.set(None)
