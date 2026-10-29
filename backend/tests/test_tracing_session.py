from __future__ import annotations

from llm.tracing import TraceCapture, TraceSession, normalize_usage
from tracing_support import RecordingSink, call_value


def test_default_session_keeps_sensitive_content_out_of_sink() -> None:
    sink = RecordingSink()
    session = TraceSession(
        sink,
        prompt="prompt secret",
        system_prompt="system secret",
        metadata={"label": "safe"},
    )

    session.message(session_id="session-1", model="model-1")
    session.system_message({"credential": "credential secret"})
    session.assistant_text("assistant secret")
    session.reasoning("reasoning secret")
    session.tool_started(
        tool_id="tool-1",
        name="Read",
        is_server=False,
        tool_input={"path": "input secret"},
    )
    session.tool_finished(tool_id="tool-1", output="output secret")
    session.result(
        is_error=False,
        usage={"input_tokens": 3, "output_tokens": 5},
        cost=0.01,
        output="final secret",
        metadata={"subtype": "success"},
    )

    recorded = repr(sink.calls)
    for secret in (
        "prompt secret",
        "system secret",
        "credential secret",
        "assistant secret",
        "reasoning secret",
        "input secret",
        "output secret",
        "final secret",
    ):
        assert secret not in recorded
    start = call_value(sink, "start")
    assert start.input is None
    assert start.metadata["label"] == "safe"
    assert start.metadata["session_id"] == "session-1"
    assert call_value(sink, "start_tool").input is None
    assert call_value(sink, "end_tool").output is None
    result = call_value(sink, "record_result")
    assert result.usage == {"input": 3, "output": 5, "total": 8}
    assert result.output is None
    assert result.reasoning is None
    assert result.metadata["num_messages"] == 1
    assert result.metadata["num_tool_uses"] == 1
    assert session.closed is True


def test_each_sensitive_category_requires_an_explicit_opt_in() -> None:
    sink = RecordingSink()
    session = TraceSession(
        sink,
        prompt="prompt",
        system_prompt="system",
        capture=TraceCapture(
            prompt=True,
            system_prompt=True,
            system_message_data=True,
            assistant_output=True,
            reasoning=True,
            tool_io=True,
        ),
    )

    session.message()
    session.system_message({"detail": "system data"})
    session.assistant_text("assistant")
    session.reasoning("private reasoning")
    session.tool_started(
        tool_id="tool-1",
        name="Read",
        is_server=False,
        tool_input={"path": "/secret"},
    )
    session.tool_finished(tool_id="tool-1", output="file contents")
    session.result(is_error=False, output="final")

    assert call_value(sink, "start").input == {
        "prompt": "prompt",
        "system_prompt": "system",
    }
    assert call_value(sink, "record_system_message") == {"detail": "system data"}
    assert call_value(sink, "start_tool").input == {"path": "/secret"}
    assert call_value(sink, "end_tool").output == "file contents"
    result = call_value(sink, "record_result")
    assert result.output == "final"
    assert result.reasoning == "private reasoning"


def test_result_close_level_reflects_error_outcome() -> None:
    """`result()`'s own `trace.close` must track `is_error`, like `close()` does.

    Regression test for the E2E-found bug (central-logger-e2e iter0/iter1
    reports): `result()` used to hardcode `TraceClose(level="default", ...)`
    regardless of the recorded `TraceResult.is_error`, so an errored agent
    run still produced an INFO-level `trace.close`. `close()` (the
    exception-driven path) already computed its level from `is_error`
    correctly -- this asserts `result()` now matches that behavior for both
    outcomes.
    """

    error_sink = RecordingSink()
    error_session = TraceSession(error_sink)
    error_session.message()

    error_session.result(is_error=True)

    error_close = call_value(error_sink, "close")
    assert error_close.level == "error"
    assert error_close.status_message == "agent_result"

    success_sink = RecordingSink()
    success_session = TraceSession(success_sink)
    success_session.message()

    success_session.result(is_error=False)

    success_close = call_value(success_sink, "close")
    assert success_close.level == "default"
    assert success_close.status_message == "agent_result"


def test_close_abandons_tools_and_is_idempotent() -> None:
    sink = RecordingSink()
    session = TraceSession(sink)
    session.message()
    session.tool_started(tool_id="tool-1", name="Bash", is_server=False)

    session.close(is_error=True, status_message="transport_error")
    session.close()

    assert ("abandon_tool", "tool-1") in sink.calls
    close = call_value(sink, "close")
    assert close.level == "error"
    assert close.status_message == "transport_error"
    assert close.metadata is not None
    assert close.metadata["num_tool_uses"] == 1


def test_sink_operation_failures_are_isolated() -> None:
    sink = RecordingSink("update_model", "start_tool", "record_result", "close")
    session = TraceSession(sink)

    session.message()
    session.message(model="model")
    session.tool_started(tool_id="tool-1", name="Read", is_server=False)
    session.result(is_error=False)

    assert session.closed is True
    assert session.error_count == 4


def test_start_failure_disables_session() -> None:
    sink = RecordingSink("start")
    session = TraceSession(sink)

    session.message()
    session.message()

    assert session.enabled is False
    assert session.error_count == 1
    assert [name for name, _ in sink.calls] == ["start"]


def test_disabled_session_is_a_noop() -> None:
    session = TraceSession(None)

    session.message()
    session.result(is_error=True)
    session.close()

    assert session.enabled is False
    assert session.closed is True
    assert session.error_count == 0


def test_usage_normalization_accepts_only_integer_token_counts() -> None:
    assert normalize_usage(
        {
            "input_tokens": 3,
            "output_tokens": 5,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": True,
            "nested": {"secret": "value"},
        }
    ) == {
        "input": 3,
        "output": 5,
        "total": 8,
        "cache_read_input_tokens": 2,
    }
