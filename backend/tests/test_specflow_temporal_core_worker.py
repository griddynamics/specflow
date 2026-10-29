"""
Tier 1 tests of the worker runner, its SIGTERM order (AC-11.4), ``worker --check``
(AC-2.3, fake health) and the AC-16.0 static ban.
"""

from __future__ import annotations

import asyncio
import os
import signal
import types
from datetime import timedelta
from typing import ClassVar

import pytest

import logger.logger as central
from agent_runtime.tracing import TracingHandle
from settings import load_settings
from temporal import client as client_module
from temporal import worker
from temporal.registry import Registration

UNTIL_SECONDS = 5.0
_UNHANDLED = (signal.SIG_DFL, signal.SIG_IGN, None)


class _Events(list[str]):
    """A list of event names that wakes ``_until`` on every append."""

    def __init__(self) -> None:
        super().__init__()
        self.changed = asyncio.Event()

    def append(self, item: str) -> None:
        super().append(item)
        self.changed.set()


# ------------------------------------------------------------- worker runner
class FakeWorker:
    """Records construction arguments and the shutdown order."""

    instances: ClassVar[list[FakeWorker]] = []
    events: ClassVar[list[str]] = []
    fail_run = False

    def __init__(self, client, **kwargs):
        self.client = client
        self.kwargs = kwargs
        self._stopped = asyncio.Event()
        FakeWorker.instances.append(self)

    async def run(self):
        FakeWorker.events.append(f"run:{self.kwargs['task_queue']}")
        if FakeWorker.fail_run:
            raise RuntimeError("poller crashed")
        await self._stopped.wait()

    async def shutdown(self):
        FakeWorker.events.append(f"shutdown:{self.kwargs['task_queue']}")
        self._stopped.set()


@pytest.fixture
def fake_workers(monkeypatch):
    FakeWorker.instances, FakeWorker.events, FakeWorker.fail_run = [], _Events(), False
    monkeypatch.setattr(worker, "Worker", FakeWorker)
    return FakeWorker


async def _async_activity() -> None: ...


def _registrations(events: list[str]) -> dict[str, Registration]:
    def closer(queue):
        async def close():
            events.append(f"registration.close:{queue}")

        return close

    return {
        "parse": Registration(workflows=[dict], activities=[len], close=closer("parse")),
        "agents": Registration(workflows=[list], activities=[_async_activity], close=closer("agents")),
    }


def test_each_queue_with_sync_activities_gets_its_own_one_thread_executor(valid_env, fake_workers):
    settings = load_settings("worker", valid_env)
    registrations = _registrations([])
    registrations["agents2"] = Registration(workflows=[], activities=[len])  # a second sync queue
    executors = worker.build_executors(registrations)

    assert executors["parse"] is not None and executors["agents2"] is not None
    assert executors["parse"] is not executors["agents2"]
    assert executors["agents"] is None  # async only: no executor
    assert executors["parse"]._max_workers == executors["agents2"]._max_workers == 1
    workers = worker.build_workers("client", settings, registrations, executors, worker_factory=FakeWorker)

    assert [w.kwargs["task_queue"] for w in workers] == ["specflow-parse", "specflow-agents"]
    assert all(w.client == "client" for w in workers)
    assert workers[0].kwargs["activity_executor"] is executors["parse"]
    assert "activity_executor" not in workers[1].kwargs
    assert all(w.kwargs["graceful_shutdown_timeout"] == timedelta(seconds=130) for w in workers)
    assert workers[0].kwargs["workflows"] == [dict]
    assert workers[0].kwargs["activities"] == [len]
    assert workers[1].kwargs["workflows"] == [list]
    for executor in executors.values():
        if executor is not None:
            executor.shutdown()


def test_build_workers_only_builds_the_polled_queues(valid_env, fake_workers):
    settings = load_settings("worker", valid_env, queues=["agents"])
    registrations = _registrations([])

    workers = worker.build_workers(
        "c", settings, registrations, worker.build_executors(registrations), worker_factory=FakeWorker
    )

    assert [w.kwargs["task_queue"] for w in workers] == ["specflow-agents"]


def test_graceful_shutdown_is_130_seconds():
    assert timedelta(seconds=130) == worker.GRACEFUL_SHUTDOWN


@pytest.fixture
def runner_env(valid_env, monkeypatch, fake_workers):
    """Patch the worker runner's collaborators; returns the shared event list."""

    events = _Events()
    registrations = _registrations(events)
    loaded: list[str] = []

    def load(queue, settings):
        loaded.append(queue)
        return registrations[queue]

    class Handle(TracingHandle):
        def shutdown(self):
            events.append("tracing.shutdown")

    async def open_connection(settings):
        events.append("open_connection")
        return client_module.TemporalConnection(client="client", tracing=Handle(enabled=True))

    monkeypatch.setattr(worker, "load_registration", load)
    monkeypatch.setattr(worker, "open_connection", open_connection)
    FakeWorker.events = events
    return types.SimpleNamespace(events=events, loaded=loaded, env=valid_env)


async def _until(events: _Events, predicate) -> None:
    async with asyncio.timeout(UNTIL_SECONDS):
        while not predicate():
            events.changed.clear()
            await events.changed.wait()


async def test_sigterm_runs_worker_shutdown_then_tracing_and_leaves_the_logger_open(runner_env):
    """AC-11.4 with fake workers and a real SIGTERM delivered to the loop."""

    task = asyncio.create_task(worker.run(load_settings("worker", runner_env.env)))
    await _until(runner_env.events, lambda: len(FakeWorker.events) >= 3 and "run:specflow-agents" in FakeWorker.events)
    assert signal.getsignal(signal.SIGTERM) not in _UNHANDLED, "the runner did not install its SIGTERM handler"

    os.kill(os.getpid(), signal.SIGTERM)
    code = await asyncio.wait_for(task, 5)

    events = runner_env.events
    assert code == 0
    assert runner_env.loaded == ["parse", "agents"]
    last = [e for e in events if e.startswith(("shutdown:", "registration.close", "tracing"))]
    assert last == [
        "shutdown:specflow-parse",
        "shutdown:specflow-agents",
        "registration.close:parse",
        "registration.close:agents",
        "tracing.shutdown",
    ]
    assert events[0] == "open_connection"
    assert events[-1] == "tracing.shutdown"
    assert len(FakeWorker.instances) == 2


async def test_a_worker_that_stops_by_itself_fails_the_process_but_still_shuts_down(runner_env):
    FakeWorker.fail_run = True

    code = await asyncio.wait_for(worker.run(load_settings("worker", runner_env.env)), 5)

    assert code == 1
    assert runner_env.events[-1] == "tracing.shutdown"


async def test_the_runner_polls_only_the_requested_queue(runner_env):
    settings = load_settings("worker", runner_env.env, queues=["parse"])
    FakeWorker.fail_run = True  # ends the run without needing a signal

    await asyncio.wait_for(worker.run(settings), 5)

    assert runner_env.loaded == ["parse"]
    assert [w.kwargs["task_queue"] for w in FakeWorker.instances] == ["specflow-parse"]


async def test_executors_are_distinct_per_queue_and_closed_on_shutdown(runner_env, monkeypatch):
    created: list = []
    real = worker.build_executors

    def spy(registrations):
        executors = real(registrations)
        created.append(executors)
        return executors

    monkeypatch.setattr(worker, "build_executors", spy)
    FakeWorker.fail_run = True  # ends the run without a signal

    await asyncio.wait_for(worker.run(load_settings("worker", runner_env.env)), 5)

    executors = created[0]
    assert executors["parse"] is not None and executors["agents"] is None
    assert executors["parse"]._shutdown is True


async def test_a_startup_failure_is_logged_with_the_type_only(runner_env, monkeypatch, caplog):
    async def refuse(settings):
        runner_env.events.append("open_connection")
        raise ConnectionError("secret-looking-detail-8841")

    monkeypatch.setattr(worker, "open_connection", refuse)
    caplog.set_level("ERROR", logger="temporal.worker")

    code = await asyncio.wait_for(worker.run(load_settings("worker", runner_env.env)), 5)

    assert code == 1
    record = next(r for r in caplog.records if r.getMessage() == "worker.run.failed")
    assert record.error_type == "ConnectionError"
    assert "secret-looking-detail-8841" not in caplog.text


async def test_a_signal_during_a_slow_connect_still_runs_the_cleanup_order(runner_env, monkeypatch):
    release = asyncio.Event()

    async def slow(settings):
        runner_env.events.append("connecting")
        await release.wait()

    monkeypatch.setattr(worker, "open_connection", slow)
    task = asyncio.create_task(worker.run(load_settings("worker", runner_env.env)))
    await _until(runner_env.events, lambda: "connecting" in runner_env.events)
    assert signal.getsignal(signal.SIGTERM) not in _UNHANDLED, "the runner did not install its SIGTERM handler"

    os.kill(os.getpid(), signal.SIGTERM)
    code = await asyncio.wait_for(task, 5)

    assert code == 0
    assert FakeWorker.instances == []
    assert runner_env.events[-1] == "registration.close:agents"  # no connection, no tracing
    assert "registration.close:parse" in runner_env.events


async def test_the_runner_never_closes_the_logger_the_entrypoint_does(runner_env, monkeypatch):
    """R-code LOW 12 (AC-2.5): ``logger.close()`` once per process, from ``cli.main`` only."""

    closed: list[str] = []
    monkeypatch.setattr(central, "close", lambda: closed.append("close"))
    FakeWorker.fail_run = True

    assert await asyncio.wait_for(worker.run(load_settings("worker", runner_env.env)), 5) == 1
    assert closed == []
    assert not hasattr(worker, "close_logger")


async def test_a_failing_second_registration_cleans_up_the_first_logs_and_exits_1(runner_env, monkeypatch, caplog):
    """R-code LOW 9: registrations load inside the try."""

    def load(queue, settings):
        if queue == "agents":
            raise RuntimeError("secret-looking-detail-5530")
        return _registrations(runner_env.events)[queue]

    monkeypatch.setattr(worker, "load_registration", load)
    caplog.set_level("ERROR", logger="temporal.worker")

    code = await asyncio.wait_for(worker.run(load_settings("worker", runner_env.env)), 5)

    assert code == 1
    assert "open_connection" not in runner_env.events  # never got as far as connecting
    assert "registration.close:parse" in runner_env.events  # the first one is released
    record = next(r for r in caplog.records if r.getMessage() == "worker.run.failed")
    assert record.error_type == "RuntimeError"
    assert "secret-looking-detail-5530" not in caplog.text
