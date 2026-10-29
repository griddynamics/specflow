"""Tier 2 (``SPECFLOW_TEST_TEMPORAL=1``): the Temporal core against the live
local ``specflow-local`` namespace.

Each test uses its own task queues and a ``pytest-<run>-`` workflow-id prefix
(``temporal_live``), and terminates what it started. Nothing is downloaded and
no test server is started (AC-16.0). No LLM gateway or Langfuse call is made.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta
from uuid import uuid4

import pytest
from agents import add_trace_processor, set_trace_processors, set_tracing_disabled
from agents.tracing import TracingProcessor
from temporalio.client import Client
from temporalio.openai_agents.testing import ResponseBuilders, TestModel, TestModelProvider
from temporalio.worker import Worker

from agent_runtime import plugin as plugin_module
from agent_runtime import tracing
from settings import load_settings
from specflow_live import requires_live
from specflow_temporal_core_support import (
    STATE,
    EchoWorkflow,
    ModelAgentWorkflow,
    SlowWorkflow,
    echo_activity,
    slow_activity,
)
from temporal import ids, worker
from temporal.registry import Registration

pytestmark = requires_live("TEMPORAL")
CONNECT_SECONDS = 1.0
UNPOLLED_SECONDS = 1.5


def _env(valid_env, queue: str) -> dict[str, str]:
    return {
        **valid_env,
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        "TEMPORAL_NAMESPACE": "specflow-local",
        "TEMPORAL_QUEUE_PARSE": f"{queue}-parse",
        "TEMPORAL_QUEUE_AGENTS": f"{queue}-agents",
    }


@pytest.fixture
def isolated_tracing(monkeypatch):
    """Undo the process-wide tracing switches a test makes."""

    monkeypatch.setenv(tracing.DISABLE_ENV, "")
    monkeypatch.delenv(tracing.DISABLE_ENV)
    yield
    set_trace_processors([])
    set_tracing_disabled(False)
    # the env flag is restored by monkeypatch (set then unset above)


class CountingProcessor(TracingProcessor):
    def __init__(self) -> None:
        self.traces = 0
        self.spans = 0

    def on_trace_start(self, _trace) -> None:
        self.traces += 1

    def on_trace_end(self, trace) -> None:
        pass

    def on_span_start(self, _span) -> None:
        self.spans += 1

    def on_span_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


@pytest.fixture
def run_worker(valid_env, temporal_live, monkeypatch, isolated_tracing):
    """Start ``worker.run`` with fake registrations on this test's queues."""

    started: list[asyncio.Task] = []

    async def start(registrations, *, queues=None):
        settings = load_settings("worker", _env(valid_env, temporal_live.task_queue), queues=queues)
        monkeypatch.setattr(worker, "load_registration", lambda q, _s: registrations[q])
        task = asyncio.create_task(worker.run(settings))
        started.append(task)
        await asyncio.sleep(CONNECT_SECONDS)  # connect + first poll
        return settings, task

    yield start
    for task in started:
        if not task.done():
            task.cancel()


def _sigterm_handler_installed() -> bool:
    return signal.SIGTERM in getattr(asyncio.get_running_loop(), "_signal_handlers", {})


async def test_worker_polls_the_configured_queues_and_stops_gracefully(temporal_live, run_worker):
    """AC-2.2: both queues are polled; SIGTERM lets a running activity finish."""

    STATE.update(slow_started=asyncio.Event(), slow_cancelled=False, slow_finished=False)
    registrations = {
        "parse": Registration(workflows=[EchoWorkflow], activities=[echo_activity]),
        "agents": Registration(workflows=[SlowWorkflow], activities=[slow_activity]),
    }
    settings, task = await run_worker(registrations)
    assert _sigterm_handler_installed()
    client = temporal_live.client
    prefix = temporal_live.id_prefix

    parse_id = ids.parse_workflow_id(uuid4(), prefix=prefix)
    parse = temporal_live.track(
        await client.start_workflow(
            EchoWorkflow.run, "parse", id=parse_id, task_queue=f"{temporal_live.task_queue}-parse"
        )
    )
    assert await asyncio.wait_for(parse.result(), 20) == "echo:parse"

    slow_id = ids.summary_workflow_id(uuid4(), prefix=prefix)
    temporal_live.track(
        await client.start_workflow(SlowWorkflow.run, 2.0, id=slow_id, task_queue=f"{temporal_live.task_queue}-agents")
    )
    await asyncio.wait_for(STATE["slow_started"].wait(), 20)

    os.kill(os.getpid(), signal.SIGTERM)
    code = await asyncio.wait_for(task, 30)

    assert code == 0
    assert STATE["slow_finished"] is True
    assert STATE["slow_cancelled"] is False
    assert parse_id.startswith(prefix) and slow_id.startswith(prefix)
    assert settings.temporal.worker_queues == ("parse", "agents")


async def test_a_queue_option_polls_only_that_queue(temporal_live, run_worker):
    """Only the requested queue gets a poller: the other one never runs a task."""

    registrations = {
        "parse": Registration(workflows=[EchoWorkflow], activities=[echo_activity]),
        "agents": Registration(workflows=[EchoWorkflow], activities=[echo_activity]),
    }
    _, task = await run_worker(registrations, queues=["parse"])
    client = temporal_live.client

    polled = temporal_live.track(
        await client.start_workflow(
            EchoWorkflow.run,
            "x",
            id=ids.parse_workflow_id(uuid4(), prefix=temporal_live.id_prefix),
            task_queue=f"{temporal_live.task_queue}-parse",
        )
    )
    unpolled = temporal_live.track(
        await client.start_workflow(
            EchoWorkflow.run,
            "x",
            id=ids.summary_workflow_id(uuid4(), prefix=temporal_live.id_prefix),
            task_queue=f"{temporal_live.task_queue}-agents",
        )
    )
    assert await asyncio.wait_for(polled.result(), 20) == "echo:x"
    await asyncio.sleep(UNPOLLED_SECONDS)  # long enough for a poller to have picked it up
    description = await unpolled.describe()
    assert description.status.name == "RUNNING"
    history = [e async for e in unpolled.fetch_history_events()]
    assert not any(e.HasField("workflow_task_completed_event_attributes") for e in history)

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 30) == 0


# AC-11.2 (live).
async def _run_agent_workflow(settings, temporal_live) -> str:
    """The TestModel workflow on this test's queue, through the real plugin."""

    plugin = plugin_module.build_plugin(
        settings,
        model_provider=TestModelProvider(TestModel.returning_responses([ResponseBuilders.output_message("pong")])),
    )
    client = await Client.connect(settings.temporal.address, namespace=settings.temporal.namespace, plugins=[plugin])
    async with Worker(client, task_queue=temporal_live.task_queue, workflows=[ModelAgentWorkflow]):
        handle = await client.start_workflow(
            ModelAgentWorkflow.run,
            "ping",
            id=temporal_live.workflow_id(f"agent-{uuid4().hex[:8]}"),
            task_queue=temporal_live.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        temporal_live.track(handle)
        return await asyncio.wait_for(handle.result(), 60)


async def test_tracing_off_emits_no_traces_and_no_spans(valid_env, temporal_live, isolated_tracing):
    """AC-11.2: ``configure`` with Langfuse off, then a TestModel run: 0 traces, 0 spans."""

    settings = load_settings("worker", _env(valid_env, temporal_live.task_queue))
    counter = CountingProcessor()

    handle = tracing.configure(settings)
    add_trace_processor(counter)
    result = await _run_agent_workflow(settings, temporal_live)

    assert handle.enabled is False
    assert result == "pong"
    assert (counter.traces, counter.spans) == (0, 0)


async def test_the_counting_processor_does_see_traces_when_tracing_is_on(valid_env, temporal_live, isolated_tracing):
    """Control for the test above: the same counter and workflow, tracing not disabled."""

    settings = load_settings("worker", _env(valid_env, temporal_live.task_queue))
    counter = CountingProcessor()
    set_tracing_disabled(False)
    set_trace_processors([counter])

    result = await _run_agent_workflow(settings, temporal_live)

    assert result == "pong"
    assert counter.traces >= 1
    assert counter.spans >= 1
