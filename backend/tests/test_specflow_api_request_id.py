"""Tier 1: each API request logs under its own ``request_id`` (X-Request-ID).

The records are read from the real JSON stdout pipeline (``central_log``),
because the correlation ids are injected there, not on ``caplog`` records.
"""

from __future__ import annotations

import asyncio
import re
import threading

import httpx
import pytest

from api.app import create_app
from api.runtime import Runtime
from drive.fake import FakeDriveGateway
from health import Readiness
from logger.correlation import current_request_id, new_request_context
from logger.logger import INFO, get_logger, log_event
from settings import load_settings
from specflow_api_support import FakeDriver, FakeTemporal, bearer
from specflow_sources_support import SYNTHETIC, new_project
from storage.memory import InMemoryObjectStore
from test_specflow_api_fakes import FakeRepo

thread_logger = get_logger("tests.request_id")
HEX_ID = r"[0-9a-f]{32}"


@pytest.fixture
def logs(central_log):
    central_log.configure()
    return central_log


class MeetingGateway(FakeDriveGateway):
    """Holds each folder check until two run at once, then logs from the thread."""

    def __init__(self) -> None:
        super().__init__(app_env="local", file_delay_s=0)
        self.both = threading.Barrier(2, timeout=5)

    def check_folder(self, folder_id):
        self.both.wait()
        log_event(thread_logger, INFO, "tests.drive.thread")
        return super().check_folder(folder_id)


async def test_concurrent_requests_log_under_their_own_ids(logs, sources):
    await new_project(sources)
    sources.harness.runtime.drive = MeetingGateway()
    first, second = await asyncio.gather(sources.test(SYNTHETIC), sources.test(SYNTHETIC))
    assert first.status_code == second.status_code == 200
    ids = {first.headers["x-request-id"], second.headers["x-request-id"]}
    assert len(ids) == 2 and all(re.fullmatch(HEX_ID, i) for i in ids)
    tested = logs.events("api.drive.tested")
    in_threads = logs.events("tests.drive.thread")
    assert {r["request_id"] for r in tested} == ids and len(tested) == 2
    assert {r["request_id"] for r in in_threads} == ids  # every thread line, both ids


@pytest.mark.parametrize("inbound", ["abc-123", "Trace.0_9", "a" * 64])
async def test_a_safe_inbound_id_is_kept(logs, harness, inbound):
    response = await harness.client.get("/healthz", headers={"X-Request-ID": inbound})
    assert response.headers["x-request-id"] == inbound


@pytest.mark.parametrize("inbound", ["", "a b", "a\nb", "a" * 65, "id;drop", "żółw"])
async def test_an_unsafe_inbound_id_is_replaced(logs, harness, inbound):
    response = await harness.client.get("/healthz", headers={"X-Request-ID": inbound.encode()})
    assert re.fullmatch(HEX_ID, response.headers["x-request-id"])


async def test_an_unhandled_error_logs_and_answers_with_the_request_id(
    logs,
    harness,
):
    async def boom() -> None:
        raise RuntimeError("boom")

    harness.app.add_api_route("/boom", boom)
    response = await harness.client.get("/boom")
    assert response.status_code == 500
    [failed] = logs.events("api.request.failed")
    assert failed["request_id"] == response.headers["x-request-id"]


async def test_startup_lines_keep_the_process_id(logs, valid_env, monkeypatch):
    repo = FakeRepo()
    repo.install(monkeypatch)
    settings = load_settings("api", valid_env)

    async def ok() -> None:
        return None

    async def factory(settings):
        return Runtime(
            settings=settings,
            session_factory=repo,
            store=InMemoryObjectStore(app_env="local"),
            temporal=FakeTemporal(),
            driver=FakeDriver(),
            readiness=Readiness({"database": ok}, ttl=0),
        )

    app = create_app(settings, runtime_factory=factory)
    with new_request_context("process-id"):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/api/v1/drive/identity", headers=bearer())
            assert current_request_id() == "process-id"
    assert response.status_code == 200
    assert response.headers["x-request-id"] != "process-id"
    for event in ("drive.gateway.selected", "api.startup.completed"):
        [line] = logs.events(event)
        assert line["request_id"] == "process-id"
