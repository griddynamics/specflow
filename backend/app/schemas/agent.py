from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentErrorType(str, Enum):
    TOOL_CALL_FAILURE = "tool_call_failure"
    MODEL_ROUTING_FAILURE = "model_routing_failure"
    CONNECTION_ERROR = "connection_error"


@dataclass
class AgentResult:
    result: Optional[str] = None
    session_id: Optional[str] = None
    is_error: bool = False
    active_model: Optional[str] = None  # set when agent_query_with_resume switched to fallback


@dataclass(frozen=True)
class AgentQueryProgress:
    """Observable work one agent_query performed before it ended.

    Captured from the message stream as it flows, so the numbers survive a
    mid-stream crash (the exception path reads the same accumulator).
    """
    num_messages: int = 0
    num_tool_uses: int = 0


class AgentQueryError(Exception):
    """agent_query failure carrying the progress observed before the crash.

    Subclasses Exception so every existing broad handler keeps working; the
    resume loop reads ``progress`` to decide whether the failed attempt still
    counts as saved work.
    """

    def __init__(self, message: str, progress: Optional[AgentQueryProgress] = None):
        super().__init__(message)
        self.progress = progress
