"""Tier 1 tests of the worker liveness endpoint (``GET /healthz``) and its place in the runner."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket

import pytest

from runtime_defaults_support import RUNTIME_DEFAULTS
from settings import ConfigurationError, load_settings
from temporal import health_server, worker
from test_specflow_temporal_core_worker import FakeWorker, _until

# The ``fake_workers`` and ``runner_env`` fixtures live in the core-worker test module.
pytest_plugins = ["test_specflow_temporal_core_worker"]

LOCAL = "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOCAL, 0))
        return int(probe.getsockname()[1])


def _get(port: int, path: str = "/healthz", method: str = "GET") -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(LOCAL, port, timeout=2)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.fixture
def server():
    health = health_server.start(0, host=LOCAL)
    assert health is not None
    yield health
    health.close()


def test_healthz_answers_200_with_a_json_status(server):
    status, body = _get(server.port)

    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_healthz_ignores_a_query_string_and_answers_head_without_a_body(server):
    assert _get(server.port, "/healthz?probe=1")[0] == 200
    assert _get(server.port, method="HEAD") == (200, b"")


def test_any_other_path_is_404(server):
    assert _get(server.port, "/readyz")[0] == 404
    assert _get(server.port, "/")[0] == 404


def test_close_frees_the_port():
    health = health_server.start(0, host=LOCAL)
    assert health is not None
    port = health.port
    health.close()

    again = health_server.start(port, host=LOCAL)
    assert again is not None
    again.close()


def test_a_port_in_use_is_logged_and_not_fatal(caplog):
    caplog.set_level("ERROR", logger="temporal.health_server")
    with socket.socket() as taken:
        taken.bind((LOCAL, 0))
        taken.listen()

        assert health_server.start(taken.getsockname()[1], host=LOCAL) is None

    record = next(r for r in caplog.records if r.getMessage() == "worker.health.unavailable")
    assert record.error_type == "OSError"


def test_the_port_defaults_to_8001_and_is_validated(valid_env):
    default_port = RUNTIME_DEFAULTS["WORKER_HEALTH_PORT"]
    assert load_settings("worker", valid_env).app.worker_health_port == default_port
    assert load_settings("worker", {**valid_env, "WORKER_HEALTH_PORT": ""}).app.worker_health_port == default_port
    assert load_settings("worker", {**valid_env, "WORKER_HEALTH_PORT": "9101"}).app.worker_health_port == 9101

    with pytest.raises(ConfigurationError) as raised:
        load_settings("worker", {**valid_env, "WORKER_HEALTH_PORT": "0"})
    assert raised.value.variables == ("WORKER_HEALTH_PORT",)


async def test_the_runner_serves_healthz_while_running_and_closes_it_after(runner_env):
    port = _free_port()
    settings = load_settings("worker", {**runner_env.env, "WORKER_HEALTH_PORT": str(port)})
    task = asyncio.create_task(worker.run(settings))
    try:
        await _until(FakeWorker.events, lambda: any(event.startswith("run:") for event in FakeWorker.events))
        status, _ = await asyncio.to_thread(_get, port)
        assert status == 200
    finally:
        for instance in FakeWorker.instances:
            await instance.shutdown()  # ends worker.run like a stopped poller
        await asyncio.wait_for(task, 5)

    with pytest.raises(ConnectionRefusedError):
        _get(port)
