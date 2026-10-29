from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from functools import partial
from typing import Any

from llm.tracing.core import (
    ToolEnd,
    ToolStart,
    TraceCapture,
    TraceClose,
    TraceResult,
    TraceSink,
    TraceStart,
)


class TraceSession:
    """Provider-neutral state and failure isolation for one traced run."""

    def __init__(
        self,
        sink: TraceSink | None,
        *,
        name: str = "llm.agent",
        model: str | None = None,
        prompt: Any = None,
        system_prompt: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        capture: TraceCapture | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("name must not be empty")
        self.sink = sink
        self.name = name
        self.model = model
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.metadata = dict(metadata or {})
        self.capture = capture or TraceCapture()
        self.error_count = 0
        self.closed = False
        self._started = False
        self._unavailable = False
        self._session_id: str | None = None
        self._num_messages = 0
        self._num_tool_uses = 0
        self._open_tools: set[str] = set()
        self._output_parts: list[str] = []
        self._reasoning_parts: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.sink is not None and not self._unavailable

    def record_failure(self) -> None:
        self.error_count += 1

    def begin_next_run(self) -> None:
        """Reset completed-run state while preserving session configuration."""

        if not self.closed:
            raise RuntimeError("cannot begin a new run before closing the current run")
        self.closed = False
        self._started = False
        self._session_id = None
        self._num_messages = 0
        self._num_tool_uses = 0
        self._open_tools.clear()
        self._output_parts.clear()
        self._reasoning_parts.clear()

    def message(
        self,
        *,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        if not self.enabled or self.closed:
            return
        self._num_messages += 1
        if self._session_id is None and session_id:
            self._session_id = session_id
        if model:
            self.model = model
        was_started = self._started
        if not self._ensure_started():
            return
        if model is not None and was_started:
            self._call(lambda sink: sink.update_model(model))

    def system_message(self, data: Mapping[str, Any]) -> None:
        if self.capture.system_message_data and self._started:
            self._call(lambda sink: sink.record_system_message(data))

    def assistant_text(self, text: str) -> None:
        if self.capture.assistant_output:
            self._output_parts.append(text)

    def reasoning(self, text: str) -> None:
        if self.capture.reasoning:
            self._reasoning_parts.append(text)

    def tool_started(
        self,
        *,
        tool_id: str,
        name: str,
        is_server: bool,
        tool_input: Any = None,
    ) -> None:
        if not self._started or self.closed:
            return
        self._num_tool_uses += 1
        event = ToolStart(
            id=tool_id,
            name=name,
            is_server=is_server,
            input=tool_input if self.capture.tool_io else None,
        )
        if self._call(lambda sink: sink.start_tool(event)):
            self._open_tools.add(tool_id)

    def tool_finished(
        self,
        *,
        tool_id: str,
        output: Any = None,
        is_error: bool = False,
    ) -> None:
        if tool_id not in self._open_tools:
            return
        self._open_tools.remove(tool_id)
        event = ToolEnd(
            id=tool_id,
            output=output if self.capture.tool_io else None,
            is_error=is_error,
        )
        self._call(lambda sink: sink.end_tool(event))

    def result(
        self,
        *,
        is_error: bool,
        usage: Mapping[str, Any] | None = None,
        cost: float | None = None,
        output: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not self._started or self.closed:
            return
        self._abandon_open_tools()
        result_metadata = {
            **dict(metadata or {}),
            **self._lifecycle_metadata(is_error=is_error),
        }
        reasoning = "\n".join(self._reasoning_parts) if self.capture.reasoning and self._reasoning_parts else None
        captured_output = None
        if self.capture.assistant_output:
            captured_output = output or "".join(self._output_parts) or None
        event = TraceResult(
            metadata=result_metadata,
            usage=normalize_usage(usage),
            cost=cost,
            output=captured_output,
            reasoning=reasoning,
            is_error=is_error,
        )
        self._call(lambda sink: sink.record_result(event))
        self.closed = True
        close_event = TraceClose(
            level="error" if is_error else "default",
            status_message="agent_result",
        )
        self._call(lambda sink: sink.close(close_event))

    def close(
        self,
        *,
        is_error: bool = False,
        status_message: str = "stream_ended_without_result",
    ) -> None:
        if self.closed:
            return
        self._abandon_open_tools()
        self.closed = True
        if self._started:
            event = TraceClose(
                level="error" if is_error else "warning",
                status_message=status_message,
                metadata=self._lifecycle_metadata(is_error=is_error),
            )
            self._call(lambda sink: sink.close(event))

    def _ensure_started(self) -> bool:
        if self._started:
            return True
        if self.sink is None:
            return False
        captured_input: dict[str, Any] = {}
        if self.capture.prompt and self.prompt is not None:
            captured_input["prompt"] = self.prompt
        if self.capture.system_prompt and self.system_prompt is not None:
            captured_input["system_prompt"] = self.system_prompt
        event = TraceStart(
            name=self.name,
            model=self.model,
            input=captured_input or None,
            metadata=self._lifecycle_metadata(),
        )
        with suppress(Exception):
            self.sink.start(event)
            self._started = True
            return True
        self.error_count += 1
        self._unavailable = True
        return False

    def _lifecycle_metadata(self, *, is_error: bool = False) -> dict[str, Any]:
        return {
            **self.metadata,
            "capture_prompt": self.capture.prompt,
            "capture_system_prompt": self.capture.system_prompt,
            "capture_system_message_data": self.capture.system_message_data,
            "capture_assistant_output": self.capture.assistant_output,
            "capture_reasoning": self.capture.reasoning,
            "capture_tool_io": self.capture.tool_io,
            "session_id": self._session_id,
            "num_messages": self._num_messages,
            "num_tool_uses": self._num_tool_uses,
            "is_error": is_error,
        }

    def _abandon_open_tools(self) -> None:
        for tool_id in tuple(self._open_tools):
            self._call(partial(_abandon_tool, tool_id))
        self._open_tools.clear()

    def _call(self, operation: Callable[[TraceSink], Any]) -> bool:
        sink = self.sink
        if sink is None:
            return False
        with suppress(Exception):
            operation(sink)
            return True
        self.error_count += 1
        return False


def _abandon_tool(tool_id: str, sink: TraceSink) -> None:
    sink.abandon_tool(tool_id)


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    details: dict[str, int] = {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        details["input"] = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        details["output"] = output_tokens
    if "input" in details and "output" in details:
        details["total"] = details["input"] + details["output"]
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            details[key] = value
    return details
