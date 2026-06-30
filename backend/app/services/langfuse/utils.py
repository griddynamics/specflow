"""Pure helpers and module constants shared by the langfuse package."""

from __future__ import annotations

import json
from typing import Any, Optional

from claude_agent_sdk.types import ToolUseBlock

_MAX_FIELD_CHARS = 10_000
_MAX_PROPAGATE_VALUE_CHARS = 190  # propagate_attributes values must be ≤200 chars
_MAX_OUTPUT_CHARS = 50_000
_PROPAGATED_CONTEXT_KEYS = ("workflow", "workspace_name", "retry_count")
_SKILL_TOOL_NAME = "Skill"
_SKILL_INPUT_KEYS = ("skill_name", "name", "command")


def _truncate(value: Any, redact: bool = False) -> Any:
    if redact:
        return "[redacted]"
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + "…[truncated]"
    if isinstance(value, (dict, list)):
        try:
            serialized = json.dumps(value)
        except Exception:
            serialized = str(value)
        if len(serialized) > _MAX_FIELD_CHARS:
            return serialized[:_MAX_FIELD_CHARS] + "…[truncated]"
    return value


def _to_propagate_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)[:_MAX_PROPAGATE_VALUE_CHARS]


def _resolve_skill_name(block: ToolUseBlock) -> Optional[str]:
    if block.name != _SKILL_TOOL_NAME or not isinstance(block.input, dict):
        return None
    for key in _SKILL_INPUT_KEYS:
        value = block.input.get(key)
        if value:
            return str(value)
    return None


def _safe_update(obs: Any, **kwargs: Any) -> None:
    """Best-effort update; tracing errors must never propagate to user code."""
    try:
        obs.update(**kwargs)
    except Exception:
        pass
