"""Unit tests for backend/app/services/langfuse/ (Langfuse v4 OTel API)."""

from unittest.mock import MagicMock, patch
from contextlib import contextmanager

import pytest

from app.services.langfuse import (
    LangfuseStreamTracer,
    LangfuseTracer,
    _NullTracer,
    tracer as lf_tracer,
)
from app.services.langfuse.utils import _truncate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_text_block(text: str) -> MagicMock:
    from claude_agent_sdk import TextBlock
    block = MagicMock(spec=TextBlock)
    block.text = text
    return block


def _make_tool_use_block(tool_id: str, name: str, input_data: dict) -> MagicMock:
    from claude_agent_sdk.types import ToolUseBlock
    block = MagicMock(spec=ToolUseBlock)
    block.id = tool_id
    block.name = name
    block.input = input_data
    return block


def _make_tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> MagicMock:
    from claude_agent_sdk.types import ToolResultBlock
    block = MagicMock(spec=ToolResultBlock)
    block.tool_use_id = tool_use_id
    block.content = content
    block.is_error = is_error
    return block


def _make_assistant_message(blocks: list, parent_tool_use_id: str = None) -> MagicMock:
    from claude_agent_sdk import AssistantMessage
    msg = MagicMock(spec=AssistantMessage)
    msg.content = blocks
    msg.parent_tool_use_id = parent_tool_use_id
    msg.session_id = None
    return msg


def _make_user_message(blocks: list) -> MagicMock:
    from claude_agent_sdk.types import UserMessage
    msg = MagicMock(spec=UserMessage)
    msg.content = blocks
    msg.session_id = None
    return msg


def _make_result_message(
    output: str = "done",
    is_error: bool = False,
    usage: dict = None,
    total_cost_usd: float = 0.0,
) -> MagicMock:
    from claude_agent_sdk import ResultMessage
    msg = MagicMock(spec=ResultMessage)
    msg.result = output
    msg.is_error = is_error
    msg.usage = usage or {"input_tokens": 100, "output_tokens": 50}
    msg.total_cost_usd = total_cost_usd
    msg.num_turns = 3
    msg.duration_ms = 1000
    msg.duration_api_ms = 800
    msg.stop_reason = "end_turn"
    msg.session_id = None
    return msg


def _complete_default_billing(tracer: LangfuseStreamTracer, cost_usd: float = 0.01) -> None:
    tracer.complete_with_billing(cost_usd)


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

def test_truncate_short_string_unchanged():
    assert _truncate("hello") == "hello"


def test_truncate_long_string():
    long_str = "x" * 20_000
    result = _truncate(long_str)
    assert isinstance(result, str)
    assert len(result) <= 10_020
    assert result.endswith("…[truncated]")


def test_truncate_redact():
    assert _truncate("sensitive data", redact=True) == "[redacted]"


def test_truncate_dict_large():
    big_dict = {"key": "v" * 20_000}
    result = _truncate(big_dict)
    assert isinstance(result, str)
    assert "…[truncated]" in result


# ---------------------------------------------------------------------------
# _NullTracer
# ---------------------------------------------------------------------------

def test_null_tracer_push_noop():
    tracer = _NullTracer()
    tracer.push(MagicMock())


def test_null_tracer_finalize_noop():
    tracer = _NullTracer()
    tracer.finalize_pending()


def test_null_tracer_end_generation_noop():
    tracer = _NullTracer()
    tracer.end_generation_on_error("timeout")


# ---------------------------------------------------------------------------
# make_stream_tracer
# ---------------------------------------------------------------------------

def test_make_stream_tracer_none_returns_null():
    tracer = lf_tracer.make_stream_tracer(None)
    assert isinstance(tracer, _NullTracer)


def test_make_stream_tracer_with_generation_returns_stream_tracer():
    generation = MagicMock()
    tracer = lf_tracer.make_stream_tracer(generation)
    assert isinstance(tracer, LangfuseStreamTracer)
    assert tracer.generation is generation


# ---------------------------------------------------------------------------
# LangfuseStreamTracer: tool_use_id pairing (v4 API: start_observation / update + end)
# ---------------------------------------------------------------------------

def test_stream_tracer_tool_use_pair():
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span

    tracer = LangfuseStreamTracer(generation)

    tool_block = _make_tool_use_block("tool-1", "Read", {"path": "/tmp/foo"})
    assistant_msg = _make_assistant_message([tool_block])
    tracer.push(assistant_msg)

    generation.start_observation.assert_called_once_with(
        name="Read",
        as_type="tool",
        input={"path": "/tmp/foo"},
        metadata={"tool_use_id": "tool-1", "is_subagent": False},
    )
    assert "tool-1" in tracer.open_spans

    result_block = _make_tool_result_block("tool-1", "file content")
    user_msg = _make_user_message([result_block])
    tracer.push(user_msg)

    span.update.assert_called_once_with(output="file content", level="DEFAULT")
    span.end.assert_called_once()
    assert "tool-1" not in tracer.open_spans


def test_stream_tracer_tool_span_captures_preceding_reasoning():
    """Text/thinking BEFORE a tool use in the same AssistantMessage should attach
    as the tool span's `preceding_reasoning` / `preceding_thinking` metadata."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span

    tracer = LangfuseStreamTracer(generation)

    # Build [TextBlock(reasoning), ThinkingBlock, ToolUseBlock] in one message
    from claude_agent_sdk.types import ThinkingBlock
    thinking_block = MagicMock(spec=ThinkingBlock)
    thinking_block.thinking = "I should inspect the schema first"
    msg = _make_assistant_message([
        _make_text_block("Let me read the config file."),
        thinking_block,
        _make_tool_use_block("t1", "Read", {"path": "/cfg.yml"}),
    ])
    tracer.push(msg)

    call_kwargs = generation.start_observation.call_args.kwargs
    md = call_kwargs["metadata"]
    assert md["preceding_reasoning"] == "Let me read the config file."
    assert md["preceding_thinking"] == "I should inspect the schema first"


def test_stream_tracer_reasoning_resets_between_tools_in_same_message():
    """Text between two tool uses in the same message attaches only to the SECOND tool."""
    generation = MagicMock()
    span1 = MagicMock()
    span2 = MagicMock()
    generation.start_observation.side_effect = [span1, span2]

    tracer = LangfuseStreamTracer(generation)

    msg = _make_assistant_message([
        _make_text_block("Reasoning for tool 1."),
        _make_tool_use_block("t1", "Read", {}),
        _make_text_block("Now I need to write."),
        _make_tool_use_block("t2", "Write", {}),
    ])
    tracer.push(msg)

    first_md = generation.start_observation.call_args_list[0].kwargs["metadata"]
    second_md = generation.start_observation.call_args_list[1].kwargs["metadata"]
    assert first_md["preceding_reasoning"] == "Reasoning for tool 1."
    assert second_md["preceding_reasoning"] == "Now I need to write."
    # First tool should NOT contain second tool's reasoning
    assert "Now I need to write" not in first_md["preceding_reasoning"]


def test_stream_tracer_tool_use_error_level():
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span

    tracer = LangfuseStreamTracer(generation)

    tracer.push(_make_assistant_message([_make_tool_use_block("t1", "Bash", {})]))
    tracer.push(_make_user_message([_make_tool_result_block("t1", "err", is_error=True)]))

    call_kwargs = span.update.call_args.kwargs
    assert call_kwargs["level"] == "ERROR"
    span.end.assert_called_once()


def test_stream_tracer_orphan_tool_result_emits_event():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    result_block = _make_tool_result_block("unknown-id", "result")
    user_msg = _make_user_message([result_block])
    tracer.push(user_msg)

    generation.create_event.assert_called_once_with(
        name="orphan_tool_result",
        metadata={"tool_use_id": "unknown-id"},
    )


def test_stream_tracer_result_message_ends_generation():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    text_block = _make_text_block("final answer")
    tracer.push(_make_assistant_message([text_block]))

    result_msg = _make_result_message(
        usage={"input_tokens": 200, "output_tokens": 80},
        total_cost_usd=0.005,
    )
    tracer.push(result_msg)
    tracer.complete_with_billing(0.004)

    generation.update.assert_called_once()
    call_kwargs = generation.update.call_args.kwargs
    assert call_kwargs["output"] == "final answer"
    assert call_kwargs["usage_details"]["input"] == 200
    assert call_kwargs["usage_details"]["output"] == 80
    assert call_kwargs["cost_details"]["total"] == 0.004
    assert call_kwargs["level"] == "DEFAULT"
    generation.end.assert_called_once()
    assert tracer._generation_ended is True


def test_stream_tracer_result_message_captures_full_thinking():
    """ThinkingBlock contents should land in generation metadata.thinking on close."""
    from claude_agent_sdk.types import ThinkingBlock
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    tb = MagicMock(spec=ThinkingBlock)
    tb.thinking = "Step 1: parse the spec. Step 2: choose phases."
    tracer.push(_make_assistant_message([tb]))
    tracer.push(_make_result_message())
    _complete_default_billing(tracer)

    md = generation.update.call_args.kwargs["metadata"]
    assert md["thinking_chars"] == len(tb.thinking)
    assert "Step 1: parse the spec" in md["thinking"]


def test_stream_tracer_result_message_error_level():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    result_msg = _make_result_message(is_error=True)
    tracer.push(result_msg)
    _complete_default_billing(tracer)

    call_kwargs = generation.update.call_args.kwargs
    assert call_kwargs["level"] == "ERROR"


def test_stream_tracer_result_message_idempotent():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    result_msg = _make_result_message()
    tracer.push(result_msg)
    tracer.push(result_msg)  # second push replaces pending only
    _complete_default_billing(tracer)

    assert generation.update.call_count == 1
    assert generation.end.call_count == 1


# ---------------------------------------------------------------------------
# LangfuseStreamTracer: finalize_pending
# ---------------------------------------------------------------------------

def test_finalize_pending_closes_open_spans():
    generation = MagicMock()
    span1 = MagicMock()
    span2 = MagicMock()
    generation.start_observation.side_effect = [span1, span2]

    tracer = LangfuseStreamTracer(generation)

    tracer.push(_make_assistant_message([
        _make_tool_use_block("t1", "Read", {}),
        _make_tool_use_block("t2", "Write", {}),
    ]))

    tracer.finalize_pending()

    span1.update.assert_called_once()
    span1.end.assert_called_once()
    span2.update.assert_called_once()
    span2.end.assert_called_once()
    assert tracer.open_spans == {}


def test_finalize_pending_idempotent():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)
    tracer.finalize_pending()
    tracer.finalize_pending()


def test_finalize_pending_closes_generation_when_billing_never_called():
    """Safety net: if complete_with_billing() is never called, finalize_pending falls back to SDK cost."""
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    tracer.push(_make_result_message(total_cost_usd=0.05))
    # Deliberately do NOT call complete_with_billing()
    tracer.finalize_pending()

    generation.update.assert_called_once()
    call_kwargs = generation.update.call_args.kwargs
    assert call_kwargs["cost_details"]["total"] == 0.05
    generation.end.assert_called_once()
    assert tracer._generation_ended is True


# ---------------------------------------------------------------------------
# LangfuseStreamTracer: end_generation_on_error
# ---------------------------------------------------------------------------

def test_end_generation_on_error():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    tracer.end_generation_on_error("timeout")

    generation.update.assert_called_once()
    call_kwargs = generation.update.call_args.kwargs
    assert call_kwargs["level"] == "ERROR"
    assert call_kwargs["status_message"] == "timeout"
    generation.end.assert_called_once()
    assert tracer._generation_ended is True


def test_end_generation_on_error_idempotent():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    tracer.end_generation_on_error("exception")
    tracer.end_generation_on_error("exception")

    assert generation.update.call_count == 1
    assert generation.end.call_count == 1


def test_end_generation_on_error_no_op_if_result_message_arrived():
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    tracer.push(_make_result_message())
    tracer.end_generation_on_error("exception")  # no-op while result pending
    _complete_default_billing(tracer)

    assert generation.update.call_count == 1


# ---------------------------------------------------------------------------
# LangfuseStreamTracer: subagent nesting
# ---------------------------------------------------------------------------

def test_stream_tracer_subagent_span_nested_under_task():
    generation = MagicMock()
    task_span = MagicMock()
    sub_span = MagicMock()
    generation.start_observation.return_value = task_span
    task_span.start_observation.return_value = sub_span

    tracer = LangfuseStreamTracer(generation)

    # Main agent creates a Task span
    task_block = _make_tool_use_block("task-1", "Task", {"prompt": "..."})
    tracer.push(_make_assistant_message([task_block]))
    assert "task-1" in tracer.open_spans

    # Subagent creates a Read span — parent_tool_use_id points to Task
    read_block = _make_tool_use_block("read-1", "Read", {"path": "/foo"})
    tracer.push(_make_assistant_message([read_block], parent_tool_use_id="task-1"))

    task_span.start_observation.assert_called_once_with(
        name="Read",
        as_type="tool",
        input={"path": "/foo"},
        metadata={"tool_use_id": "read-1", "is_subagent": True},
    )


# ---------------------------------------------------------------------------
# start_workflow_step_trace: disabled mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_workflow_step_trace_disabled_is_noop():
    lf_tracer._client = None

    yielded = []
    async with lf_tracer.start_workflow_step_trace("test_step") as t:
        yielded.append(t)

    assert yielded == [None]


# ---------------------------------------------------------------------------
# start_workflow_step_trace: enabled mode (v4 API: start_as_current_observation + propagate_attributes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("app.services.langfuse.client.TelemetryContext")
async def test_start_workflow_step_trace_enabled(mock_ctx):
    mock_root_obs = MagicMock()
    mock_propagate = MagicMock()
    mock_propagate.return_value.__enter__ = MagicMock(return_value=None)
    mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

    # Build a real async context manager for start_as_current_observation
    @contextmanager
    def _fake_start_as_current(*args, **kwargs):
        yield mock_root_obs

    mock_client = MagicMock()
    mock_client.start_as_current_observation.side_effect = _fake_start_as_current

    mock_ctx.get_user_context.return_value = {
        "generation_id": "est-abc123",
        "user_email": "user@example.com",
        "workspace_ids": ["ws-01-1", "ws-01-2"],
    }
    mock_ctx.get_mcp_props.return_value = {}

    lf_tracer._client = mock_client
    lf_tracer._propagate_attributes_fn = mock_propagate
    try:
        async with lf_tracer.start_workflow_step_trace(
            "kb_init", extra_metadata={"workspace_name": "ws-01-1"}
        ) as obs:
            assert obs is mock_root_obs
    finally:
        lf_tracer._client = None
        lf_tracer._propagate_attributes_fn = None

    mock_client.start_as_current_observation.assert_called_once_with(
        name="kb_init", as_type="span"
    )
    # propagate_attributes should be called with session_id and user_id
    call_kwargs = mock_propagate.call_args.kwargs
    assert call_kwargs["session_id"] == "est-abc123"
    assert call_kwargs["user_id"] == "user@example.com"
    tags = call_kwargs.get("tags") or []
    assert "ws:ws-01-1" in tags


@pytest.mark.asyncio
@patch("app.services.langfuse.client.TelemetryContext")
async def test_start_workflow_step_trace_marks_error_on_exception(mock_ctx):
    mock_root_obs = MagicMock()
    mock_propagate = MagicMock()
    mock_propagate.return_value.__enter__ = MagicMock(return_value=None)
    mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _fake_start_as_current(*args, **kwargs):
        yield mock_root_obs

    mock_client = MagicMock()
    mock_client.start_as_current_observation.side_effect = _fake_start_as_current

    mock_ctx.get_user_context.return_value = {}
    mock_ctx.get_mcp_props.return_value = {}

    lf_tracer._client = mock_client
    lf_tracer._propagate_attributes_fn = mock_propagate
    try:
        with pytest.raises(RuntimeError):
            async with lf_tracer.start_workflow_step_trace("planning"):
                raise RuntimeError("agent failed")
    finally:
        lf_tracer._client = None
        lf_tracer._propagate_attributes_fn = None

    mock_root_obs.update.assert_called_once_with(
        level="ERROR", status_message="workflow_step_failed"
    )


# ---------------------------------------------------------------------------
# create_generation
# ---------------------------------------------------------------------------

def test_create_generation_no_root_obs_returns_none():
    lf_tracer._client = MagicMock()
    lf_tracer._current_root_obs.set(None)

    result = lf_tracer.create_generation(
        name="agent_query:kb_init",
        model="claude-opus-4",
        input_data={"system_prompt": "..."},
        metadata={},
    )
    assert result is None


def test_create_generation_client_disabled_returns_none():
    lf_tracer._client = None
    result = lf_tracer.create_generation(
        name="agent_query:planning",
        model="claude-opus-4",
        input_data={},
        metadata={},
    )
    assert result is None


def test_create_generation_calls_start_observation_on_root():
    mock_root = MagicMock()
    mock_gen = MagicMock()
    mock_root.start_observation.return_value = mock_gen

    lf_tracer._client = MagicMock()
    lf_tracer._current_root_obs.set(mock_root)
    try:
        result = lf_tracer.create_generation(
            name="agent_query:coding",
            model="claude-opus-4",
            input_data={"system_prompt": "test"},
            metadata={"workspace_name": "ws-01-1"},
        )
    finally:
        lf_tracer._client = None
        lf_tracer._current_root_obs.set(None)

    assert result is mock_gen
    mock_root.start_observation.assert_called_once_with(
        name="agent_query:coding",
        as_type="generation",
        model="claude-opus-4",
        input={"system_prompt": "test"},
        metadata={"workspace_name": "ws-01-1"},
        model_parameters=None,
    )


def test_create_generation_forwards_model_parameters():
    mock_root = MagicMock()
    mock_gen = MagicMock()
    mock_root.start_observation.return_value = mock_gen

    lf_tracer._client = MagicMock()
    lf_tracer._current_root_obs.set(mock_root)
    try:
        lf_tracer.create_generation(
            name="agent_query:planning",
            model="claude-opus-4",
            input_data={},
            metadata={},
            model_parameters={"max_turns": 100, "permission_mode": "acceptEdits"},
        )
    finally:
        lf_tracer._client = None
        lf_tracer._current_root_obs.set(None)

    call_kwargs = mock_root.start_observation.call_args.kwargs
    assert call_kwargs["model_parameters"]["max_turns"] == 100
    assert call_kwargs["model_parameters"]["permission_mode"] == "acceptEdits"


# ---------------------------------------------------------------------------
# init() enablement rule: all three of PUBLIC_KEY, SECRET_KEY, BASE_URL required
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "public_key, secret_key, base_url",
    [
        (None, "sk", "https://cloud.langfuse.com"),
        ("pk", None, "https://cloud.langfuse.com"),
        ("pk", "sk", None),
        (None, None, None),
    ],
    ids=["missing_public_key", "missing_secret_key", "missing_base_url", "all_missing"],
)
def test_init_disabled_when_any_credential_missing(public_key, secret_key, base_url):
    """init() must not construct a client when any of the three creds is missing."""
    instance = LangfuseTracer()
    with patch("app.services.langfuse.client.settings") as mock_settings:
        mock_settings.LANGFUSE_PUBLIC_KEY = public_key
        mock_settings.LANGFUSE_SECRET_KEY = secret_key
        mock_settings.LANGFUSE_BASE_URL = base_url
        # settings.langfuse_enabled is the derived property used by init()
        mock_settings.langfuse_enabled = bool(public_key and secret_key and base_url)
        instance.init()
    assert instance._client is None
    assert instance.is_enabled() is False


# ---------------------------------------------------------------------------
# sdk_session_id capture and propagation
# ---------------------------------------------------------------------------

def test_sdk_session_id_captured_from_first_message_with_it():
    """SDK session_id is captured from the first message that exposes it and
    stamped onto the generation observation immediately."""
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)

    # First message has no session_id — capture should not fire.
    msg_no_sid = _make_assistant_message([_make_text_block("hello")])
    msg_no_sid.session_id = None  # explicit override of MagicMock default
    tracer.push(msg_no_sid)
    assert tracer.sdk_session_id is None

    # Second message carries session_id — capture fires once.
    msg_with_sid = MagicMock()
    msg_with_sid.session_id = "49289eec-0d35-4927-8197-43f6e63a5783"
    tracer.push(msg_with_sid)

    assert tracer.sdk_session_id == "49289eec-0d35-4927-8197-43f6e63a5783"
    generation.update.assert_any_call(
        metadata={"sdk_session_id": "49289eec-0d35-4927-8197-43f6e63a5783"}
    )

    # Third message also carries session_id — must NOT re-stamp (idempotent).
    update_calls_before = generation.update.call_count
    msg_with_sid_again = MagicMock()
    msg_with_sid_again.session_id = "different-id"
    tracer.push(msg_with_sid_again)
    assert tracer.sdk_session_id == "49289eec-0d35-4927-8197-43f6e63a5783"
    assert generation.update.call_count == update_calls_before


def test_sdk_session_id_appears_on_tool_span_metadata():
    """Once captured, sdk_session_id must flow into every subsequent tool span."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span
    tracer = LangfuseStreamTracer(generation)

    # Seed the SDK session id directly (skip the message-capture path).
    tracer.sdk_session_id = "sdk-xyz"

    tracer.push(_make_assistant_message([
        _make_tool_use_block("t1", "Read", {"path": "/foo"}),
    ]))

    md = generation.start_observation.call_args.kwargs["metadata"]
    assert md["sdk_session_id"] == "sdk-xyz"


def test_sdk_session_id_in_final_generation_update():
    """sdk_session_id must also land in the generation.update() metadata on ResultMessage."""
    generation = MagicMock()
    tracer = LangfuseStreamTracer(generation)
    tracer.sdk_session_id = "sdk-final"

    tracer.push(_make_result_message())
    _complete_default_billing(tracer)

    # Find the update call carrying usage_details (the final-ResultMessage update,
    # not the early metadata-only stamp from _capture_sdk_session_id).
    final_call = next(
        c for c in generation.update.call_args_list
        if "usage_details" in c.kwargs
    )
    assert final_call.kwargs["metadata"]["sdk_session_id"] == "sdk-final"


# ---------------------------------------------------------------------------
# retry_count propagation from TelemetryContext into trace metadata
# ---------------------------------------------------------------------------

@patch("app.services.langfuse.client.TelemetryContext")
def test_build_propagate_metadata_includes_retry_count(mock_ctx):
    mock_ctx.get_user_context.return_value = {"retry_count": 2}
    result = lf_tracer._build_propagate_metadata(extra_metadata=None)
    assert result["retry_count"] == "2"


@patch("app.services.langfuse.client.TelemetryContext")
def test_build_propagate_metadata_omits_retry_count_when_unset(mock_ctx):
    mock_ctx.get_user_context.return_value = {"workspace_name": "ws-01"}
    result = lf_tracer._build_propagate_metadata(extra_metadata=None)
    assert "retry_count" not in result


@patch("app.services.langfuse.client.TelemetryContext")
def test_build_propagate_metadata_retry_count_zero_still_propagated(mock_ctx):
    """retry_count=0 (first attempt) must still propagate — 0 is a meaningful value,
    not a sentinel for 'unset'."""
    mock_ctx.get_user_context.return_value = {"retry_count": 0}
    result = lf_tracer._build_propagate_metadata(extra_metadata=None)
    assert result["retry_count"] == "0"


@patch("app.services.langfuse.client.TelemetryContext")
def test_build_propagate_metadata_workflow_is_canonical_string(mock_ctx):
    """A TelemetryWorkflowLabel must propagate as its `to_stored_string()`,
    not the verbose Pydantic repr ('label=... phase_kind=None ...')."""
    from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
    label = TelemetryWorkflowLabel.plain("specification_completeness_check")
    mock_ctx.get_user_context.return_value = {"workflow": label}
    result = lf_tracer._build_propagate_metadata(extra_metadata=None)
    assert result["workflow"] == "specification_completeness_check"


# ---------------------------------------------------------------------------
# Skill spans: name lifted from input, is_skill metadata
# ---------------------------------------------------------------------------

def test_skill_tool_use_block_renames_span_from_input_skill_name():
    """ToolUseBlock(name='Skill', input={'skill_name': 'x'}) → span 'Skill: x'."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span
    tracer = LangfuseStreamTracer(generation)

    skill_block = _make_tool_use_block(
        "s1", "Skill", {"skill_name": "backend-quality-gate"}
    )
    tracer.push(_make_assistant_message([skill_block]))

    call_kwargs = generation.start_observation.call_args.kwargs
    assert call_kwargs["name"] == "Skill: backend-quality-gate"
    assert call_kwargs["metadata"]["is_skill"] is True
    assert call_kwargs["metadata"]["skill_name"] == "backend-quality-gate"


def test_skill_tool_use_block_falls_back_to_name_or_command_key():
    """If 'skill_name' is missing, the loader tries 'name' then 'command'."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span
    tracer = LangfuseStreamTracer(generation)

    skill_block = _make_tool_use_block("s1", "Skill", {"command": "/foo"})
    tracer.push(_make_assistant_message([skill_block]))

    call_kwargs = generation.start_observation.call_args.kwargs
    assert call_kwargs["name"] == "Skill: /foo"
    assert call_kwargs["metadata"]["is_skill"] is True


def test_skill_tool_use_block_without_identifiable_input_keeps_skill_name():
    """If no known key is present in input, span stays named 'Skill' (no is_skill tag)."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span
    tracer = LangfuseStreamTracer(generation)

    skill_block = _make_tool_use_block("s1", "Skill", {"unrelated": "x"})
    tracer.push(_make_assistant_message([skill_block]))

    call_kwargs = generation.start_observation.call_args.kwargs
    assert call_kwargs["name"] == "Skill"
    assert "is_skill" not in call_kwargs["metadata"]


def test_non_skill_tool_use_block_unchanged():
    """Regular ToolUseBlocks (name != 'Skill') must not be touched by the Skill branch."""
    generation = MagicMock()
    span = MagicMock()
    generation.start_observation.return_value = span
    tracer = LangfuseStreamTracer(generation)

    tracer.push(_make_assistant_message([
        _make_tool_use_block("t1", "Read", {"path": "/foo"}),
    ]))

    call_kwargs = generation.start_observation.call_args.kwargs
    assert call_kwargs["name"] == "Read"
    assert "is_skill" not in call_kwargs["metadata"]
