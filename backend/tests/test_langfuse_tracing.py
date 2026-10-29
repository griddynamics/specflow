from __future__ import annotations

import logging
import re
from contextlib import contextmanager, suppress
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

import llm.agent as agent_module
import logger.logger as central_logger
from llm import Agent, AgentSettings
from llm.tracing import (
    LangfuseMessageObserver,
    LangfuseRegistryEvidence,
    LangfuseSink,
    LangfuseTraceRegistry,
    ToolEnd,
    ToolStart,
    TraceClose,
    TraceResult,
    TraceStart,
    require_authenticated_langfuse_client,
)
from llm.tracing.logging_sink import LoggingSink
from logger.correlation import bound, current_agent_id, current_run_id
from logger_harness import events


class FakeObservation:
    def __init__(
        self,
        *,
        fail_update: bool = False,
        fail_end: bool = False,
    ) -> None:
        self.fail_update = fail_update
        self.fail_end = fail_end
        self.started: list[dict[str, Any]] = []
        self.children: list[FakeObservation] = []
        self.updates: list[dict[str, Any]] = []
        self.end_count = 0

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        self.started.append(kwargs)
        child = FakeObservation(
            fail_update=self.fail_update,
            fail_end=self.fail_end,
        )
        self.children.append(child)
        return child

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        if self.fail_update:
            raise RuntimeError("update failed")

    def end(self) -> None:
        self.end_count += 1
        if self.fail_end:
            raise RuntimeError("end failed")


class FakeClient:
    def __init__(
        self,
        *,
        generation: FakeObservation | None = None,
        fail_start: bool = False,
        authenticated: bool = True,
    ) -> None:
        self.generation = generation or FakeObservation()
        self.fail_start = fail_start
        self.authenticated = authenticated
        self.started: list[dict[str, Any]] = []
        self.flushes = 0

    def start_observation(self, **kwargs: Any) -> FakeObservation:
        self.started.append(kwargs)
        if self.fail_start:
            raise RuntimeError("client failed")
        return self.generation

    def flush(self) -> None:
        self.flushes += 1

    def auth_check(self) -> bool:
        return self.authenticated


def start_event() -> TraceStart:
    return TraceStart(
        name="agent",
        model="model-1",
        input=None,
        metadata={"session_id": "session-1"},
    )


def test_sink_propagates_langfuse_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    @contextmanager
    def fake_propagate(*, session_id: str):
        seen.append(session_id)
        yield

    monkeypatch.setattr("llm.tracing.langfuse.propagate_attributes", fake_propagate)
    client = FakeClient()
    sink = LangfuseSink(client)
    sink.start(
        TraceStart(
            name="agent",
            model="model-1",
            input=None,
            metadata={
                "langfuse_session_id": "lf-session",
                "session_id": "claude-session",
            },
        )
    )
    sink.start_tool(ToolStart(id="tool-1", name="Read", is_server=False))

    assert seen == ["lf-session", "lf-session"]


def test_sink_maps_generic_lifecycle_to_langfuse() -> None:
    client = FakeClient()
    sink = LangfuseSink(client)

    sink.start(start_event())
    sink.update_model("model-2")
    sink.record_system_message({"subtype": "init"})
    sink.start_tool(
        ToolStart(
            id="tool-1",
            name="Read",
            is_server=False,
            input={"path": "/work/a"},
        )
    )
    sink.end_tool(ToolEnd(id="tool-1", output="contents"))
    sink.record_result(
        TraceResult(
            metadata={"session_id": "session-1", "num_turns": 2},
            usage={"input": 3, "output": 5, "total": 8},
            cost=0.01,
            output="final",
        )
    )
    sink.close(TraceClose(level="default", status_message="agent_result"))

    assert re.fullmatch(r"[0-9a-f]{32}", sink.trace_id)
    assert client.started == [
        {
            "name": "agent",
            "as_type": "generation",
            "model": "model-1",
            "metadata": {
                "session_id": "session-1",
                "integration": "llm.LangfuseMessageObserver",
            },
            "trace_context": {"trace_id": sink.trace_id},
        }
    ]
    assert client.generation.started[0]["name"] == "Read"
    assert client.generation.started[0]["input"] == {"path": "/work/a"}
    tool = client.generation.children[0]
    assert tool.updates == [{"output": "contents"}]
    assert tool.end_count == 1
    assert {"model": "model-2"} in client.generation.updates
    final = client.generation.updates[-1]
    assert final["metadata"]["sdk_session_id"] == "session-1"
    assert final["usage_details"]["total"] == 8
    assert final["cost_details"] == {"total": 0.01}
    assert final["output"] == "final"
    assert client.generation.end_count == 1


def test_sink_marks_error_results_without_diagnostics() -> None:
    client = FakeClient()
    sink = LangfuseSink(client)
    sink.start(start_event())

    sink.record_result(
        TraceResult(
            metadata={"session_id": "session-1", "is_error": True},
            usage={},
            cost=None,
            is_error=True,
        )
    )

    assert client.generation.updates[-1]["level"] == "ERROR"
    assert client.generation.updates[-1]["status_message"] == "agent_result_error"


def test_sink_always_ends_tool_after_update_failure() -> None:
    generation = FakeObservation(fail_update=True)
    sink = LangfuseSink(FakeClient(generation=generation))
    sink.start(start_event())
    sink.start_tool(ToolStart(id="tool-1", name="Read", is_server=False))

    with suppress(RuntimeError):
        sink.end_tool(ToolEnd(id="tool-1", output="contents"))

    assert generation.children[0].end_count == 1


def test_sink_abandons_open_tool() -> None:
    client = FakeClient()
    sink = LangfuseSink(client)
    sink.start(start_event())
    sink.start_tool(ToolStart(id="tool-1", name="Bash", is_server=False))

    sink.abandon_tool("tool-1")

    tool = client.generation.children[0]
    assert tool.updates == [
        {
            "level": "WARNING",
            "status_message": "stream_ended_with_open_tool",
        }
    ]
    assert tool.end_count == 1


def test_sink_maps_incomplete_run_close_metadata() -> None:
    client = FakeClient()
    sink = LangfuseSink(client)
    sink.start(start_event())

    sink.close(
        TraceClose(
            level="error",
            status_message="transport_error",
            metadata={"session_id": "session-1", "num_messages": 2},
        )
    )

    assert client.generation.updates[-1] == {
        "metadata": {
            "session_id": "session-1",
            "num_messages": 2,
            "sdk_session_id": "session-1",
        },
        "level": "ERROR",
        "status_message": "transport_error",
    }
    assert client.generation.end_count == 1


def test_message_observer_composes_claude_adapter_session_and_sink() -> None:
    client = FakeClient()
    observer = LangfuseMessageObserver(client)

    observer(
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={"input_tokens": 3, "output_tokens": 5},
            result="secret by default",
        )
    )

    final = client.generation.updates[-1]
    assert final["usage_details"]["total"] == 8
    assert "output" not in final
    assert observer.closed is True
    assert observer.error_count == 0


def test_message_observer_starts_a_new_trace_for_each_run() -> None:
    client = FakeClient()
    observer = LangfuseMessageObserver(client)

    for session_id in ("session-1", "session-2"):
        observer(
            ResultMessage(
                subtype="success",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                usage={},
            )
        )

    assert len(client.started) == 2
    assert client.started[0]["metadata"]["session_id"] == "session-1"
    assert client.started[1]["metadata"]["session_id"] == "session-2"
    assert client.started[1]["metadata"]["num_messages"] == 1
    assert client.generation.end_count == 2
    assert observer.closed is True


def test_client_start_failure_is_isolated_and_disables_observer() -> None:
    observer = LangfuseMessageObserver(FakeClient(fail_start=True))

    observer(
        ResultMessage(
            subtype="error",
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=0,
            session_id="session-1",
        )
    )

    assert observer.enabled is False
    assert observer.error_count == 1


def test_default_client_factory_disables_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    observer = LangfuseMessageObserver.from_default_client()

    assert observer.enabled is False


def test_trace_registry_closes_and_flushes() -> None:
    client = FakeClient()
    registry = LangfuseTraceRegistry(
        client,
        model="model",
        session_id="run-session",
        metadata={"run_id": "run"},
    )
    observer = registry.observer("acceptance.test", {"role": "judge"})
    observer.close(is_error=False, status_message="agent_result")

    snapshot = registry.evidence()
    assert snapshot == LangfuseRegistryEvidence(
        passed=True,
        enabled=True,
        observer_count=1,
        error_count=0,
        leftover_open_count=0,
    )
    assert client.flushes == 0

    evidence = registry.close_and_flush()
    second = registry.close_and_flush()

    assert observer.session.metadata["run_id"] == "run"
    assert observer.session.metadata["role"] == "judge"
    assert observer.session.metadata["langfuse_session_id"] == "run-session"
    assert evidence == LangfuseRegistryEvidence(
        passed=True,
        enabled=True,
        observer_count=1,
        error_count=0,
        leftover_open_count=0,
    )
    assert second == evidence
    assert client.flushes == 1
    with pytest.raises(RuntimeError, match="after close_and_flush"):
        registry.observer("too-late")


def test_trace_registry_marks_leftover_open_observers() -> None:
    client = FakeClient()
    registry = LangfuseTraceRegistry(client)
    registry.observer("acceptance.open")

    evidence = registry.close_and_flush()

    assert evidence.leftover_open_count == 1
    assert evidence.passed is False
    assert client.flushes == 1


def test_require_authenticated_client_requires_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        require_authenticated_langfuse_client()


def test_require_authenticated_client_rejects_failed_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnauthenticatedLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def auth_check(self) -> bool:
            return False

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr("llm.tracing.langfuse.Langfuse", UnauthenticatedLangfuse)

    with pytest.raises(RuntimeError, match="authentication failed"):
        require_authenticated_langfuse_client()


@pytest.mark.asyncio
async def test_agent_closes_open_langfuse_observations_without_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    observer = LangfuseMessageObserver(client)

    async def stream() -> Any:
        yield AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Read", input={})],
            model="test-model",
        )

    monkeypatch.setattr(
        agent_module,
        "query",
        lambda **_: stream(),
    )

    result = await Agent(
        model="test-model",
        message_observer=observer,
        settings=AgentSettings(sandbox=False),
    ).run("hello")

    assert result.is_error is True
    assert observer.closed is True
    assert client.generation.children[0].end_count == 1
    assert client.generation.end_count == 1


# ---------------------------------------------------------------------------
# TRC-11..20 These target the
# LangfuseSink.trace_id/adopt_run_id behaviour, extra_sinks fan-out via
# CompositeTraceSink, and close_and_flush()'s new central-logger flush call —
# none of which a bare LangfuseSink/LangfuseMessageObserver provides.
# ---------------------------------------------------------------------------


# TRC-11
def test_standalone_sink_generates_fresh_ids_and_only_adopts_a_valid_bound_run_id() -> None:
    client = FakeClient()
    standalone = LangfuseSink(client)

    standalone.start(start_event())
    first_id = standalone.trace_id
    assert re.fullmatch(r"[0-9a-f]{32}", first_id)
    assert client.started[-1]["trace_context"] == {"trace_id": first_id}

    with bound(run_id="a" * 32):
        standalone.start(start_event())
    # A standalone sink (adopt_run_id defaults to False) never adopts, even
    # when a valid run_id happens to be bound.
    second_id = standalone.trace_id
    assert second_id != "a" * 32
    assert re.fullmatch(r"[0-9a-f]{32}", second_id)
    assert second_id != first_id

    adopting = LangfuseSink(client, adopt_run_id=True)
    with bound(run_id="b" * 32):
        adopting.start(start_event())
    assert adopting.trace_id == "b" * 32
    assert client.started[-1]["trace_context"] == {"trace_id": "b" * 32}

    with bound(run_id="not-a-valid-hex-id"):
        adopting.start(start_event())
    assert adopting.trace_id != "not-a-valid-hex-id"
    assert re.fullmatch(r"[0-9a-f]{32}", adopting.trace_id)


# TRC-12
def test_observer_run_id_matches_the_langfuse_trace_id_on_every_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = FakeClient()
    observer = LangfuseMessageObserver(client, extra_sinks=(LoggingSink(),))

    observer(
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Read", input={})],
            model="test-model",
        )
    )
    observer(
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={"input_tokens": 3, "output_tokens": 5},
            result="final",
        )
    )

    expected_trace_id = client.started[0]["trace_context"]["trace_id"]
    assert expected_trace_id == observer.trace_id

    logged_run_ids = {record.run_id for record in caplog.records if hasattr(record, "run_id")}
    assert logged_run_ids == {expected_trace_id}


# TRC-13
def test_two_runs_on_one_observer_get_distinct_trace_ids_and_records_split_by_run_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = FakeClient()
    observer = LangfuseMessageObserver(client, extra_sinks=(LoggingSink(),))

    trace_ids: list[str] = []
    for session_id in ("session-1", "session-2"):
        observer(
            ResultMessage(
                subtype="success",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                usage={},
            )
        )
        trace_ids.append(observer.trace_id)

    assert len(set(trace_ids)) == 2
    assert len(client.started) == 2
    assert client.started[0]["trace_context"]["trace_id"] == trace_ids[0]
    assert client.started[1]["trace_context"]["trace_id"] == trace_ids[1]

    close_run_ids = [record.run_id for record in events(caplog, "trace.close") if hasattr(record, "run_id")]
    assert close_run_ids == trace_ids


# TRC-14
def test_no_extra_sinks_preserves_todays_exact_single_sink_behaviour() -> None:
    client = FakeClient()
    observer = LangfuseMessageObserver(client)

    assert isinstance(observer.session.sink, LangfuseSink)
    assert observer.session.sink.client is client
    # Nothing bound yet: constructing the observer must not start a trace.
    assert current_run_id() is None
    assert current_agent_id() is None
    assert client.started == []


# TRC-15
def test_client_none_with_extra_sinks_stays_enabled_and_records_without_langfuse(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    observer = LangfuseMessageObserver(None, extra_sinks=(LoggingSink(),))
    assert observer.enabled is True

    observer(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={},
        )
    )

    assert events(caplog, "trace.close")
    assert observer.error_count == 0

    caplog.clear()
    from_factory = LangfuseMessageObserver.from_default_client(extra_sinks=(LoggingSink(),))
    assert from_factory.enabled is True

    from_factory(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={},
        )
    )
    assert events(caplog, "trace.close")
    assert from_factory.error_count == 0


# TRC-16
def test_construction_with_extras_never_calls_the_langfuse_client() -> None:
    class _UnusedClient:
        def start_observation(self, **_kwargs: Any) -> Any:
            raise AssertionError("must not call the client at construction time")

        def flush(self) -> None:
            return None

        def auth_check(self) -> bool:
            return True

    # Construction alone (no message processed yet) must never touch the
    # client.
    observer = LangfuseMessageObserver(_UnusedClient(), extra_sinks=(LoggingSink(),))

    assert observer.enabled is True


# TRC-17
def test_registry_rejects_the_same_sink_instance_reused_across_observers() -> None:
    client = FakeClient()
    registry = LangfuseTraceRegistry(client)
    shared_sink = LoggingSink()

    registry.observer("first", extra_sinks=(shared_sink,))

    with pytest.raises(ValueError, match="already given"):
        registry.observer("second", extra_sinks=(shared_sink,))


# TRC-18
def test_close_and_flush_flushes_the_central_logger_once_after_client_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "logger.logger.flush",
        lambda *_args, **_kwargs: calls.append("flush") or True,
        raising=False,
    )
    client = FakeClient()
    registry = LangfuseTraceRegistry(client)
    registry.observer("acceptance.test")

    registry.close_and_flush()

    assert calls == ["flush"]
    assert client.flushes == 1


def test_close_and_flush_still_flushes_the_central_logger_when_client_flush_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "logger.logger.flush",
        lambda *_args, **_kwargs: calls.append("flush") or True,
        raising=False,
    )

    class RaisingFlushClient(FakeClient):
        def flush(self) -> None:
            self.flushes += 1
            raise RuntimeError("flush failed")

    client = RaisingFlushClient()
    registry = LangfuseTraceRegistry(client)
    registry.observer("acceptance.test")

    with pytest.raises(RuntimeError, match="flush failed"):
        registry.close_and_flush()

    assert calls == ["flush"]
    assert client.flushes == 1


# TRC-19
def test_langfuse_failures_are_isolated_from_the_logging_sink_end_to_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    generation = FakeObservation(fail_update=True)
    client = FakeClient(generation=generation)
    observer = LangfuseMessageObserver(client, extra_sinks=(LoggingSink(),))

    observer(
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Read", input={})],
            model="test-model",
        )
    )
    observer(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-1",
            usage={"input_tokens": 1, "output_tokens": 1},
            result="ok",
        )
    )

    assert observer.closed is True
    assert observer.error_count > 0
    logged_events = {record.event for record in caplog.records if hasattr(record, "event")}
    assert {"trace.start", "tool.start", "tool.abandoned", "trace.result", "trace.close"} <= (logged_events)


# TRC-20
@pytest.mark.asyncio
async def test_agent_run_propagates_one_run_id_to_every_sink_and_app_log_record(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    client = FakeClient()
    observer = LangfuseMessageObserver(client, extra_sinks=(LoggingSink(),))

    async def stream() -> Any:
        yield AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Read", input={})],
            model="test-model",
        )
        central_logger.log_event(
            central_logger.get_logger(__name__),
            central_logger.INFO,
            "app.mid_run",
            "app log mid-run",
        )

    monkeypatch.setattr(agent_module, "query", lambda **_: stream())

    result = await Agent(
        model="test-model",
        message_observer=observer,
        settings=AgentSettings(sandbox=False),
    ).run("hello")

    assert result.is_error is True
    assert observer.closed is True

    run_id = observer.trace_id
    assert run_id is not None

    for event_name in ("trace.start", "tool.start", "tool.abandoned", "trace.close"):
        matching = events(caplog, event_name)
        assert matching, f"missing {event_name}"
        assert all(getattr(record, "run_id", None) == run_id for record in matching)

    app_records = events(caplog, "app.mid_run")
    assert app_records
    assert app_records[0].run_id == run_id

    caplog.clear()
    central_logger.log_event(
        central_logger.get_logger(__name__),
        central_logger.INFO,
        "app.after_run",
        "app log after run",
    )
    after_records = events(caplog, "app.after_run")
    assert after_records
    assert getattr(after_records[0], "run_id", None) is None
