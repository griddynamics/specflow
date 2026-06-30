"""Unit tests for agent stream event conversion (services/agent_stream_events.py).

Scenario coverage:
- Each SDK message type maps to the expected compact UI event kind.
- Tool calls expose tool_name (and subagent_name for Task) without dumping input.
- Long previews are capped at MAX_MESSAGE_CHARS.
- Conversion never raises — a broken message yields [] instead of propagating.
"""

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_agent_sdk.types import TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from app.services.agent_stream_events import (
    MAX_MESSAGE_CHARS,
    MAX_TOOL_RESULT_CHARS,
    message_to_ui_events,
)

_GID = "est-abc123"
_WS = "ws-01-1"


def _convert(message):
    return message_to_ui_events(message, generation_id=_GID, workspace_id=_WS, workflow="phase:1:coding")


class TestAssistantMessage:
    def test_text_block_becomes_assistant_text(self):
        msg = AssistantMessage(content=[TextBlock(text="Inspecting the routing layer")], model="m")
        events = _convert(msg)
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "assistant_text"
        assert ev.message == "Inspecting the routing layer"
        assert ev.generation_id == _GID
        assert ev.workspace_id == _WS
        assert ev.workflow == "phase:1:coding"

    def test_empty_text_block_is_skipped(self):
        msg = AssistantMessage(content=[TextBlock(text="   ")], model="m")
        assert _convert(msg) == []

    def test_tool_use_block_sets_tool_name_and_summary(self):
        msg = AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "pytest -q"})],
            model="m",
        )
        events = _convert(msg)
        assert len(events) == 1
        assert events[0].kind == "tool_use"
        assert events[0].tool_name == "Bash"
        assert "pytest -q" in events[0].message
        assert events[0].subagent_name is None

    def test_task_tool_use_sets_subagent_name(self):
        msg = AssistantMessage(
            content=[
                ToolUseBlock(
                    id="t1",
                    name="Task",
                    input={"subagent_type": "engineer", "description": "build api"},
                )
            ],
            model="m",
        )
        events = _convert(msg)
        assert events[0].subagent_name == "engineer"

    def test_multiple_blocks_produce_multiple_events(self):
        msg = AssistantMessage(
            content=[
                TextBlock(text="Now running tests"),
                ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
            ],
            model="m",
        )
        events = _convert(msg)
        assert [e.kind for e in events] == ["assistant_text", "tool_use"]


class TestUserMessage:
    def test_tool_result_becomes_tool_result_event(self):
        msg = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="3 passed")])
        events = _convert(msg)
        assert len(events) == 1
        assert events[0].kind == "tool_result"
        assert "3 passed" in events[0].message

    def test_tool_result_list_content_extracts_text(self):
        msg = UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "done"}])]
        )
        events = _convert(msg)
        assert events[0].message == "done"

    def test_string_content_user_message_yields_nothing(self):
        assert _convert(UserMessage(content="hello")) == []

    def test_tool_result_is_single_line_and_strictly_capped(self):
        # Multi-line dumps must collapse to one line and clamp to the stricter
        # tool-result cap so large/sensitive command output cannot flood the feed.
        noisy = "line one\n" + ("secret-ish " * 100)
        msg = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=noisy)])
        ev = _convert(msg)[0]
        assert ev.kind == "tool_result"
        assert "\n" not in ev.message
        assert len(ev.message) <= MAX_TOOL_RESULT_CHARS
        assert ev.message.startswith("line one secret-ish")


class TestResultAndSystem:
    def test_result_message_becomes_result_event(self):
        msg = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=3,
            session_id="s1",
            result="All phases complete",
        )
        events = _convert(msg)
        assert len(events) == 1
        assert events[0].kind == "result"
        assert events[0].message == "All phases complete"

    def test_system_message_surfaces_subtype_only(self):
        msg = SystemMessage(subtype="init", data={"secret": "do-not-leak"})
        events = _convert(msg)
        assert len(events) == 1
        assert events[0].kind == "system"
        assert events[0].message == "init"
        assert "do-not-leak" not in events[0].message


class TestTruncationAndSafety:
    def test_long_text_is_capped(self):
        long = "x" * 1000
        msg = AssistantMessage(content=[TextBlock(text=long)], model="m")
        events = _convert(msg)
        assert len(events[0].message) <= MAX_MESSAGE_CHARS

    def test_unknown_message_type_yields_nothing(self):
        assert _convert(object()) == []

    def test_conversion_failure_does_not_propagate(self):
        # Force iteration over content to raise; converter must swallow and return [].
        class _Raises:
            def __iter__(self):
                raise RuntimeError("boom")

        bad = AssistantMessage(content=[TextBlock(text="ok")], model="m")
        object.__setattr__(bad, "content", _Raises())
        assert _convert(bad) == []
