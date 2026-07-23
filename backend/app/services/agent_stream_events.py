"""Convert Claude SDK stream messages into compact, safe UI events.

This module is the single source of truth for turning raw ``claude_agent_sdk``
messages into the small ``AgentStreamEvent`` payloads the TUI workspace drill-in
renders. It is the single control point for the message-flow *parse + safety* policy
(presentation/styling lives client-side in ``mcp_server/tui/render.py``). It is
intentionally conservative:

- It never streams full tool inputs or full tool results — only bounded previews.
- Tool *inputs* surface the tool name plus a short summary of one allow-listed
  argument (command / file / path / …), never the full argument dict.
- Tool *results* are the highest-risk content (arbitrary file/command output),
  so they are collapsed to a single line and capped at the stricter
  :data:`MAX_TOOL_RESULT_CHARS` rather than the assistant-text cap. This cap is a
  real security control: it runs server-side, so no client can request more.
- Every other ``message`` preview is capped at :data:`MAX_MESSAGE_CHARS`.
- Conversion never raises: ``message_to_ui_events`` swallows and logs any error
  and returns ``[]`` so a malformed message can never break the Claude
  consumption loop (the publisher path is best-effort by contract).

It has no network, Firestore, or filesystem dependency, and performs only small
in-process work so it is safe to call inline from ``process_query_stream``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import (
    ServerToolUseBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Hard cap on any previewed text. Keeps events tiny and avoids leaking large
# tool inputs / results into the live stream.
MAX_MESSAGE_CHARS = 400

# Stricter cap for tool *results*: they carry arbitrary file/command output and
# are the most likely to contain sensitive data, so they are clamped harder than
# assistant text and collapsed to a single line (see ``_tool_result_preview``).
MAX_TOOL_RESULT_CHARS = 160

_WHITESPACE_RE = re.compile(r"\s+")

EventKind = Literal[
    "assistant_text",
    "tool_use",
    "tool_result",
    "result",
    "system",
    "error",
    "unknown",
]

# Input keys (in priority order) that make a short, human-useful summary of a
# tool call without dumping the whole input.
_TOOL_SUMMARY_KEYS = (
    "description",
    "command",
    "file_path",
    "path",
    "pattern",
    "query",
    "prompt",
    "url",
)


class AgentStreamEvent(BaseModel):
    """A compact, display-ready agent message event.

    Mirrors the SSE payload contract documented in the TUI message-stream plan.
    All text fields are pre-truncated; consumers may render them verbatim.
    """

    timestamp: str = Field(..., description="UTC ISO timestamp at backend receive time")
    generation_id: str
    workspace_id: str
    workflow: Optional[str] = Field(None, description="Telemetry workflow label, if known")
    kind: EventKind
    message: str = Field("", description=f"Preview text, capped at {MAX_MESSAGE_CHARS} chars")
    tool_name: Optional[str] = None
    subagent_name: Optional[str] = None


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _collapse_whitespace(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) into single spaces."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_use_summary(tool_input: Any) -> str:
    """Build a short summary of a tool call input without dumping it whole."""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(f"{key}: {value}")
    # Fall back to the set of argument names only (never the values).
    return _truncate("args: " + ", ".join(sorted(tool_input.keys())))


def _subagent_name_from_tool(name: str, tool_input: Any) -> Optional[str]:
    """Derive a subagent name from a Task/Agent tool call when present."""
    if name not in ("Task", "Agent"):
        return None
    if isinstance(tool_input, dict):
        sub = tool_input.get("subagent_type")
        if isinstance(sub, str) and sub.strip():
            return sub.strip()
    return None


def _text_from_block_list(items: list) -> str:
    """Join the ``text`` fields of a tool-result block list, bounded by length."""
    parts: List[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
        if sum(len(p) for p in parts) >= MAX_MESSAGE_CHARS:
            break
    return " ".join(parts)


def _tool_result_preview(content: Any) -> str:
    """Single-line, safety-capped preview of a tool result's content.

    Tool results can be large multi-line file/command dumps with potentially
    sensitive data, so the raw text is collapsed to one line and clamped to
    :data:`MAX_TOOL_RESULT_CHARS` (stricter than the assistant-text cap).
    """
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        raw = _text_from_block_list(content)
    elif isinstance(content, dict) and isinstance(content.get("text"), str):
        raw = content["text"]
    else:
        return ""
    # Bound the slice scanned by the whitespace regex: a tool result can be a
    # multi-MB file/command dump, but we only ever keep MAX_TOOL_RESULT_CHARS, so
    # scanning a small leading window is enough and avoids O(n) work on huge text.
    raw = raw[: MAX_TOOL_RESULT_CHARS * 8]
    return _truncate(_collapse_whitespace(raw), MAX_TOOL_RESULT_CHARS)


def _assistant_events(
    message: AssistantMessage,
    *,
    generation_id: str,
    workspace_id: str,
    workflow: Optional[str],
) -> List[AgentStreamEvent]:
    events: List[AgentStreamEvent] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text = _truncate(block.text)
            if not text:
                continue
            events.append(
                AgentStreamEvent(
                    timestamp=_now_iso(),
                    generation_id=generation_id,
                    workspace_id=workspace_id,
                    workflow=workflow,
                    kind="assistant_text",
                    message=text,
                )
            )
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            events.append(
                AgentStreamEvent(
                    timestamp=_now_iso(),
                    generation_id=generation_id,
                    workspace_id=workspace_id,
                    workflow=workflow,
                    kind="tool_use",
                    message=_tool_use_summary(block.input),
                    tool_name=block.name,
                    subagent_name=_subagent_name_from_tool(block.name, block.input),
                )
            )
        # ThinkingBlock and other block types are intentionally skipped to keep
        # the live stream focused and avoid surfacing internal reasoning.
    return events


def _user_events(
    message: UserMessage,
    *,
    generation_id: str,
    workspace_id: str,
    workflow: Optional[str],
) -> List[AgentStreamEvent]:
    content = message.content
    if isinstance(content, str):
        return []
    events: List[AgentStreamEvent] = []
    for block in content:
        if isinstance(block, ToolResultBlock):
            preview = _tool_result_preview(block.content)
            if not preview:
                continue
            events.append(
                AgentStreamEvent(
                    timestamp=_now_iso(),
                    generation_id=generation_id,
                    workspace_id=workspace_id,
                    workflow=workflow,
                    kind="tool_result",
                    message=preview,
                )
            )
    return events


def synthetic_event(
    kind: EventKind,
    message: str,
    *,
    generation_id: str,
    workspace_id: str,
    workflow: Optional[str] = None,
) -> AgentStreamEvent:
    """Build a backend-authored event (crash/retry narrative), not from an SDK message.

    Used by the resume loop to surface errors and retry waits inline in the
    live message feed; same truncation contract as converted SDK messages.
    """
    return AgentStreamEvent(
        timestamp=_now_iso(),
        generation_id=generation_id,
        workspace_id=workspace_id,
        workflow=workflow,
        kind=kind,
        message=_truncate(_collapse_whitespace(message)),
    )


def message_to_ui_events(
    message: Any,
    *,
    generation_id: str,
    workspace_id: str,
    workflow: Optional[str] = None,
) -> List[AgentStreamEvent]:
    """Convert one SDK message into zero or more compact UI events.

    Never raises: any failure is logged at debug level and yields ``[]`` so the
    publisher tap can stay strictly best-effort.
    """
    try:
        if isinstance(message, AssistantMessage):
            return _assistant_events(
                message,
                generation_id=generation_id,
                workspace_id=workspace_id,
                workflow=workflow,
            )
        if isinstance(message, UserMessage):
            return _user_events(
                message,
                generation_id=generation_id,
                workspace_id=workspace_id,
                workflow=workflow,
            )
        if isinstance(message, ResultMessage):
            text = _truncate(message.result or message.subtype or "result")
            return [
                AgentStreamEvent(
                    timestamp=_now_iso(),
                    generation_id=generation_id,
                    workspace_id=workspace_id,
                    workflow=workflow,
                    kind="result",
                    message=text,
                )
            ]
        if isinstance(message, SystemMessage):
            # System messages are low-value/noisy; surface only the subtype.
            return [
                AgentStreamEvent(
                    timestamp=_now_iso(),
                    generation_id=generation_id,
                    workspace_id=workspace_id,
                    workflow=workflow,
                    kind="system",
                    message=_truncate(message.subtype or "system"),
                )
            ]
        # StreamEvent (partial deltas) and anything else are dropped — too noisy
        # for a human-readable live feed.
        return []
    except Exception:
        logger.debug(
            "agent_stream_events: failed to convert message type=%s",
            type(message).__name__,
            exc_info=True,
        )
        return []
