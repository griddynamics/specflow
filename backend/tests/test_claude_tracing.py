from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from llm.tracing import ClaudeTraceObserver


class RecordingSession:
    def __init__(self, *, fail_message: bool = False) -> None:
        self.enabled = True
        self.closed = False
        self.error_count = 0
        self.fail_message = fail_message
        self.calls: list[tuple[str, Any]] = []

    def message(self, **kwargs: Any) -> None:
        if self.fail_message:
            raise RuntimeError("mapping failed")
        self.calls.append(("message", kwargs))

    def system_message(self, data: Any) -> None:
        self.calls.append(("system_message", data))

    def assistant_text(self, text: str) -> None:
        self.calls.append(("assistant_text", text))

    def reasoning(self, text: str) -> None:
        self.calls.append(("reasoning", text))

    def tool_started(self, **kwargs: Any) -> None:
        self.calls.append(("tool_started", kwargs))

    def tool_finished(self, **kwargs: Any) -> None:
        self.calls.append(("tool_finished", kwargs))

    def result(self, **kwargs: Any) -> None:
        self.calls.append(("result", kwargs))

    def close(self, **kwargs: Any) -> None:
        self.closed = True
        self.calls.append(("close", kwargs))

    def record_failure(self) -> None:
        self.error_count += 1


def test_maps_claude_messages_and_blocks_to_generic_session() -> None:
    session = RecordingSession()
    observer = ClaudeTraceObserver(session)

    observer(SystemMessage(subtype="init", data={"session_id": "session-1"}))
    observer(
        AssistantMessage(
            model="model-1",
            content=[
                TextBlock(text="answer"),
                ThinkingBlock(thinking="reasoning", signature="signature"),
                ToolUseBlock(id="tool-1", name="Read", input={"path": "/work/a"}),
                ServerToolUseBlock(
                    id="tool-2",
                    name="web_search",
                    input={"query": "example"},
                ),
            ],
        )
    )
    observer(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-1",
                    content="contents",
                    is_error=True,
                )
            ]
        )
    )
    observer(
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=2,
            session_id="session-1",
            stop_reason="end_turn",
            total_cost_usd=0.01,
            usage={"input_tokens": 3, "output_tokens": 5},
            result="final",
        )
    )

    assert session.calls[0] == (
        "message",
        {"session_id": "session-1", "model": None},
    )
    assert ("assistant_text", "answer") in session.calls
    assert ("reasoning", "reasoning") in session.calls
    tool_calls = [value for name, value in session.calls if name == "tool_started"]
    assert tool_calls[0]["is_server"] is False
    assert tool_calls[1]["is_server"] is True
    assert (
        "tool_finished",
        {"tool_id": "tool-1", "output": "contents", "is_error": True},
    ) in session.calls
    result = next(value for name, value in session.calls if name == "result")
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 5}
    assert result["metadata"] == {
        "subtype": "success",
        "num_turns": 2,
        "duration_ms": 100,
        "duration_api_ms": 80,
        "stop_reason": "end_turn",
    }


def test_mapping_failures_do_not_escape_observer() -> None:
    session = RecordingSession(fail_message=True)
    observer = ClaudeTraceObserver(session)

    observer(SystemMessage(subtype="init", data={}))

    assert observer.error_count == 1


def test_close_and_state_delegate_to_session() -> None:
    session = RecordingSession()
    observer = ClaudeTraceObserver(session)

    observer.close(is_error=True, status_message="transport_error")

    assert observer.enabled is True
    assert observer.closed is True
    assert session.calls == [
        (
            "close",
            {"is_error": True, "status_message": "transport_error"},
        )
    ]
