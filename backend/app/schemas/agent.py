from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentErrorType(str, Enum):
    TOOL_CALL_FAILURE = "tool_call_failure"
    MODEL_ROUTING_FAILURE = "model_routing_failure"


@dataclass
class AgentResult:
    result: Optional[str] = None
    session_id: Optional[str] = None
    is_error: bool = False
    active_model: Optional[str] = None  # set when agent_query_with_resume switched to fallback
