"""RED tests for TRC-01..06 (`llm.tracing.composite.CompositeTraceSink`).

Target-state module: `src/llm/tracing/composite.py` (does not exist yet — Wave
3 step `w3-b` builds it). Catalog: the central-logger test catalog
§2.2 rows TRC-01..06.

Fakes follow this repo's hand-written idiom rather than a mocking library:
`RecordingSink` (`tests/tracing_support.py`, shared with
`tests/test_tracing_session.py`) implements every `TraceSink` protocol method,
records call order, and can be told to raise on named methods.

`llm.tracing.composite` and `logger.correlation` are imported at module level.
"""

from __future__ import annotations

import contextvars
import logging
import re
from contextlib import contextmanager
from typing import Any

import pytest

from llm.tracing.composite import CompositeTraceSink
from llm.tracing.core import (
    ToolEnd,
    ToolStart,
    TraceClose,
    TraceResult,
    TraceStart,
)
from llm.tracing.session import TraceSession
from logger.correlation import (
    current_agent_id,
    current_request_id,
    current_run_id,
    current_session_id,
    new_request_context,
)
from logger_harness import events, fields
from tracing_support import RecordingSink


def _composite(sinks: tuple[Any, ...]) -> Any:
    return CompositeTraceSink(sinks)


def _current_run_id() -> str | None:
    return current_run_id()


def _current_agent_id() -> str | None:
    return current_agent_id()


def _current_session_id() -> str | None:
    return current_session_id()


def _current_request_id() -> str | None:
    return current_request_id()


@contextmanager
def _new_request_context():
    with new_request_context() as request_id:
        yield request_id


ALL_METHOD_NAMES = (
    "start",
    "update_model",
    "record_system_message",
    "start_tool",
    "end_tool",
    "abandon_tool",
    "record_result",
    "close",
)
NON_START_METHOD_NAMES = ALL_METHOD_NAMES[1:]


def start_event(*, langfuse_session_id: str | None = "session-1") -> TraceStart:
    metadata: dict[str, Any] = {}
    if langfuse_session_id is not None:
        metadata["langfuse_session_id"] = langfuse_session_id
    return TraceStart(name="agent", model="model-1", input=None, metadata=metadata)


def close_event() -> TraceClose:
    return TraceClose(level="default", status_message="agent_result")


def drive_all_methods(sink: Any) -> None:
    sink.start(start_event())
    sink.update_model("model-2")
    sink.record_system_message({"subtype": "init"})
    sink.start_tool(ToolStart(id="tool-1", name="Read", is_server=False))
    sink.end_tool(ToolEnd(id="tool-1", output="contents"))
    sink.abandon_tool("tool-2")
    sink.record_result(TraceResult(metadata={}, usage={}, cost=None))
    sink.close(close_event())


# TRC-01
def test_composite_fans_out_all_eight_methods_to_every_sink_in_order() -> None:
    sink_a = RecordingSink(label="a")
    sink_b = RecordingSink(label="b")
    composite = _composite((sink_a, sink_b))

    drive_all_methods(composite)

    assert [name for name, _ in sink_a.calls] == list(ALL_METHOD_NAMES)
    assert [name for name, _ in sink_b.calls] == list(ALL_METHOD_NAMES)


# TRC-02
def test_failing_child_is_isolated_and_the_other_child_still_gets_every_call() -> None:
    sink_a = RecordingSink(*NON_START_METHOD_NAMES, label="a")
    sink_b = RecordingSink(label="b")
    composite = _composite((sink_a, sink_b))

    drive_all_methods(composite)

    assert composite.failure_count == len(NON_START_METHOD_NAMES)
    assert [name for name, _ in sink_a.calls] == list(ALL_METHOD_NAMES)
    assert [name for name, _ in sink_b.calls] == list(ALL_METHOD_NAMES)


def test_repeated_failures_of_one_method_log_sink_failed_only_once_per_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    sink_a = RecordingSink("end_tool", label="a")
    sink_b = RecordingSink(label="b")
    composite = _composite((sink_a, sink_b))

    composite.start(start_event())
    for index in range(3):
        composite.end_tool(ToolEnd(id=f"tool-{index}", output="x"))

    assert composite.failure_count == 3
    sink_failed = events(caplog, "trace.sink_failed")
    assert len(sink_failed) == 1
    record = sink_failed[0]
    assert record.levelname == "WARNING"
    assert record.exc_info is not None
    record_fields = fields(record)
    assert record_fields["sink"] == "RecordingSink"
    assert record_fields["method"] == "end_tool"
    # sink_b never failed, so it is not the one named in the log record.
    assert [name for name, _ in sink_b.calls] == [
        "start",
        "end_tool",
        "end_tool",
        "end_tool",
    ]


# TRC-03
def test_a_start_failure_skips_that_child_for_the_rest_of_the_run_and_retries_next_run() -> None:
    sink_a = RecordingSink("start", label="a")
    sink_b = RecordingSink(label="b")
    composite = _composite((sink_a, sink_b))

    composite.start(start_event())
    composite.update_model("model-2")
    composite.close(close_event())

    assert [name for name, _ in sink_a.calls] == ["start"]
    assert [name for name, _ in sink_b.calls] == ["start", "update_model", "close"]

    # Next run: A is retried (not permanently disabled) and, once it succeeds,
    # participates normally again for the rest of that run.
    sink_a.fail_methods.clear()
    composite.start(start_event())
    composite.close(close_event())

    assert [name for name, _ in sink_a.calls] == ["start", "start", "close"]


# TRC-04
def test_all_children_failing_start_reraises_the_first_error_and_unbinds() -> None:
    sink_a = RecordingSink("start", label="a")
    sink_b = RecordingSink("start", label="b")
    composite = _composite((sink_a, sink_b))

    with pytest.raises(RuntimeError, match=r"^a\.start failed$"):
        composite.start(start_event())

    assert [name for name, _ in sink_a.calls] == ["start"]
    assert [name for name, _ in sink_b.calls] == ["start"]
    assert _current_run_id() is None
    assert _current_agent_id() is None
    assert _current_session_id() is None


def test_all_children_failing_start_disables_the_trace_session() -> None:
    sink_a = RecordingSink("start", label="a")
    composite = _composite((sink_a,))
    session = TraceSession(composite, name="agent")

    session.message()

    assert session.enabled is False
    assert session.error_count == 1


# TRC-05
def test_child_start_sees_the_bound_ids_and_outer_request_id_survives_close() -> None:
    captured: dict[str, str | None] = {}

    class CapturingSink(RecordingSink):
        def start(self, event: TraceStart) -> None:
            captured["run_id"] = _current_run_id()
            captured["agent_id"] = _current_agent_id()
            captured["session_id"] = _current_session_id()
            super().start(event)

    composite = _composite((CapturingSink(),))

    with _new_request_context() as request_id:
        composite.start(
            TraceStart(
                name="implement",
                model="model-1",
                input=None,
                metadata={"langfuse_session_id": "implement-1a2b3c4d"},
            )
        )
        composite.close(close_event())

        assert _current_request_id() == request_id

    assert re.fullmatch(r"[0-9a-f]{32}", captured["run_id"])
    assert captured["agent_id"] == "implement"
    assert captured["session_id"] == "implement-1a2b3c4d"
    assert _current_run_id() is None
    assert _current_agent_id() is None
    assert _current_session_id() is None


# TRC-06
def test_close_from_a_copied_context_does_not_raise_or_corrupt_later_runs() -> None:
    composite = _composite((RecordingSink(),))
    composite.start(start_event())

    # Simulate a cross-context close (e.g. an SDK boundary that forks the
    # contextvars Context between start() and close()): the token from
    # start() cannot be reset inside a *different* Context, so the
    # correlation layer's O4 fallback must absorb the ValueError instead of
    # letting it escape through close().
    ctx = contextvars.copy_context()
    ctx.run(composite.close, close_event())

    # No leak: a fresh, real start/close cycle afterwards still behaves
    # normally and leaves nothing bound.
    composite.start(start_event())
    composite.close(close_event())

    assert _current_run_id() is None
    assert _current_agent_id() is None
    assert _current_session_id() is None


def test_close_from_a_copied_context_still_fans_out_to_children() -> None:
    sink = RecordingSink()
    composite = _composite((sink,))
    composite.start(start_event())

    ctx = contextvars.copy_context()
    ctx.run(composite.close, close_event())

    assert [name for name, _ in sink.calls] == ["start", "close"]
