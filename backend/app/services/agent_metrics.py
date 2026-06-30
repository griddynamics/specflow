"""Stream Claude Code SDK messages into aggregated tool / MCP usage metrics."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_agent_sdk.types import (
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.core.tool_usage import MCP_TOOL_PREFIX
from app.schemas.agent_metrics import AgentUsage, ToolUsage

logger = logging.getLogger(__name__)

_BUCKET_MAIN_TOOLS = "main_tools"
_BUCKET_MAIN_MCP = "main_mcp"
_BUCKET_SUB_TOOLS = "sub_tools"
_BUCKET_SUB_MCP = "sub_mcp"

# Claude Code SDK uses "Task" in recent versions and "Agent" in older versions
# to represent the tool that spawns a subagent.
_SUBAGENT_TOOL_TASK = "Task"
_SUBAGENT_TOOL_AGENT = "Agent"


class _ToolMeta(TypedDict):
    name: str
    assistant_parent: Optional[str]


@dataclass
class _BucketEntry:
    count: int = 0
    tokens: Optional[int] = None


class AgentMetrics(ABC):
    """Base type for per-stream agent usage aggregation."""

    @abstractmethod
    def push(self, message: Any) -> None:
        """Ingest one SDK message."""

    @abstractmethod
    def get_metrics(self) -> dict[str, Any]:
        """Return JSON-friendly metrics for logs and telemetry."""


def _is_mcp_tool(name: str) -> bool:
    return name.startswith(MCP_TOOL_PREFIX)


def _is_subagent_tool(name: str) -> bool:
    return name in (_SUBAGENT_TOOL_TASK, _SUBAGENT_TOOL_AGENT)


def _extract_tokens_from_tool_result(block: ToolResultBlock) -> Optional[int]:
    content = block.content
    if content is None:
        return None
    if isinstance(content, str):
        return _tokens_from_json_string(content)
    if isinstance(content, dict):
        return _tokens_from_dict(content)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                n = _tokens_from_dict(item)
            elif isinstance(item, str):
                n = _tokens_from_json_string(item)
            else:
                n = None
            if n is not None:
                return n
    return None


def _tokens_from_json_string(s: str) -> Optional[int]:
    """Parse JSON-encoded tool output; return None if not a JSON object."""
    stripped = s.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return _tokens_from_dict(parsed) if isinstance(parsed, dict) else None


def _tokens_from_dict(d: dict[str, Any]) -> Optional[int]:
    for key in ("totalTokens", "total_tokens"):
        v = d.get(key)
        if isinstance(v, int):
            return v
    usage = d.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
        inp = usage.get("input_tokens")
        outp = usage.get("output_tokens")
        if isinstance(inp, int) and isinstance(outp, int):
            return inp + outp
        if isinstance(inp, int):
            return inp
        if isinstance(outp, int):
            return outp
    # Top-level input/output (some MCP payloads omit total)
    inp_t = d.get("input_tokens")
    outp_t = d.get("output_tokens")
    if isinstance(inp_t, int) and isinstance(outp_t, int):
        return inp_t + outp_t
    return None


def _task_completion_stats(content: Any) -> Tuple[Optional[int], Optional[int]]:
    """From Task tool result content, return (totalToolUseCount, totalTokens) if present."""
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            tc = item.get("totalToolUseCount")
            tt = item.get("totalTokens")
            if isinstance(tc, int) or isinstance(tt, int):
                return (
                    int(tc) if isinstance(tc, int) else None,
                    int(tt) if isinstance(tt, int) else None,
                )
    return None, None


def _structured_task_payload(content: Any) -> Optional[dict[str, Any]]:
    """If content encodes a dict-like Task result (e.g. embedded usage), return it."""
    if isinstance(content, dict):
        if "totalToolUseCount" in content or "totalTokens" in content:
            return content
        nested = content.get("usage")
        if isinstance(nested, dict) and "total_tokens" in nested:
            return content
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if "totalToolUseCount" in item or "totalTokens" in item:
                return item
    return None


class ClaudeCodeSdkAgentMetrics(AgentMetrics):
    """
    Reconstruct main vs Task-subagent tool usage from a single query() stream.

    Uses ToolUseBlock.id registration and ToolResultBlock.tool_use_id to pair calls.
    Main vs subagent: AssistantMessage.parent_tool_use_id is None for the top-level
    agent; subagent tool calls carry the Task tool_use_id as parent.

    Claude Code Task subagents are single-depth — a subagent cannot spawn further
    subagents — so AssistantMessage.parent_tool_use_id being non-None is an exact
    discriminator for subagent context.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, _ToolMeta] = {}
        # Per Task id: number of completed non-Task tool results under that Task
        self._per_task_inner_results: Dict[str, int] = {}
        self._buckets: Dict[str, Dict[str, _BucketEntry]] = {
            _BUCKET_MAIN_TOOLS: {},
            _BUCKET_MAIN_MCP: {},
            _BUCKET_SUB_TOOLS: {},
            _BUCKET_SUB_MCP: {},
        }
        self._unknown_result_ids: set[str] = set()

    def push(self, message: Any) -> None:
        try:
            if isinstance(message, AssistantMessage):
                self._push_assistant(message)
            elif isinstance(message, UserMessage):
                self._push_user(message)
            elif isinstance(message, (ResultMessage, SystemMessage, StreamEvent)):
                return
            else:
                logger.debug("ClaudeCodeSdkAgentMetrics: ignored message type %s", type(message).__name__)
        except Exception:
            logger.warning(
                "ClaudeCodeSdkAgentMetrics: failed to process message type=%s",
                type(message).__name__,
                exc_info=True,
            )

    def _push_assistant(self, message: AssistantMessage) -> None:
        parent = message.parent_tool_use_id
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                self._registry[block.id] = {
                    "name": block.name,
                    "assistant_parent": parent,
                }

    def _push_user(self, message: UserMessage) -> None:
        content = message.content
        if isinstance(content, str):
            return
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._push_tool_result(block)

    def _push_tool_result(self, block: ToolResultBlock) -> None:
        tid = block.tool_use_id
        meta = self._registry.pop(tid, None)
        if meta is None:
            if tid not in self._unknown_result_ids:
                self._unknown_result_ids.add(tid)
                logger.debug(
                    "ClaudeCodeSdkAgentMetrics: ToolResultBlock for unknown tool_use_id=%s",
                    tid,
                )
            return

        name = meta["name"]
        assistant_parent = meta["assistant_parent"]
        is_subagent_context = assistant_parent is not None
        bucket_key = self._bucket_key(name, is_subagent_context)
        tokens = _extract_tokens_from_tool_result(block)

        self._increment_bucket(bucket_key, name, tokens)

        if assistant_parent is not None and not _is_subagent_tool(name):
            self._per_task_inner_results[assistant_parent] = (
                self._per_task_inner_results.get(assistant_parent, 0) + 1
            )

        if _is_subagent_tool(name):
            self._maybe_reconcile_task(tid, block)

    def _maybe_reconcile_task(self, task_tool_use_id: str, block: ToolResultBlock) -> None:
        inner = self._per_task_inner_results.pop(task_tool_use_id, 0)
        content = block.content
        stats_dict = _structured_task_payload(content)
        expected_count: Optional[int] = None
        if stats_dict:
            tc = stats_dict.get("totalToolUseCount")
            if isinstance(tc, int):
                expected_count = tc
        if expected_count is None:
            tc2, _ = _task_completion_stats(content)
            expected_count = tc2
        if expected_count is not None and expected_count != inner:
            logger.debug(
                "ClaudeCodeSdkAgentMetrics: Task %s totalToolUseCount=%s vs tracked inner=%s",
                task_tool_use_id,
                expected_count,
                inner,
            )

    @staticmethod
    def _bucket_key(tool_name: str, is_subagent_context: bool) -> str:
        mcp = _is_mcp_tool(tool_name)
        if is_subagent_context:
            return _BUCKET_SUB_MCP if mcp else _BUCKET_SUB_TOOLS
        return _BUCKET_MAIN_MCP if mcp else _BUCKET_MAIN_TOOLS

    def _increment_bucket(self, bucket_key: str, tool_name: str, tokens: Optional[int]) -> None:
        bucket = self._buckets[bucket_key]
        if tool_name not in bucket:
            bucket[tool_name] = _BucketEntry()
        entry = bucket[tool_name]
        entry.count += 1
        if tokens is not None:
            entry.tokens = tokens if entry.tokens is None else entry.tokens + tokens

    def _bucket_to_agent_usage(self, bucket_key: str) -> AgentUsage:
        bucket = self._buckets[bucket_key]
        tools_list: List[ToolUsage] = []
        sum_tokens: Optional[int] = None
        for name in sorted(bucket.keys()):
            e = bucket[name]
            tools_list.append(ToolUsage(name=name, count=e.count, tokens=e.tokens))
            if e.tokens is not None:
                sum_tokens = e.tokens if sum_tokens is None else sum_tokens + e.tokens
        return AgentUsage(tools_usage_count=tools_list, tools_tokens=sum_tokens)

    def _flush_pending_registry(self) -> None:
        """Count any ToolUseBlock entries that never received a ToolResultBlock.

        This can happen when the stream ends mid-turn (interrupt, error, timeout).
        We record the call with tokens=None so the tool name appears in the metrics.
        """
        if not self._registry:
            return
        pending = list(self._registry.items())
        self._registry.clear()
        logger.debug(
            "ClaudeCodeSdkAgentMetrics: flushing %d pending (unmatched) tool calls: %s",
            len(pending),
            [meta["name"] for _, meta in pending],
        )
        for _tid, meta in pending:
            name = meta["name"]
            is_subagent_context = meta["assistant_parent"] is not None
            bucket_key = self._bucket_key(name, is_subagent_context)
            self._increment_bucket(bucket_key, name, tokens=None)

    def get_metrics(self) -> dict[str, Any]:
        """Return JSON-friendly metrics for logs and telemetry.

        Note: calling this method has a side effect — any ToolUseBlock entries
        that never received a matching ToolResultBlock (e.g. stream interrupted
        mid-turn) are flushed into the buckets with tokens=None before the result
        is computed. This makes the call idempotent: subsequent calls return the
        same counts without double-counting.
        """
        try:
            self._flush_pending_registry()
            main_tool = self._bucket_to_agent_usage(_BUCKET_MAIN_TOOLS)
            main_mcp = self._bucket_to_agent_usage(_BUCKET_MAIN_MCP)
            sub_tool = self._bucket_to_agent_usage(_BUCKET_SUB_TOOLS)
            sub_mcp = self._bucket_to_agent_usage(_BUCKET_SUB_MCP)
            return {
                "main_agent_tool_usage": main_tool.to_dict(),
                "main_agent_mcp_usage": main_mcp.to_dict(),
                "subagents_tool_usage": sub_tool.to_dict(),
                "subagents_mcp_usage": sub_mcp.to_dict(),
            }
        except Exception:
            logger.warning("ClaudeCodeSdkAgentMetrics: failed to compute metrics", exc_info=True)
            return {}


def tool_usage_telemetry_props(breakdown: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten breakdown for PostHog: token totals and compact tool-count JSON.
    No tool inputs or paths — names and counts only.
    """
    out: dict[str, Any] = {}

    def add_usage(key_tokens: str, key_json: str, usage: dict[str, Any]) -> None:
        tools = usage.get("tools_usage_count") or []
        tt = usage.get("tools_tokens")
        out[key_tokens] = tt if tt is not None else 0
        slim = [{"name": t["name"], "count": t["count"]} for t in tools if isinstance(t, dict)]
        if slim:
            out[key_json] = json.dumps(slim, separators=(",", ":"))

    main_t = breakdown.get("main_agent_tool_usage") or {}
    main_m = breakdown.get("main_agent_mcp_usage") or {}
    sub_t = breakdown.get("subagents_tool_usage") or {}
    sub_m = breakdown.get("subagents_mcp_usage") or {}

    add_usage("main_agent_tool_tokens", "main_agent_tool_usage_json", main_t)
    add_usage("main_agent_mcp_tokens", "main_agent_mcp_usage_json", main_m)
    add_usage("subagents_tool_tokens", "subagents_tool_usage_json", sub_t)
    add_usage("subagents_mcp_tokens", "subagents_mcp_usage_json", sub_m)

    return out
