"""RED tests for TRC-07..10 (`llm.tracing.logging_sink.LoggingSink`).

Target-state module: `src/llm/tracing/logging_sink.py` (does not exist yet —
Wave 3 step `w3-b` builds it). Catalog: the central-logger test catalog
§2.2 rows TRC-07..10.

`LoggingSink` calls `log_event` directly for every method, so every
assertion here goes through `caplog`, using the shared helpers from
`tests/logger_harness.py` (`events`, `fields`, `assert_json_plain`) per the
task's harness instructions.

`llm.tracing.logging_sink` and `logger.correlation` are imported at module level.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest

from llm.settings import _log_cli_stderr_line
from llm.tracing.core import ToolEnd, ToolStart, TraceClose, TraceResult, TraceStart
from llm.tracing.logging_sink import LoggingSink
from logger.correlation import bound
from logger_harness import assert_json_plain, events, fields


def _logging_sink(**kwargs: Any) -> Any:
    return LoggingSink(**kwargs)


@contextmanager
def _bound(**ids: str):
    with bound(**ids):
        yield


def start_event(
    *,
    prompt_input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> TraceStart:
    return TraceStart(name="agent", model="model-1", input=prompt_input, metadata=dict(metadata or {}))


def _assert_tool_events(caplog: pytest.LogCaptureFixture) -> None:
    tool_start_records = events(caplog, "tool.start")
    assert len(tool_start_records) == 2
    first_tool_start = fields(tool_start_records[0])
    assert first_tool_start["tool_use_id"] == "tool-1"
    assert first_tool_start["tool_name"] == "Read"
    assert first_tool_start["is_server"] is False

    tool_end_records = events(caplog, "tool.end")
    assert len(tool_end_records) == 2
    known = fields(tool_end_records[0])
    assert known["tool_use_id"] == "tool-1"
    assert known["tool_name"] == "Read"
    assert known["is_error"] is False
    assert tool_end_records[0].levelname == "INFO"
    unknown = fields(tool_end_records[1])
    assert unknown["tool_use_id"] == "unknown-tool"
    assert unknown["tool_name"] is None
    assert unknown["is_error"] is True
    assert tool_end_records[1].levelname == "ERROR"

    abandoned_records = events(caplog, "tool.abandoned")
    assert len(abandoned_records) == 1
    assert abandoned_records[0].levelname == "WARNING"
    abandoned_fields = fields(abandoned_records[0])
    assert abandoned_fields["tool_use_id"] == "tool-2"
    assert abandoned_fields["tool_name"] == "Bash"


def _assert_result_and_close_events(caplog: pytest.LogCaptureFixture) -> None:
    result_records = events(caplog, "trace.result")
    assert len(result_records) == 1
    assert result_records[0].levelname == "INFO"
    result_fields = fields(result_records[0])
    assert result_fields["usage"] == {"input": 3, "output": 5, "total": 8}
    assert result_fields["cost"] == 0.01
    assert result_fields["num_turns"] == 2
    assert result_fields["subtype"] == "success"
    assert result_fields["is_error"] is False

    close_records = events(caplog, "trace.close")
    assert len(close_records) == 1
    assert close_records[0].levelname == "INFO"
    close_fields = fields(close_records[0])
    assert close_fields["status_message"] == "agent_result"
    # metadata was None on this close() call -> the session-snapshot keys
    # must be entirely absent, not present-with-null.
    assert "num_messages" not in close_fields
    assert "num_tool_uses" not in close_fields
    assert "sdk_session_id" not in close_fields


# TRC-07
def test_logging_sink_maps_every_method_to_its_documented_event_level_and_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Construct the sink *before* touching caplog's level: `LoggingSink()`
    # triggers the central logger's first bootstrap (via `get_logger()`) if
    # the pipeline isn't already up, and every bootstrap unconditionally
    # sets `cfg.level` on the "llm" APP_ROOT_LOGGERS entry
    # ("app roots -> cfg.level") -- so doing
    # this first, then setting caplog's level second, means caplog's own
    # level-setting is the LAST thing to run and reliably wins regardless
    # of whether this happens to be the process's first bootstrap or a
    # later one (e.g. when this test runs after another test file's own
    # bootstrap/reset cycle). Reversing this order is exactly what made
    # this test order-dependent before: a bootstrap triggered by
    # constructing the sink *after* caplog.set_level() would silently
    # clobber caplog's DEBUG override back down to `cfg.level` (INFO).
    sink = _logging_sink()

    # `caplog.set_level(DEBUG)` with no `logger=` only changes the ROOT
    # logger's captured level. `LoggingSink` logs through
    # `llm.tracing.logging_sink`, a child of the "llm" APP_ROOT_LOGGERS entry
    # that bootstrap pins to INFO ("app roots -> cfg.level"), so
    # `trace.model`/`trace.system_message`
    # (DEBUG-level, per §4.2's mapping table) would never reach the
    # originating logger's effective-level check, let alone caplog. Passing
    # `logger="llm"` sets that ancestor's level (and caplog's handler level)
    # to DEBUG, which the child `llm.tracing.logging_sink` logger inherits.
    caplog.set_level(logging.DEBUG, logger="llm")

    sink.start(
        start_event(
            prompt_input={"prompt": "hi"},
            metadata={
                "session_id": "sdk-session-1",
                "langfuse_session_id": "implement-1a2b3c4d",
                "capture_prompt": True,
                "num_messages": 0,
                "is_error": False,
                "role": "judge",
            },
        )
    )
    sink.update_model("model-2")
    sink.record_system_message({"subtype": "init"})
    sink.start_tool(ToolStart(id="tool-1", name="Read", is_server=False, input={"path": "/a"}))
    sink.end_tool(ToolEnd(id="tool-1", output="contents", is_error=False))
    sink.start_tool(ToolStart(id="tool-2", name="Bash", is_server=False))
    sink.abandon_tool("tool-2")
    # end_tool for an id that was never start_tool'd on this instance -> null.
    sink.end_tool(ToolEnd(id="unknown-tool", output=None, is_error=True))
    sink.record_result(
        TraceResult(
            metadata={"subtype": "success", "num_turns": 2},
            usage={"input": 3, "output": 5, "total": 8},
            cost=0.01,
            output="final",
            reasoning="because",
        )
    )
    sink.close(TraceClose(level="default", status_message="agent_result"))

    start_records = events(caplog, "trace.start")
    assert len(start_records) == 1
    start_record = start_records[0]
    assert start_record.levelname == "INFO"
    assert start_record.funcName == "start"
    start_fields = fields(start_record)
    assert start_fields["model"] == "model-1"
    assert start_fields["sdk_session_id"] == "sdk-session-1"
    assert start_fields["metadata"] == {"role": "judge"}
    assert_json_plain(start_record)

    model_records = events(caplog, "trace.model")
    assert len(model_records) == 1
    assert model_records[0].levelname == "DEBUG"
    assert fields(model_records[0])["model"] == "model-2"

    system_records = events(caplog, "trace.system_message")
    assert len(system_records) == 1
    assert system_records[0].levelname == "DEBUG"

    _assert_tool_events(caplog)
    _assert_result_and_close_events(caplog)


def test_end_tool_is_error_true_logs_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()
    sink.start_tool(ToolStart(id="tool-1", name="Bash", is_server=False))

    sink.end_tool(ToolEnd(id="tool-1", output="boom", is_error=True))

    record = events(caplog, "tool.end")[-1]
    assert record.levelname == "ERROR"
    assert fields(record)["is_error"] is True


def test_record_result_is_error_true_logs_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()

    sink.record_result(TraceResult(metadata={}, usage={}, cost=None, is_error=True))

    record = events(caplog, "trace.result")[-1]
    assert record.levelname == "ERROR"


def test_close_includes_session_snapshot_fields_only_when_metadata_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()

    sink.close(
        TraceClose(
            level="warning",
            status_message="transport_error",
            metadata={
                "session_id": "sdk-session-1",
                "num_messages": 2,
                "num_tool_uses": 1,
            },
        )
    )

    record = events(caplog, "trace.close")[-1]
    assert record.levelname == "WARNING"
    record_fields = fields(record)
    assert record_fields["status_message"] == "transport_error"
    assert record_fields["sdk_session_id"] == "sdk-session-1"
    assert record_fields["num_messages"] == 2
    assert record_fields["num_tool_uses"] == 1


def test_close_error_level_maps_to_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()

    sink.close(TraceClose(level="error", status_message="transport_error"))

    record = events(caplog, "trace.close")[-1]
    assert record.levelname == "ERROR"


# TRC-08 (AC-9) — payloads are only ever summarised: no full mode exists.
def test_logging_sink_has_no_agent_full_parameter() -> None:
    with pytest.raises(TypeError):
        _logging_sink(agent_full=True)


_SUMMARISED_PREFIXES = ("payload", "data", "reasoning")


def _assert_only_summaries(record_fields: dict[str, Any]) -> None:
    for prefix in _SUMMARISED_PREFIXES:
        assert prefix not in record_fields, f"bare {prefix!r} body logged"
    assert not [key for key in record_fields if key.endswith("_capped")]


def test_every_payload_field_is_a_bounded_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _logging_sink()
    caplog.set_level(logging.DEBUG, logger="llm")
    big = "x" * 500

    sink.start(start_event(prompt_input={"prompt": big}))
    sink.record_system_message({"subtype": "init", "blob": big})
    sink.start_tool(ToolStart(id="tool-1", name="Read", is_server=False, input=big))
    sink.end_tool(ToolEnd(id="tool-1", output=big, is_error=False))
    sink.record_result(TraceResult(metadata={}, usage={}, cost=None, output=big, reasoning=big))

    expected_prefix = {
        "trace.start": "payload",
        "trace.system_message": "data",
        "tool.start": "payload",
        "tool.end": "payload",
        "trace.result": "payload",
    }
    for event_name, prefix in expected_prefix.items():
        [record] = events(caplog, event_name)
        record_fields = fields(record)
        _assert_only_summaries(record_fields)
        assert record_fields[f"{prefix}_truncated"] is True
        assert len(record_fields[f"{prefix}_preview"]) == 200
    result_fields = fields(events(caplog, "trace.result")[0])
    assert result_fields["reasoning_truncated"] is True


def test_cli_stderr_line_is_logged_as_a_summary_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _logging_sink()  # bootstrap first, so caplog's DEBUG level wins
    caplog.set_level(logging.DEBUG, logger="llm")

    _log_cli_stderr_line("[claude-code:unrecognized_model] " + "z" * 500)

    [record] = events(caplog, "agent.cli.stderr")
    record_fields = fields(record)
    assert "line" not in record_fields
    assert not [key for key in record_fields if key.endswith("_capped")]
    assert record_fields["line_preview"].startswith("[claude-code:unrecognized_model]")
    assert record_fields["line_truncated"] is True


# AC-19 (a) — a normal-size metadata dict stays structured on stdout JSON.
def test_start_metadata_stays_structured_through_the_pipeline(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")
    sink = _logging_sink()

    sink.start(start_event(metadata={"note": "m" * 500}))

    [record] = central_log.events("trace.start")
    assert record["metadata"] == {"note": "m" * 500}
    assert record["severity"] == "INFO"


def test_no_input_produces_no_payload_keys_at_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()

    sink.start(start_event(prompt_input=None))

    record_fields = fields(events(caplog, "trace.start")[-1])
    payload_keys = [key for key in record_fields if key.startswith("payload")]
    assert payload_keys == []


# TRC-09
def test_trace_url_uses_the_configured_base_and_is_absent_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    run_id = "ab" * 16

    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    configured_sink = _logging_sink()
    with _bound(run_id=run_id):
        configured_sink.start(start_event())
    configured_fields = fields(events(caplog, "trace.start")[-1])
    assert configured_fields["trace_url"] == f"https://cloud.langfuse.com/trace/{run_id}"

    caplog.clear()
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    unconfigured_sink = _logging_sink()
    with _bound(run_id=run_id):
        unconfigured_sink.start(start_event())
    unconfigured_fields = fields(events(caplog, "trace.start")[-1])
    assert "trace_url" not in unconfigured_fields


def test_logging_sink_never_holds_or_calls_a_langfuse_client() -> None:
    # LoggingSink(*, logger=None) takes no client at all,
    # so computing trace_url can never reach out over the network.
    sink = _logging_sink()
    assert not hasattr(sink, "client")


# TRC-10
def test_sdk_session_id_field_is_independent_of_the_bound_registry_session_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    sink = _logging_sink()

    with _bound(session_id="implement-1a2b3c4d", run_id="c" * 32, agent_id="implement"):
        sink.start(start_event(metadata={"session_id": "sdk-session-xyz"}))

    record = events(caplog, "trace.start")[-1]
    record_fields = fields(record)
    assert record_fields["sdk_session_id"] == "sdk-session-xyz"
    # Correlation IDs are never passed as explicit fields by LoggingSink, so
    # log_event's field/RESERVED-key collision rename never fires for them.
    assert "field_run_id" not in record_fields
    assert "field_session_id" not in record_fields
    assert "field_agent_id" not in record_fields
    # The registry session id (bound ambiently) is the one on the record,
    # distinct from the SDK's own session id field above.
    assert record.session_id == "implement-1a2b3c4d"
    assert record.run_id == "c" * 32
