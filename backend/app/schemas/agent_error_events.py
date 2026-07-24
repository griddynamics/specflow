"""Durable agent error/warning events surfaced to /status and the TUI.

Issue-focused by design: these are the states a user must know about
(crashes, retries, model switches, give-ups, workspace aborts) — not the
full agent lifecycle. Stored on the generation session doc under
``agent_error_events``, written ONLY via
``GenerationSessionStateMachine.record_agent_error_event`` (Commandment VII).
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.schemas.agent import AgentErrorType


class AgentErrorEventKind(str, Enum):
    AGENT_CRASH = "agent_crash"            # SDK/API crash or timeout in a phase query
    MODEL_FALLBACK = "model_fallback"      # mid-generation model switch (old -> new + reason)
    PHASE_GAVE_UP = "phase_gave_up"        # resume budget exhausted
    PHASE_INCOMPLETE = "phase_incomplete"  # phase checkpointed but validator unsatisfied
    WORKSPACE_ABORTED = "workspace_aborted"


class WorkspaceAgentState(str, Enum):
    """Transient per-workspace badge shown in the TUI workspace list.

    Absence of ``workspace_phases[ws]["agent_state"]`` means running normally.
    """

    RETRYING = "retrying"
    ABORTED = "aborted"


class AgentErrorEvent(BaseModel):
    at: str  # UTC ISO timestamp
    workspace_id: str
    kind: AgentErrorEventKind
    message: str  # human-readable, no internal jargon
    phase: Optional[int] = None
    error_type: Optional[AgentErrorType] = None
    crash_backoff: Optional[str] = None  # "n/6" at the time of the event
    next_attempt_at: Optional[str] = None  # UTC ISO, set when a backoff wait was scheduled
    model: Optional[str] = None  # active model; for model_fallback: the NEW model
    previous_model: Optional[str] = None  # for model_fallback
