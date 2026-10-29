"""Tier 1: failure paths of the worker runner (missing registration, failing cleanup)."""

from __future__ import annotations

from agent_runtime.tracing import TracingHandle
from settings import load_settings
from temporal import registry, worker
from temporal.registry import Registration


async def test_a_missing_registration_module_fails_before_connecting(valid_env, monkeypatch):
    opened: list[str] = []

    async def open_connection(settings):
        opened.append("connect")

    monkeypatch.setattr(worker, "open_connection", open_connection)

    def missing(name):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(registry.importlib, "import_module", missing)
    monkeypatch.setattr(
        worker,
        "load_registration",
        lambda queue, settings: registry.load_registration(queue, settings, import_module=missing),
    )

    # R-code LOW 9: not raised any more; logged, cleaned up, exit code 1.
    assert await worker.run(load_settings("worker", valid_env)) == 1
    assert opened == []


async def test_shutdown_in_order_survives_a_failing_worker_and_registration():
    events: list[str] = []

    class Bad:
        async def shutdown(self):
            raise RuntimeError("boom")

    async def bad_close():
        raise RuntimeError("boom")

    class Handle(TracingHandle):
        def shutdown(self):
            events.append("tracing")

    await worker.shutdown_in_order(
        [Bad()],
        [],
        Handle(enabled=True),
        registrations=[Registration([], [], close=bad_close)],
    )

    assert events == ["tracing"]
