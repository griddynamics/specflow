"""Shared support for the tracing tests (no tests in here).

- ``RecordingSink``: a hand-written ``llm.tracing.TraceSink`` fake used instead of a mocking
  library. It implements every protocol method, records each call as ``(method, value)`` in call
  order, and raises ``RuntimeError`` for the method names it is told to fail. With a ``label``
  the error message is ``"<label>.<method> failed"``, without one it is ``"<method> failed"``.
- ``call_value``: the value of the first recorded call of a method.
"""

from __future__ import annotations

from typing import Any

from llm.tracing.core import ToolEnd, ToolStart, TraceClose, TraceResult, TraceStart


class RecordingSink:
    """Records every ``TraceSink`` call, in order, and raises for the configured method names."""

    def __init__(self, *fail_methods: str, label: str | None = None) -> None:
        self.label = label
        self.fail_methods = set(fail_methods)
        self.calls: list[tuple[str, Any]] = []

    def _record(self, method: str, value: Any = None) -> None:
        self.calls.append((method, value))
        if method in self.fail_methods:
            prefix = "" if self.label is None else f"{self.label}."
            raise RuntimeError(f"{prefix}{method} failed")

    def start(self, event: TraceStart) -> None:
        self._record("start", event)

    def update_model(self, model: str) -> None:
        self._record("update_model", model)

    def record_system_message(self, data: dict[str, Any]) -> None:
        self._record("record_system_message", data)

    def start_tool(self, event: ToolStart) -> None:
        self._record("start_tool", event)

    def end_tool(self, event: ToolEnd) -> None:
        self._record("end_tool", event)

    def abandon_tool(self, tool_id: str) -> None:
        self._record("abandon_tool", tool_id)

    def record_result(self, event: TraceResult) -> None:
        self._record("record_result", event)

    def close(self, event: TraceClose) -> None:
        self._record("close", event)


def call_value(sink: RecordingSink, method: str) -> Any:
    """The value of the first call of ``method`` that ``sink`` recorded."""
    return next(value for name, value in sink.calls if name == method)
