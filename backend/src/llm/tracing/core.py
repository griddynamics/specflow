from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

TraceLevel = Literal["default", "warning", "error"]


@dataclass(frozen=True, slots=True)
class TraceCapture:
    """Explicit opt-ins for data that may contain sensitive content."""

    prompt: bool = False
    system_prompt: bool = False
    system_message_data: bool = False
    assistant_output: bool = False
    reasoning: bool = False
    tool_io: bool = False


@dataclass(frozen=True, slots=True)
class TraceStart:
    name: str
    model: str | None
    input: Mapping[str, Any] | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolStart:
    id: str
    name: str
    is_server: bool
    input: Any = None


@dataclass(frozen=True, slots=True)
class ToolEnd:
    id: str
    output: Any = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TraceResult:
    metadata: Mapping[str, Any]
    usage: Mapping[str, int]
    cost: float | None
    output: Any = None
    reasoning: str | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TraceClose:
    level: TraceLevel
    status_message: str
    metadata: Mapping[str, Any] | None = None


class TraceSink(Protocol):
    def start(self, event: TraceStart) -> None: ...

    def update_model(self, model: str) -> None: ...

    def record_system_message(self, data: Mapping[str, Any]) -> None: ...

    def start_tool(self, event: ToolStart) -> None: ...

    def end_tool(self, event: ToolEnd) -> None: ...

    def abandon_tool(self, tool_id: str) -> None: ...

    def record_result(self, event: TraceResult) -> None: ...

    def close(self, event: TraceClose) -> None: ...
