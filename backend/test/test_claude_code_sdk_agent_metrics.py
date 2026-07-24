"""Unit tests for ClaudeCodeSdkAgentMetrics stream aggregation.

Covers all possible UserMessage / AssistantMessage content block shapes
(TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock) plus SDK message
types (ResultMessage, SystemMessage, StreamEvent) and error resilience.
"""

from unittest.mock import Mock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from app.services.agent_metrics import (
    ClaudeCodeSdkAgentMetrics,
    tool_usage_telemetry_props,
)
from app.services.telemetry import PostHogTelemetry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assistant(content, *, parent=None, model="m", error=None):
    return AssistantMessage(content=content, model=model, parent_tool_use_id=parent, error=error)


def _user(content, *, parent=None):
    return UserMessage(content=content, parent_tool_use_id=parent)


def _tool_use(id, name, input=None):
    return ToolUseBlock(id=id, name=name, input=input or {})


def _tool_result(tool_use_id, content=None, *, is_error=None):
    return ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=is_error)


def _empty_buckets():
    return {
        "main_agent_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
        "main_agent_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
        "subagents_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
        "subagents_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
    }


# ---------------------------------------------------------------------------
# AssistantMessage content block variants
# ---------------------------------------------------------------------------

class TestAssistantMessageSchemaVariants:
    """All possible content block types in AssistantMessage."""

    def test_empty_content_list(self):
        """AssistantMessage with empty content list — no crash, nothing registered."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([]))
        assert m.get_metrics() == _empty_buckets()

    def test_text_block_only(self):
        """TextBlock in AssistantMessage content is silently ignored."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([TextBlock(text="Hello, world!")]))
        assert m.get_metrics() == _empty_buckets()

    def test_thinking_block_only(self):
        """ThinkingBlock in AssistantMessage content is silently ignored."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([ThinkingBlock(thinking="Let me reason...", signature="sig")]))
        assert m.get_metrics() == _empty_buckets()

    def test_tool_use_block_registered(self):
        """ToolUseBlock in AssistantMessage is registered for later pairing."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read")]))
        m.push(_user([_tool_result("t1", "file content")]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Read", "count": 1, "tokens": None}]

    def test_mixed_blocks_text_thinking_tool_use(self):
        """Mixed TextBlock + ThinkingBlock + ToolUseBlock — only ToolUseBlock registered."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([
            TextBlock(text="I will read the file."),
            ThinkingBlock(thinking="Need to check path", signature="sig"),
            _tool_use("t1", "Read"),
        ]))
        m.push(_user([_tool_result("t1", "content")]))
        out = m.get_metrics()
        assert out["main_agent_tool_usage"]["tools_usage_count"] == [
            {"name": "Read", "count": 1, "tokens": None}
        ]

    def test_multiple_tool_use_blocks_in_one_message(self):
        """Multiple ToolUseBlocks in a single AssistantMessage all get registered."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([
            _tool_use("t1", "Read"),
            _tool_use("t2", "Bash"),
            _tool_use("t3", "Write"),
        ]))
        for tid in ("t1", "t2", "t3"):
            m.push(_user([_tool_result(tid, "ok")]))
        main = m.get_metrics()["main_agent_tool_usage"]
        names = {e["name"] for e in main["tools_usage_count"]}
        assert names == {"Bash", "Read", "Write"}
        assert all(e["count"] == 1 for e in main["tools_usage_count"])

    def test_error_field_rate_limit_no_crash(self):
        """AssistantMessage with error='rate_limit' and empty content — no crash."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([], error="rate_limit"))
        assert m.get_metrics() == _empty_buckets()

    def test_error_field_all_variants(self):
        """All AssistantMessageError literal values produce no crash."""
        errors = [
            "authentication_failed", "billing_error", "rate_limit",
            "invalid_request", "server_error", "unknown",
        ]
        for err in errors:
            m = ClaudeCodeSdkAgentMetrics()
            m.push(_assistant([], error=err))
            assert m.get_metrics() == _empty_buckets(), f"failed for error={err}"

    def test_tool_result_block_in_assistant_content_ignored(self):
        """ToolResultBlock in AssistantMessage content is ignored (not a ToolUseBlock)."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_result("weird", "content")]))
        assert m.get_metrics() == _empty_buckets()

    def test_subagent_assistant_message_parent_set(self):
        """AssistantMessage with parent_tool_use_id routes tool to sub_* bucket."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("task1", "Task")]))
        m.push(_assistant([_tool_use("bash1", "Bash")], parent="task1"))
        m.push(_user([_tool_result("bash1", "done")], parent="task1"))
        m.push(_user([_tool_result("task1", "ok")]))
        out = m.get_metrics()
        assert out["subagents_tool_usage"]["tools_usage_count"] == [
            {"name": "Bash", "count": 1, "tokens": None}
        ]
        assert out["main_agent_tool_usage"]["tools_usage_count"] == [
            {"name": "Task", "count": 1, "tokens": None}
        ]


# ---------------------------------------------------------------------------
# UserMessage content variants
# ---------------------------------------------------------------------------

class TestUserMessageSchemaVariants:
    """All possible content shapes in UserMessage."""

    def test_content_is_string(self):
        """UserMessage with str content (initial prompt) — no crash, nothing counted."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user("Please generate code for me."))
        assert m.get_metrics() == _empty_buckets()

    def test_content_is_empty_list(self):
        """UserMessage with empty list content — no crash."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user([]))
        assert m.get_metrics() == _empty_buckets()

    def test_content_list_of_text_blocks(self):
        """UserMessage containing only TextBlocks — silently ignored."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user([TextBlock(text="Some context"), TextBlock(text="More context")]))
        assert m.get_metrics() == _empty_buckets()

    def test_content_list_of_thinking_blocks(self):
        """UserMessage containing only ThinkingBlocks — silently ignored."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user([ThinkingBlock(thinking="thoughts", signature="sig")]))
        assert m.get_metrics() == _empty_buckets()

    def test_content_list_of_tool_use_blocks(self):
        """UserMessage containing ToolUseBlocks (unusual) — not ToolResultBlock, ignored."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user([_tool_use("t1", "Read")]))
        assert m.get_metrics() == _empty_buckets()

    def test_content_mixed_text_and_tool_result(self):
        """UserMessage with TextBlock + ToolResultBlock — only ToolResultBlock counted."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash")]))
        m.push(_user([
            TextBlock(text="Some surrounding text"),
            _tool_result("t1", "command output"),
        ]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Bash", "count": 1, "tokens": None}]

    def test_tool_result_content_none(self):
        """ToolResultBlock with content=None — counted, tokens=None."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read")]))
        m.push(_user([_tool_result("t1", None)]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Read", "count": 1, "tokens": None}]
        assert main["tools_tokens"] is None

    def test_tool_result_is_error_true(self):
        """ToolResultBlock with is_error=True is still counted (error tools consume tokens)."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash")]))
        m.push(_user([_tool_result("t1", "command not found", is_error=True)]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Bash", "count": 1, "tokens": None}]

    def test_tool_result_is_error_false(self):
        """ToolResultBlock with is_error=False is counted normally."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Write")]))
        m.push(_user([_tool_result("t1", "written", is_error=False)]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Write", "count": 1, "tokens": None}]

    def test_tool_result_content_list_of_dicts_with_token_key(self):
        """ToolResultBlock content is list[dict] containing totalTokens — parsed."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read")]))
        m.push(_user([_tool_result("t1", [{"totalTokens": 512, "text": "file content"}])]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"][0]["tokens"] == 512

    def test_tool_result_content_list_of_dicts_with_usage_nested(self):
        """ToolResultBlock content is list[dict] with nested usage.total_tokens."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash")]))
        m.push(_user([_tool_result("t1", [{"usage": {"total_tokens": 200}}])]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"][0]["tokens"] == 200

    def test_tool_result_content_list_no_tokens(self):
        """ToolResultBlock content is list[dict] with no token info — tokens=None."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Glob")]))
        m.push(_user([_tool_result("t1", [{"type": "text", "text": "file.py"}])]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"][0]["tokens"] is None

    def test_multiple_tool_results_in_one_user_message(self):
        """Multiple ToolResultBlocks in one UserMessage all get counted."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read"), _tool_use("t2", "Bash")]))
        m.push(_user([_tool_result("t1", "content"), _tool_result("t2", "output")]))
        main = m.get_metrics()["main_agent_tool_usage"]
        names = {e["name"] for e in main["tools_usage_count"]}
        assert names == {"Bash", "Read"}

    def test_parent_tool_use_id_on_user_message_ignored(self):
        """parent_tool_use_id on UserMessage doesn't affect bucket classification.

        Bucket is determined by the parent of the AssistantMessage that emitted
        the ToolUseBlock, not by the parent on the UserMessage.
        """
        m = ClaudeCodeSdkAgentMetrics()
        # Main agent emits Read (parent=None on AssistantMessage)
        m.push(_assistant([_tool_use("t1", "Read")], parent=None))
        # UserMessage carries parent="some_task" — should not change bucket
        m.push(_user([_tool_result("t1", "ok")], parent="some_task"))
        out = m.get_metrics()
        # Read was emitted by main agent, must land in main_tools
        assert out["main_agent_tool_usage"]["tools_usage_count"] == [
            {"name": "Read", "count": 1, "tokens": None}
        ]
        assert out["subagents_tool_usage"]["tools_usage_count"] == []


# ---------------------------------------------------------------------------
# Other SDK message types
# ---------------------------------------------------------------------------

class TestOtherMessageTypes:
    """ResultMessage, SystemMessage, StreamEvent are silently skipped."""

    def test_result_message_ignored(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(ResultMessage(
            subtype="success",
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="s1",
            total_cost_usd=0.01,
        ))
        assert m.get_metrics() == _empty_buckets()

    def test_system_message_ignored(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(SystemMessage(subtype="init", data={"version": "1.0"}))
        assert m.get_metrics() == _empty_buckets()

    def test_stream_event_ignored(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(StreamEvent(
            uuid="u1",
            session_id="s1",
            event={"type": "content_block_delta"},
            parent_tool_use_id=None,
        ))
        assert m.get_metrics() == _empty_buckets()

    def test_unknown_message_type_no_crash(self):
        """Completely unknown message type logs debug and doesn't crash."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push("a plain string")
        m.push(42)
        m.push(None)
        assert m.get_metrics() == _empty_buckets()


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------

class TestErrorResilience:
    """push() and get_metrics() failures are logged as warnings, not raised."""

    def test_push_exception_becomes_warning(self, caplog):
        """If push() raises internally, it is caught and logged as warning."""
        import logging

        m = ClaudeCodeSdkAgentMetrics()

        # Inject a broken message that will cause AttributeError inside _push_assistant
        broken = AssistantMessage(
            content=None,  # type: ignore[arg-type]  # forces crash inside for loop
            model="m",
            parent_tool_use_id=None,
        )
        with caplog.at_level(logging.WARNING, logger="app.services.agent_metrics"):
            m.push(broken)

        assert any("failed to process message" in r.message for r in caplog.records)
        # State unchanged — metrics still return cleanly
        assert m.get_metrics() == _empty_buckets()

    def test_get_metrics_still_works_after_bad_push(self):
        """After a failed push, get_metrics() still returns a valid (empty) dict."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(AssistantMessage(content=None, model="m", parent_tool_use_id=None))  # type: ignore[arg-type]
        result = m.get_metrics()
        # Should return either full empty buckets or empty dict (on internal error)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Pending (unmatched) registry flush
# ---------------------------------------------------------------------------

class TestFlushPendingRegistry:
    """get_metrics() counts ToolUseBlocks that never received a ToolResultBlock."""

    def test_pending_main_tool_counted(self):
        """Stream ends after ToolUseBlock with no result — still counted once, tokens=None."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read")]))
        # No matching UserMessage with ToolResultBlock

        metrics = m.get_metrics()
        tools = metrics["main_agent_tool_usage"]["tools_usage_count"]
        assert len(tools) == 1
        assert tools[0]["name"] == "Read"
        assert tools[0]["count"] == 1
        assert tools[0]["tokens"] is None

    def test_pending_mcp_tool_counted(self):
        """Unmatched MCP tool use lands in main_mcp bucket."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "mcp__playwright__screenshot")]))

        metrics = m.get_metrics()
        mcp = metrics["main_agent_mcp_usage"]["tools_usage_count"]
        assert len(mcp) == 1
        assert mcp[0]["name"] == "mcp__playwright__screenshot"
        assert mcp[0]["count"] == 1

    def test_pending_subagent_tool_counted(self):
        """Unmatched tool inside a subagent context lands in sub_tools bucket."""
        m = ClaudeCodeSdkAgentMetrics()
        # Register a Task tool so we have a parent_tool_use_id
        m.push(_assistant([_tool_use("task1", "Task")]))
        # Subagent assistant message references task1 as parent
        m.push(AssistantMessage(
            content=[_tool_use("t2", "Write")],
            model="m",
            parent_tool_use_id="task1",
        ))
        # Neither Task nor Write gets a result

        metrics = m.get_metrics()
        main_tools = metrics["main_agent_tool_usage"]["tools_usage_count"]
        sub_tools = metrics["subagents_tool_usage"]["tools_usage_count"]

        assert any(t["name"] == "Task" for t in main_tools)
        assert any(t["name"] == "Write" for t in sub_tools)

    def test_flush_then_completed_tool_accumulates(self):
        """A completed call + a pending call for the same tool name both contribute."""
        m = ClaudeCodeSdkAgentMetrics()
        # First call completes normally
        m.push(_assistant([_tool_use("t1", "Read")]))
        m.push(_user([_tool_result("t1", [{"totalTokens": 50}])]))
        # Second call never gets a result
        m.push(_assistant([_tool_use("t2", "Read")]))

        metrics = m.get_metrics()
        tools = metrics["main_agent_tool_usage"]["tools_usage_count"]
        read = next(t for t in tools if t["name"] == "Read")
        assert read["count"] == 2
        assert read["tokens"] == 50  # only the completed call had tokens

    def test_registry_cleared_after_get_metrics(self):
        """Calling get_metrics() twice does not double-count the pending entries."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Glob")]))

        first = m.get_metrics()
        second = m.get_metrics()

        tools_first = first["main_agent_tool_usage"]["tools_usage_count"]
        tools_second = second["main_agent_tool_usage"]["tools_usage_count"]
        assert tools_first[0]["count"] == 1
        assert tools_second[0]["count"] == 1  # same entry, not doubled


# ---------------------------------------------------------------------------
# Core scenarios (original tests, preserved)
# ---------------------------------------------------------------------------

class TestClaudeCodeSdkAgentMetrics:
    """Scenario: replay SDK-shaped messages and assert bucket splits."""

    def test_main_agent_read_with_tokens(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t_read", "Read", {"file_path": "x"})]))
        m.push(_user([_tool_result("t_read", "ok")], parent=None))
        out = m.get_metrics()
        main = out["main_agent_tool_usage"]
        assert main["tools_usage_count"] == [{"name": "Read", "count": 1, "tokens": None}]
        assert main["tools_tokens"] is None

    def test_main_agent_read_tokens_from_string_result_is_none(self):
        """String tool result content has no structured token field — tokens=None."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Read")]))
        m.push(_user([_tool_result("t1", "file content")]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"][0]["tokens"] is None
        assert main["tools_tokens"] is None

    def test_mcp_prefix_main_bucket(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("m1", "mcp__Figma__get")]))
        m.push(_user([_tool_result("m1", "{}")]))
        out = m.get_metrics()
        assert out["main_agent_tool_usage"]["tools_usage_count"] == []
        assert out["main_agent_mcp_usage"]["tools_usage_count"] == [
            {"name": "mcp__Figma__get", "count": 1, "tokens": None}
        ]

    def test_mcp_tool_result_json_string_sums_usage_tokens(self):
        """MCP tools may return JSON strings with usage.input_tokens / output_tokens (no total_tokens)."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("m1", "mcp__acme__call")]))
        payload = '{"usage":{"input_tokens":42,"output_tokens":58}}'
        m.push(_user([_tool_result("m1", payload)]))
        out = m.get_metrics()
        mcp = out["main_agent_mcp_usage"]
        assert mcp["tools_usage_count"][0]["tokens"] == 100
        assert mcp["tools_tokens"] == 100

    def test_subagent_mcp_json_string_tokens_in_sub_bucket(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("task1", "Task")]))
        m.push(_assistant([_tool_use("m1", "mcp__x__y")], parent="task1"))
        sub_payload = '{"usage":{"input_tokens":5,"output_tokens":7}}'
        m.push(_user([_tool_result("m1", sub_payload)]))
        m.push(_user([_tool_result("task1", "ok")]))
        out = m.get_metrics()
        sub = out["subagents_mcp_usage"]
        assert sub["tools_usage_count"][0]["tokens"] == 12
        assert sub["tools_tokens"] == 12

    def test_mcp_prefix_subagent_bucket(self):
        """MCP tool called by a subagent lands in sub_mcp, not main_mcp."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("task1", "Task")]))
        m.push(_assistant([_tool_use("mcp1", "mcp__playwright__click")], parent="task1"))
        m.push(_user([_tool_result("mcp1", "clicked")], parent="task1"))
        m.push(_user([_tool_result("task1", "done")]))
        out = m.get_metrics()
        assert out["main_agent_mcp_usage"]["tools_usage_count"] == []
        assert out["subagents_mcp_usage"]["tools_usage_count"] == [
            {"name": "mcp__playwright__click", "count": 1, "tokens": None}
        ]

    def test_subagent_bash_under_task(self):
        task_id = "t_task"
        bash_id = "t_bash"
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use(task_id, "Task", {"subagent_type": "Bash"})]))
        m.push(_assistant([_tool_use(bash_id, "Bash", {"command": "ls"})], parent=task_id))
        m.push(_user([_tool_result(bash_id, "out", is_error=False)], parent=task_id))
        m.push(_user([_tool_result(task_id, [{"type": "text", "text": "done"}])]))
        out = m.get_metrics()
        assert out["main_agent_tool_usage"]["tools_usage_count"] == [
            {"name": "Task", "count": 1, "tokens": None}
        ]
        assert out["subagents_tool_usage"]["tools_usage_count"] == [
            {"name": "Bash", "count": 1, "tokens": None}
        ]

    def test_unknown_tool_result_id_no_crash(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_user([_tool_result("missing", "x")]))
        for k in (
            "main_agent_tool_usage",
            "main_agent_mcp_usage",
            "subagents_tool_usage",
            "subagents_mcp_usage",
        ):
            assert m.get_metrics()[k]["tools_usage_count"] == []

    def test_same_tool_called_multiple_times_count_accumulates(self):
        """Repeated calls to the same tool increment count, not create new entries."""
        m = ClaudeCodeSdkAgentMetrics()
        for i in range(3):
            tid = f"t{i}"
            m.push(_assistant([_tool_use(tid, "Read")]))
            m.push(_user([_tool_result(tid, "content")]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert len(main["tools_usage_count"]) == 1
        assert main["tools_usage_count"][0] == {"name": "Read", "count": 3, "tokens": None}

    def test_token_accumulation_across_calls(self):
        """Tokens from multiple calls to same tool are summed."""
        m = ClaudeCodeSdkAgentMetrics()
        for i, tokens in enumerate([100, 200, 300]):
            tid = f"t{i}"
            m.push(_assistant([_tool_use(tid, "Read")]))
            m.push(_user([_tool_result(tid, [{"totalTokens": tokens}])]))
        main = m.get_metrics()["main_agent_tool_usage"]
        assert main["tools_usage_count"][0]["tokens"] == 600
        assert main["tools_tokens"] == 600

    def test_tool_usage_telemetry_props(self):
        breakdown = {
            "main_agent_tool_usage": {
                "tools_usage_count": [{"name": "Read", "count": 2, "tokens": 100}],
                "tools_tokens": 100,
            },
            "main_agent_mcp_usage": {
                "tools_usage_count": [],
                "tools_tokens": None,
            },
            "subagents_tool_usage": {
                "tools_usage_count": [{"name": "Bash", "count": 1, "tokens": None}],
                "tools_tokens": None,
            },
            "subagents_mcp_usage": {
                "tools_usage_count": [{"name": "mcp__X__y", "count": 3, "tokens": 30}],
                "tools_tokens": 30,
            },
        }
        props = tool_usage_telemetry_props(breakdown)
        assert props["main_agent_tool_tokens"] == 100
        assert props["main_agent_mcp_tokens"] == 0
        assert props["subagents_tool_tokens"] == 0
        assert props["subagents_mcp_tokens"] == 30
        assert '"name":"Read","count":2' in props["main_agent_tool_usage_json"]

    def test_telemetry_props_no_json_key_when_no_tools(self):
        """Buckets with no tools don't emit a *_json key (sparse representation)."""
        breakdown = {
            "main_agent_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
            "main_agent_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
            "subagents_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
            "subagents_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
        }
        props = tool_usage_telemetry_props(breakdown)
        assert "main_agent_tool_usage_json" not in props
        assert "main_agent_mcp_usage_json" not in props
        assert "subagents_tool_usage_json" not in props
        assert "subagents_mcp_usage_json" not in props
        # Token keys are always present, defaulting to 0
        assert props["main_agent_tool_tokens"] == 0


# ---------------------------------------------------------------------------
# "Agent" tool name compat (older SDK versions use "Agent" instead of "Task")
# ---------------------------------------------------------------------------

class TestAgentToolNameCompat:
    """Verify "Agent" is treated identically to "Task" as a subagent-spawning tool."""

    def test_agent_tool_routes_to_main_bucket(self):
        """Tool named "Agent" lands in main_tools, inner tool lands in sub_tools."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("agent1", "Agent")]))
        m.push(_assistant([_tool_use("bash1", "Bash")], parent="agent1"))
        m.push(_user([_tool_result("bash1", "done")]))
        m.push(_user([_tool_result("agent1", "ok")]))
        out = m.get_metrics()
        assert out["main_agent_tool_usage"]["tools_usage_count"] == [
            {"name": "Agent", "count": 1, "tokens": None}
        ]
        assert out["subagents_tool_usage"]["tools_usage_count"] == [
            {"name": "Bash", "count": 1, "tokens": None}
        ]

    def test_agent_tool_inner_results_not_double_counted(self):
        """Inner tool result under an "Agent" call is not also counted in main_tools."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("agent1", "Agent")]))
        m.push(_assistant([_tool_use("read1", "Read"), _tool_use("write1", "Write")], parent="agent1"))
        m.push(_user([_tool_result("read1", "content")]))
        m.push(_user([_tool_result("write1", "ok")]))
        m.push(_user([_tool_result("agent1", "done")]))
        out = m.get_metrics()
        main_names = {e["name"] for e in out["main_agent_tool_usage"]["tools_usage_count"]}
        assert main_names == {"Agent"}
        sub_names = {e["name"] for e in out["subagents_tool_usage"]["tools_usage_count"]}
        assert sub_names == {"Read", "Write"}

    def test_agent_tool_mcp_inner_routes_to_sub_mcp(self):
        """MCP tool called under an "Agent" subagent lands in sub_mcp."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("agent1", "Agent")]))
        m.push(_assistant([_tool_use("mcp1", "mcp__playwright__click")], parent="agent1"))
        m.push(_user([_tool_result("mcp1", "clicked")]))
        m.push(_user([_tool_result("agent1", "done")]))
        out = m.get_metrics()
        assert out["main_agent_mcp_usage"]["tools_usage_count"] == []
        assert out["subagents_mcp_usage"]["tools_usage_count"] == [
            {"name": "mcp__playwright__click", "count": 1, "tokens": None}
        ]

    def test_pending_agent_tool_flushed_on_get_metrics(self):
        """Unmatched "Agent" tool use is flushed into main_tools with tokens=None."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("agent1", "Agent")]))
        m.push(_assistant([_tool_use("bash1", "Bash")], parent="agent1"))
        # Neither receives a result — stream interrupted
        out = m.get_metrics()
        main_names = {e["name"] for e in out["main_agent_tool_usage"]["tools_usage_count"]}
        sub_names = {e["name"] for e in out["subagents_tool_usage"]["tools_usage_count"]}
        assert "Agent" in main_names
        assert "Bash" in sub_names

    def test_agent_tool_structured_token_field(self):
        """Token data in a structured field on the "Agent" result is captured."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("agent1", "Agent")]))
        m.push(_user([_tool_result("agent1", [{"totalTokens": 512}])]))
        out = m.get_metrics()
        entry = out["main_agent_tool_usage"]["tools_usage_count"][0]
        assert entry["name"] == "Agent"
        assert entry["tokens"] == 512


# ---------------------------------------------------------------------------
# stream_metrics=None path in process_query_stream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_query_stream_with_none_metrics():
    """process_query_stream with stream_metrics=None skips metric collection."""
    from app.services.claude_code import process_query_stream
    import logging

    async def _fake_stream():
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="s",
        )

    logger = logging.getLogger("test")
    messages = await process_query_stream(_fake_stream(), logger, stream_metrics=None)
    assert len(messages) == 1
    assert messages[0].session_id == "s"


@pytest.mark.asyncio
async def test_process_query_stream_with_metrics_collects_tools():
    """process_query_stream with stream_metrics set calls push() for each message."""
    from app.services.claude_code import process_query_stream
    import logging

    async def _fake_stream():
        yield _assistant([_tool_use("t1", "Read")])
        yield _user([_tool_result("t1", "data")])
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="s2",
        )

    stream_metrics = ClaudeCodeSdkAgentMetrics()
    logger = logging.getLogger("test")
    await process_query_stream(_fake_stream(), logger, stream_metrics)
    out = stream_metrics.get_metrics()
    assert out["main_agent_tool_usage"]["tools_usage_count"] == [
        {"name": "Read", "count": 1, "tokens": None}
    ]


# ---------------------------------------------------------------------------
# PostHog integration
# ---------------------------------------------------------------------------

@patch("app.services.telemetry.TelemetryContext")
def test_capture_agent_query_event_merges_tool_breakdown(mock_ctx: Mock) -> None:
    """Scenario: PostHog $ai_generation includes flattened tool usage props."""
    mock_ctx.get_user_email.return_value = "user@test.com"
    mock_ctx.get_workspace_name.return_value = None
    mock_ctx.get_session_id.return_value = None
    mock_ctx.get_generation_id.return_value = None
    mock_ctx.get_workflow.return_value = None
    mock_ctx.get_mcp_props.return_value = {}

    t = PostHogTelemetry.__new__(PostHogTelemetry)
    t._client = Mock()
    t._initialized = True

    breakdown = {
        "main_agent_tool_usage": {
            "tools_usage_count": [{"name": "Read", "count": 1, "tokens": None}],
            "tools_tokens": None,
        },
        "main_agent_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
        "subagents_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
        "subagents_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
    }
    t.capture_agent_query_event(
        "anthropic/claude-haiku-4.5",
        "anthropic",
        "/workspaces/ws-01",
        tool_usage_breakdown=breakdown,
    )
    call_kwargs = t._client.capture.call_args.kwargs
    assert call_kwargs["event"] == "$ai_generation"
    props = call_kwargs["properties"]
    assert props["main_agent_tool_tokens"] == 0
    assert props["duration_seconds"] == 0
    assert '"name":"Read","count":1' in props["main_agent_tool_usage_json"]


@patch("app.services.telemetry.TelemetryContext")
def test_capture_agent_query_event_none_breakdown_no_tool_props(mock_ctx: Mock) -> None:
    """With tool_usage_breakdown=None, no tool props are added to PostHog event."""
    mock_ctx.get_user_email.return_value = "user@test.com"
    mock_ctx.get_workspace_name.return_value = None
    mock_ctx.get_session_id.return_value = None
    mock_ctx.get_generation_id.return_value = None
    mock_ctx.get_workflow.return_value = None
    mock_ctx.get_mcp_props.return_value = {}

    t = PostHogTelemetry.__new__(PostHogTelemetry)
    t._client = Mock()
    t._initialized = True

    t.capture_agent_query_event(
        "anthropic/claude-haiku-4.5",
        "anthropic",
        "/workspaces/ws-01",
        tool_usage_breakdown=None,
    )
    props = t._client.capture.call_args.kwargs["properties"]
    assert "main_agent_tool_tokens" not in props
    assert "main_agent_tool_usage_json" not in props


# ---------------------------------------------------------------------------
# progress_snapshot (resume-loop progress signal)
# ---------------------------------------------------------------------------

class TestProgressSnapshot:
    def test_empty_stream_reports_zero(self):
        m = ClaudeCodeSdkAgentMetrics()
        snap = m.progress_snapshot()
        assert snap.num_messages == 0
        assert snap.num_tool_uses == 0

    def test_counts_messages_and_matched_tool_uses(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash")]))
        m.push(_user([_tool_result("t1")]))
        snap = m.progress_snapshot()
        assert snap.num_messages == 2
        assert snap.num_tool_uses == 1

    def test_counts_pending_unmatched_tool_uses(self):
        """A crash mid-tool (no ToolResultBlock yet) is still observed work."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash"), _tool_use("t2", "Edit")]))
        snap = m.progress_snapshot()
        assert snap.num_tool_uses == 2

    def test_snapshot_is_side_effect_free(self):
        """Unlike get_metrics(), snapshot must not flush the pending registry."""
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([_tool_use("t1", "Bash")]))
        m.progress_snapshot()
        m.push(_user([_tool_result("t1")]))
        # The late result still matches: exactly one Bash use counted, not two.
        snap = m.progress_snapshot()
        assert snap.num_tool_uses == 1
        metrics = m.get_metrics()
        bash = metrics["main_agent_tool_usage"]["tools_usage_count"]
        assert bash == [{"name": "Bash", "count": 1, "tokens": None}]

    def test_non_tool_messages_count_as_messages_only(self):
        m = ClaudeCodeSdkAgentMetrics()
        m.push(_assistant([TextBlock(text="thinking about it")]))
        m.push(ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ))
        snap = m.progress_snapshot()
        assert snap.num_messages == 2
        assert snap.num_tool_uses == 0
