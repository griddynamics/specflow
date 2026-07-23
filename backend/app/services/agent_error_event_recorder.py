"""Best-effort recording of agent error events from deep inside agent code.

Mirrors the ``agent_query_totals_handler`` pattern: the workflow registers
bound GenerationSessionService methods on TelemetryContext
(``build_workflow_context``), and agent-level code calls the module functions
below without threading services through call signatures. Outside a workflow
(unit tests, one-off queries) both functions are silent no-ops.

They NEVER raise — error reporting must not break the agent loop it reports
on (and events fired while the session is no longer RUNNING are dropped by
the state machine guard and swallowed here).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.core.telemetry_context import TelemetryContext
from app.schemas.agent import AgentErrorType
from app.schemas.agent_error_events import (
    AgentErrorEvent,
    AgentErrorEventKind,
    WorkspaceAgentState,
)

logger = logging.getLogger(__name__)

# Keep durable event messages compact; full detail stays in the agent logs.
_MAX_EVENT_MESSAGE_CHARS = 300


def _resolve_identity(workspace_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(generation_id, workspace_id) from args + TelemetryContext fallback."""
    generation_id = TelemetryContext.get_generation_id()
    ws = workspace_id or TelemetryContext.get_workspace_name()
    return generation_id, ws


async def record_agent_error_event_safe(
    kind: AgentErrorEventKind,
    message: str,
    *,
    workspace_id: Optional[str] = None,
    phase: Optional[int] = None,
    error_type: Optional[AgentErrorType] = None,
    crash_backoff: Optional[str] = None,
    next_attempt_at: Optional[str] = None,
    model: Optional[str] = None,
    previous_model: Optional[str] = None,
) -> None:
    """Record one durable agent error/warning event; silent no-op outside a workflow."""
    handler = TelemetryContext.get_agent_error_event_handler()
    generation_id, ws = _resolve_identity(workspace_id)
    if handler is None or not generation_id or not ws:
        return
    event = AgentErrorEvent(
        at=datetime.now(timezone.utc).isoformat(),
        workspace_id=ws,
        kind=kind,
        message=(message or "")[:_MAX_EVENT_MESSAGE_CHARS],
        phase=phase,
        error_type=error_type,
        crash_backoff=crash_backoff,
        next_attempt_at=next_attempt_at,
        model=model,
        previous_model=previous_model,
    )
    try:
        await handler(generation_id, event)
    except Exception:
        logger.warning(
            "Failed to record agent error event (kind=%s ws=%s)", kind, ws, exc_info=True
        )


async def set_workspace_retry_state_safe(
    state: Optional[WorkspaceAgentState],
    *,
    workspace_id: Optional[str] = None,
) -> None:
    """Set/clear the per-workspace agent badge; silent no-op outside a workflow."""
    handler = TelemetryContext.get_workspace_agent_state_handler()
    generation_id, ws = _resolve_identity(workspace_id)
    if handler is None or not generation_id or not ws:
        return
    try:
        await handler(generation_id, ws, state)
    except Exception:
        logger.warning(
            "Failed to set workspace agent state (%s ws=%s)", state, ws, exc_info=True
        )
