"""Tier 1 (+ one tier-2 check): the S7 follow-up fixes.

Summarize model allow-list, filename classes, database outage as 503, the
``Allow`` header on 405, the uvicorn server header and the storage prime.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import replace

import asyncpg
import httpx
import pytest
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)

from api import server
from api.app import PRIME_TIMEOUT_SECONDS, create_app
from api.runtime import Runtime
from db.session import build_engine, build_session_factory
from health import Readiness
from logger_harness import events, fields
from settings import load_settings
from specflow_api_support import (
    PDF,
    FakeDriver,
    FakeRepo,
    FakeTemporal,
    bearer,
)
from specflow_live import requires_live
from test_specflow_api_workflows import post, row, uploaded

# ---- 1. the summarize model ------------------------------------------------


async def parsed(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    return pid, did


async def test_an_absent_model_uses_agent_model(harness):
    pid, did = await parsed(harness)
    assert (await post(harness, pid, did, "summarize", json={})).status_code == 202
    assert harness.temporal.calls[0]["arg"].model == "test/model"


@pytest.mark.parametrize("model", ["other/model", "test/model ", " test/model", "TEST/MODEL", ""])
async def test_a_different_model_is_422_model_not_allowed(harness, model):
    pid, did = await parsed(harness)
    response = await post(harness, pid, did, "summarize", json={"model": model})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "model_not_allowed"
    assert model.strip() not in error["message"] or not model.strip()
    assert "other" not in str(response.json())
    assert not harness.temporal.calls
    assert row(harness, did).summarization_status != "running"


@pytest.mark.parametrize("model", ["x" * 200_000, "x" * 201, "a\r\nb", "a\nb", "a\x00b", "a\x7fb", "a\x85b"])
async def test_an_oversized_or_control_character_model_is_422_validation(
    harness,
    model,
):
    pid, did = await parsed(harness)
    response = await post(harness, pid, did, "summarize", json={"model": model})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert model not in response.text
    assert not harness.temporal.calls


async def test_the_workflow_input_never_holds_a_client_string(harness):
    pid, did = await parsed(harness)
    await post(harness, pid, did, "summarize", json={"model": "test/model"})
    [call] = harness.temporal.calls
    assert call["arg"].model is harness.runtime.settings.agent_run.model or (
        call["arg"].model == harness.runtime.settings.agent_run.model
    )
    harness.temporal.calls.clear()
    harness.runtime.settings = replace(
        harness.runtime.settings,
        agent_run=harness.runtime.settings.agent_run.model_copy(update={"model": "server/chosen"}),
    )
    row(harness, did).summarization_status = "idle"
    rejected = await post(harness, pid, did, "summarize", json={"model": "test/model"})
    assert rejected.status_code == 422  # only the server's current value is allowed
    assert (await post(harness, pid, did, "summarize", json={})).status_code == 202
    assert harness.temporal.calls[0]["arg"].model == "server/chosen"


async def test_the_client_model_is_not_logged(harness, caplog):
    pid, did = await parsed(harness)
    hidden_model = "sneaky/model-name"
    with caplog.at_level("DEBUG"):
        await post(harness, pid, did, "summarize", json={"model": hidden_model})
    assert hidden_model not in caplog.text


# ---- 2. filenames ----------------------------------------------------------


async def upload_named(harness, pid, filename):
    return await harness.client.post(
        f"/api/v1/projects/{pid}/documents",
        params={"filename": filename},
        content=PDF,
        headers={**bearer(), "Content-Type": "application/pdf"},
    )


async def project(harness) -> str:
    response = await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer())
    return response.json()["id"]


@pytest.mark.parametrize(
    "filename",
    [
        "a\u202etxt.pdf",  # right-to-left override (Cf)
        "a\u200bb.pdf",  # zero-width space (Cf)
        "a\u2066b.pdf",  # bidi isolate (Cf)
        "a\u0085b.pdf",  # next line (Cc)
        "a\u2028b.pdf",  # line separator (Zl)
        "a\u2029b.pdf",  # paragraph separator (Zp)
        "a\tb.pdf",
        "..",
        ".",
        "x/..",
    ],
)
async def test_unsafe_filenames_are_422(harness, filename):
    pid = await project(harness)
    response = await upload_named(harness, pid, filename)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert not harness.repo.documents


@pytest.mark.parametrize(
    "filename",
    ["zażółć gęślą jaźń.pdf", "報告書 2026.pdf", "Ünïcode-ñame.pdf", "a b.pdf"],
)
async def test_international_filenames_are_accepted(harness, filename):
    pid = await project(harness)
    response = await upload_named(harness, pid, filename)
    assert response.status_code == 201
    assert response.json()["document"]["original_filename"] == filename


# ---- 3. database outage ----------------------------------------------------


def raising_factory(error: BaseException):
    class Session:
        async def __aenter__(self):
            raise error

        async def __aexit__(self, *exc):
            return None

    return Session


def operational() -> OperationalError:
    return OperationalError("SELECT secret_host", {}, OSError("host=db.internal pw=hunter2"))


def invalidated() -> DBAPIError:
    error = DBAPIError("SELECT 1", {}, Exception("driver text"))
    error.connection_invalidated = True
    return error


async def runtime_with(harness, error):
    harness.runtime.session_factory = raising_factory(error)


@pytest.mark.parametrize(
    "error",
    [
        operational(),
        invalidated(),
        ConnectionRefusedError("refused host=db.internal"),
        OSError("unreachable"),
        TimeoutError("slow"),
    ],
)
async def test_database_connectivity_failures_are_503(harness, error, caplog):
    await runtime_with(harness, error)
    with caplog.at_level("DEBUG"):
        response = await harness.client.get("/api/v1/me", headers=bearer())
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "database_unavailable",
            "message": "The database is unavailable. Try again later.",
            "details": {},
        }
    }
    for leaked in ("db.internal", "hunter2", "driver text", "secret_host"):
        assert leaked not in response.text and leaked not in caplog.text
    [record] = events(caplog, "api.database.unavailable")
    assert fields(record)["error_type"] == type(error).__name__


@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError("SELECT 1", {}, Exception("bad sql")),
        IntegrityError("INSERT", {}, Exception("dup")),
        DBAPIError("SELECT 1", {}, Exception("plain")),
        ValueError("a genuine bug"),
        KeyError("x"),
    ],
)
async def test_programming_errors_stay_500(harness, error):
    await runtime_with(harness, error)
    response = await harness.client.get("/api/v1/me", headers=bearer())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


async def test_asyncpg_connection_errors_are_503(harness):

    await runtime_with(harness, asyncpg.exceptions.ConnectionDoesNotExistError("gone"))
    response = await harness.client.get("/api/v1/me", headers=bearer())
    assert response.status_code == 503


@requires_live("POSTGRES")
async def test_a_real_unreachable_database_is_503(configured_database, valid_env):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    unreachable = configured_database.model_copy(update={"host": "127.0.0.1", "port": closed_port})
    engine = build_engine(unreachable)
    settings = replace(load_settings("api", valid_env), database=unreachable)

    async def build(settings):
        return Runtime(
            settings=settings,
            session_factory=build_session_factory(engine),
            store=None,
            temporal=FakeTemporal(),
            driver=FakeDriver(),
            readiness=Readiness({}, ttl=0),
        )

    app = create_app(settings, runtime_factory=build)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/projects", headers=bearer())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"
        assert str(closed_port) not in response.text
    finally:
        await engine.dispose()


# ---- 4. Allow header -------------------------------------------------------


async def test_a_405_keeps_the_allow_header(harness):
    response = await harness.client.post("/api/v1/me", headers=bearer())
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    allowed = {m.strip() for m in response.headers["allow"].split(",")}
    assert allowed == {"GET"}


async def test_a_401_keeps_www_authenticate(harness):
    response = await harness.client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# ---- 5. server header ------------------------------------------------------


def test_the_uvicorn_config_hides_the_server_header_and_keeps_date(monkeypatch, valid_env):
    seen: dict = {}

    class FakeServer:
        def __init__(self, config) -> None:
            seen["config"] = config

        def run(self) -> None:
            return None

    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    server.run(load_settings("api", valid_env))
    assert seen["config"].server_header is False
    assert seen["config"].date_header is True


# ---- 6. storage prime ------------------------------------------------------


class Store:
    def __init__(self, behaviour) -> None:
        self.behaviour, self.calls = behaviour, 0

    async def check(self) -> None:
        self.calls += 1
        await self.behaviour()


async def start_with(store, valid_env, monkeypatch, prime_timeout=None):
    if prime_timeout is not None:
        monkeypatch.setattr("api.app.PRIME_TIMEOUT_SECONDS", prime_timeout)
    started: list[bool] = []

    async def factory(settings):
        return Runtime(
            settings=settings,
            session_factory=FakeRepo(),
            store=store,
            temporal=FakeTemporal(),
            driver=FakeDriver(),
            readiness=Readiness({}),
        )

    app = create_app(load_settings("api", valid_env), runtime_factory=factory)
    async with app.router.lifespan_context(app):
        started.append(True)
    return started


async def test_the_storage_is_primed_once_at_startup(monkeypatch, valid_env):
    async def fine() -> None:
        return None

    store = Store(fine)
    assert await start_with(store, valid_env, monkeypatch) == [True]
    assert store.calls == 1


async def test_a_failing_prime_does_not_stop_the_start(monkeypatch, valid_env):
    async def broken() -> None:
        raise RuntimeError("bucket secret-detail")

    assert await start_with(Store(broken), valid_env, monkeypatch) == [True]


async def test_a_slow_prime_is_cut_off_and_does_not_stop_the_start(monkeypatch, valid_env):
    async def slow() -> None:
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    began = loop.time()
    assert await start_with(Store(slow), valid_env, monkeypatch, 0.05) == [True]
    assert loop.time() - began < 2


async def test_a_missing_store_does_not_stop_the_start(monkeypatch, valid_env):
    assert await start_with(None, valid_env, monkeypatch) == [True]


def test_the_prime_timeout_stays_within_five_seconds():

    assert PRIME_TIMEOUT_SECONDS <= 5.0
