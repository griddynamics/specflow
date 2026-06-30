"""Per-agent_query stream tracer: builds Langfuse tool/generation observations from the message stream."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock, UserMessage

from .utils import (
    _MAX_OUTPUT_CHARS,
    _resolve_skill_name,
    _safe_update,
    _truncate,
)

logger = logging.getLogger(__name__)


class StreamTracer(ABC):
    """Contract for per-agent_query stream tracers."""

    @abstractmethod
    def push(self, message: Any) -> None: ...

    @abstractmethod
    def end_generation_on_error(self, reason: str) -> None: ...

    @abstractmethod
    def finalize_pending(self) -> None: ...

    @abstractmethod
    def complete_with_billing(self, cost_usd: float) -> None: ...


class _NullTracer(StreamTracer):
    """No-op stream tracer used when Langfuse is disabled."""

    def push(self, message: Any) -> None:
        pass

    def end_generation_on_error(self, reason: str) -> None:
        pass

    def finalize_pending(self) -> None:
        pass

    def complete_with_billing(self, cost_usd: float) -> None:
        pass


class LangfuseStreamTracer(StreamTracer):
    """Translates the agent_query() message stream into Langfuse tool/generation observations."""

    def __init__(
        self,
        generation: Any,
        redact_tool_io: bool = False,
        get_root_obs: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        self.generation = generation
        self.redact_tool_io = redact_tool_io
        # Injected so the stream tracer doesn't reach back into the singleton —
        # also makes the root-obs dependency explicit for tests.
        self._get_root_obs: Callable[[], Optional[Any]] = get_root_obs or (lambda: None)
        self.open_spans: dict[str, Any] = {}
        self.text_buffer: list[str] = []
        self.thinking_buffer: list[str] = []
        self._generation_ended = False
        self._pending_result: Optional[ResultMessage] = None
        # SDK session id is distinct from Langfuse's session_id (which is our generation_id);
        # captured once so log lines can be cross-referenced to the trace.
        self.sdk_session_id: Optional[str] = None

    def push(self, message: Any) -> None:
        try:
            self._capture_sdk_session_id(message)
            self._dispatch(message)
        except Exception:
            logger.warning(
                "LangfuseStreamTracer: error on message type=%s",
                type(message).__name__,
                exc_info=True,
            )

    def end_generation_on_error(self, reason: str) -> None:
        if self._generation_ended:
            return
        if self._pending_result is not None:
            return
        self._generation_ended = True
        kwargs: dict[str, Any] = {"level": "ERROR", "status_message": reason}
        if self.text_buffer:
            kwargs["output"] = "".join(self.text_buffer)[-_MAX_OUTPUT_CHARS:]
        try:
            self.generation.update(**kwargs)
            self.generation.end()
        except Exception:
            logger.warning(
                "LangfuseStreamTracer: failed to end generation on error (%r)",
                reason, exc_info=True,
            )

    def finalize_pending(self) -> None:
        for span in list(self.open_spans.values()):
            _safe_update(
                span,
                output="[stream ended with open tool span]",
                level="WARNING",
                status_message="stream_ended_with_open_tool",
            )
            try:
                span.end()
            except Exception:
                pass
        self.open_spans.clear()
        if self._pending_result is not None and not self._generation_ended:
            logger.warning(
                "LangfuseStreamTracer: complete_with_billing was not called; "
                "closing generation with SDK-reported cost as fallback"
            )
            sdk_cost = float(self._pending_result.total_cost_usd or 0.0)
            self._end_generation_for_result(self._pending_result, cost_usd=sdk_cost)
        elif not self._generation_ended:
            self.end_generation_on_error("stream_ended_without_result_message")

    def complete_with_billing(self, cost_usd: float) -> None:
        """End generation with authoritative resolved cost."""
        if self._generation_ended:
            return
        if self._pending_result is None:
            logger.warning("LangfuseStreamTracer: complete_with_billing without pending result")
            return
        self._end_generation_for_result(self._pending_result, cost_usd=cost_usd)

    def _dispatch(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            self._handle_assistant_message(message)
            return
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            self._handle_user_message(message)
            return
        if isinstance(message, ResultMessage):
            self._handle_result_message(message)

    def _capture_sdk_session_id(self, message: Any) -> None:
        if self.sdk_session_id is not None:
            return
        sid = getattr(message, "session_id", None)
        if not sid:
            return
        self.sdk_session_id = sid
        _safe_update(self.generation, metadata={"sdk_session_id": sid})
        root_obs = self._get_root_obs()
        if root_obs is not None:
            _safe_update(root_obs, metadata={"sdk_session_id": sid})

    def _handle_assistant_message(self, message: AssistantMessage) -> None:
        parent_id = getattr(message, "parent_tool_use_id", None)
        # Text/thinking blocks that precede a ToolUseBlock in the same message
        # are the model's reasoning for that tool call — attach to the span.
        pending_reasoning: list[str] = []
        pending_thinking: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                self.text_buffer.append(block.text)
                pending_reasoning.append(block.text)
                continue
            if hasattr(block, "thinking"):
                self.thinking_buffer.append(block.thinking)
                pending_thinking.append(block.thinking)
                continue
            if isinstance(block, ToolUseBlock):
                self._open_tool_span(block, parent_id, pending_reasoning, pending_thinking)
                pending_reasoning = []
                pending_thinking = []

    def _open_tool_span(
        self,
        block: ToolUseBlock,
        parent_id: Optional[str],
        reasoning: list[str],
        thinking: list[str],
    ) -> None:
        parent_obs = self.open_spans.get(parent_id) if parent_id else self.generation
        if parent_obs is None:
            parent_obs = self.generation
        tool_metadata = self._build_tool_metadata(block, parent_id, reasoning, thinking)
        span_name = self._resolve_span_name(block, tool_metadata)
        try:
            span = parent_obs.start_observation(
                name=span_name,
                as_type="tool",
                input=_truncate(block.input, self.redact_tool_io),
                metadata=tool_metadata,
            )
            self.open_spans[block.id] = span
        except Exception:
            logger.warning(
                "LangfuseStreamTracer: failed to create span for tool %r",
                block.name, exc_info=True,
            )

    def _build_tool_metadata(
        self,
        block: ToolUseBlock,
        parent_id: Optional[str],
        reasoning: list[str],
        thinking: list[str],
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "tool_use_id": block.id,
            "is_subagent": parent_id is not None,
        }
        if self.sdk_session_id:
            metadata["sdk_session_id"] = self.sdk_session_id
        reasoning_text = "".join(reasoning).strip()
        if reasoning_text:
            metadata["preceding_reasoning"] = _truncate(reasoning_text)
        thinking_text = "".join(thinking).strip()
        if thinking_text:
            metadata["preceding_thinking"] = _truncate(thinking_text)
        return metadata

    @staticmethod
    def _resolve_span_name(block: ToolUseBlock, tool_metadata: dict[str, Any]) -> str:
        # Skills surface as ToolUseBlock(name="Skill", input={...}); lift the
        # skill identifier from input so the span name is recognizable.
        skill_id = _resolve_skill_name(block)
        if skill_id is None:
            return block.name
        tool_metadata["is_skill"] = True
        tool_metadata["skill_name"] = skill_id[:200]
        return f"Skill: {skill_id}"

    def _handle_user_message(self, message: UserMessage) -> None:
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                self._close_tool_span(block)

    def _close_tool_span(self, block: ToolResultBlock) -> None:
        span = self.open_spans.pop(block.tool_use_id, None)
        if span is None:
            self._emit_orphan_event(block.tool_use_id)
            return
        try:
            span.update(
                output=_truncate(block.content, self.redact_tool_io),
                level="ERROR" if block.is_error else "DEFAULT",
            )
            span.end()
        except Exception:
            logger.warning(
                "LangfuseStreamTracer: failed to end span for tool_use_id=%r",
                block.tool_use_id, exc_info=True,
            )

    def _emit_orphan_event(self, tool_use_id: str) -> None:
        try:
            self.generation.create_event(
                name="orphan_tool_result",
                metadata={"tool_use_id": tool_use_id},
            )
        except Exception:
            pass

    def _handle_result_message(self, message: ResultMessage) -> None:
        if self._generation_ended:
            return
        # Billing finalized in complete_with_billing() once catalog cost is resolved.
        self._pending_result = message

    def _end_generation_for_result(self, message: ResultMessage, *, cost_usd: float) -> None:
        self._generation_ended = True
        usage = message.usage or {}
        thinking_text = "\n---\n".join(self.thinking_buffer).strip()
        try:
            self.generation.update(
                output="".join(self.text_buffer)[-_MAX_OUTPUT_CHARS:],
                usage_details=self._build_usage_details(usage),
                cost_details={"total": cost_usd},
                metadata=self._build_result_metadata(message, thinking_text),
                level="ERROR" if message.is_error else "DEFAULT",
            )
            self.generation.end()
        except Exception:
            logger.warning("LangfuseStreamTracer: failed to end generation", exc_info=True)

    @staticmethod
    def _build_usage_details(usage: dict[str, Any]) -> dict[str, int]:
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        return {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        }

    def _build_result_metadata(self, message: ResultMessage, thinking_text: str) -> dict[str, Any]:
        return {
            "num_turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "stop_reason": message.stop_reason,
            "is_error": message.is_error,
            "thinking_chars": sum(len(t) for t in self.thinking_buffer),
            "thinking": _truncate(thinking_text) if thinking_text else None,
            "sdk_session_id": self.sdk_session_id,
        }
