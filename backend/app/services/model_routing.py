"""
Model routing failure detection and fallback logic.

When an OpenRouter model returns a malformed 200 (empty or invalid body), the agent
crashes rather than returning a structured error.  The functions here detect that
condition from exception messages and switch the active model to a tier-appropriate
fallback for the remainder of the agent call sequence.
"""
import logging
import re
from typing import Optional

from app.core.config import LLM_LOW_DEFAULT_FIRST_MODEL, LLM_MEDIUM_DEFAULT_FIRST_MODEL
from app.schemas.agent import AgentErrorType

# Error message patterns that indicate tool-calling incompatibility with non-Anthropic models
# via OpenRouter (GPT/Gemini failing to handle Anthropic's tool_use format).
_TOOL_CALL_ERROR_PATTERNS = [
    "tool_use",
    "tool_calls",
    "tool use",
    "function_call",
    "function call",
    "does not support tools",
    "unsupported tool",
    "invalid tool",
    "tool not found",
    "tool calling",
    "function calling",
    "ToolUseBlock",
    "tool_result",
    "tool result",
]

# "malformed response" / "empty response" are specific message fragments for the model routing failure mode.
_MODEL_ROUTING_FAILURE_PATTERNS = [
    "malformed response",
    "empty response",
]

# Transient network/connection failures (laptop asleep, lost internet) — retryable, no HTTP status.
_CONNECTION_ERROR_PATTERNS = [
    "unable to connect to api",
    "connectionrefused",
    "connection refused",
    "socket connection was closed",
    "connection error",
    "connection reset",
    "server disconnected",
    "network is unreachable",
]

def classify_error(error_msg: str, api_error_status: Optional[int] = None) -> Optional[AgentErrorType]:
    """Classify an agent error for targeted logging and telemetry."""
    msg_lower = error_msg.lower()
    if any(p.lower() in msg_lower for p in _TOOL_CALL_ERROR_PATTERNS):
        return AgentErrorType.TOOL_CALL_FAILURE
    # Connection failure checked before the 4xx/5xx guard (there is no HTTP status) and before routing.
    if any(p in msg_lower for p in _CONNECTION_ERROR_PATTERNS):
        return AgentErrorType.CONNECTION_ERROR
    # 4xx/5xx = definitive HTTP error, not a routing failure. Routing failures return HTTP 200
    # with a malformed body, so api_error_status=200 (or None) must NOT be excluded here.
    if api_error_status is not None and api_error_status >= 400:
        return None
    if any(p.lower() in msg_lower for p in _MODEL_ROUTING_FAILURE_PATTERNS):
        return AgentErrorType.MODEL_ROUTING_FAILURE
    return None


def extract_http_status_from_message(error_msg: str) -> Optional[int]:
    """Extract a 4xx/5xx HTTP status code from an exception message string, if present."""
    for token in re.findall(r"\d+", error_msg):
        code = int(token)
        if 400 <= code <= 599:
            return code
    return None


def get_fallback_model(model: Optional[str]) -> Optional[str]:
    """Return a cross-tier fallback model for routing-failure recovery."""
    if not model:
        return None
    return (
        LLM_LOW_DEFAULT_FIRST_MODEL
        if model.lower() == LLM_MEDIUM_DEFAULT_FIRST_MODEL.lower()
        else LLM_MEDIUM_DEFAULT_FIRST_MODEL
    )


def apply_model_fallback_if_routing_failure(
    err_str: str,
    active_model: str,
    fallback_candidate: Optional[str],
    logger: logging.Logger,
    context_label: str,
) -> tuple:
    """Check whether *err_str* is a routing failure and, if so, switch to the fallback model.

    Returns ``(new_active_model, new_fallback_candidate)``.  When no switch occurs the
    inputs are returned unchanged.  *context_label* appears in the log line (e.g. the
    current model name or ``"resume 2"``).
    """
    inferred_status = extract_http_status_from_message(err_str)
    if fallback_candidate and classify_error(err_str, api_error_status=inferred_status) == AgentErrorType.MODEL_ROUTING_FAILURE:
        logger.warning(
            "[MODEL FALLBACK] Routing failure on %s — switching to %s",
            context_label, fallback_candidate,
        )
        return fallback_candidate, None  # at most one switch per call
    return active_model, fallback_candidate
