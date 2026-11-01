"""The ArcadeDB probe drops the databases its session created, and only those (offline, no server)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

import graph_support
from graph.stores.arcadedb.admin import AdminClient
from graph.stores.config import StoreConfig
from graph_probes import arcadedb as probe_module
from graph_probes.arcadedb import ArcadeDbProbe, drop_session_databases
from graph_probes.base import GraphProbe

URI = "bolt://arcade.invalid:7687"
ADMIN = "http://arcade.invalid:2480"


class FakeServer:
    """Answers ``exists`` from a set of database names and records every drop command."""

    def __init__(self, *databases: str) -> None:
        self.databases = set(databases)
        self.commands: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/api/v1/exists/"):
            return httpx.Response(200, json={"result": request.url.path.rsplit("/", 1)[1] in self.databases})
        assert (request.method, request.url.path) == ("POST", "/api/v1/server"), request.url
        command = json.loads(request.content)["command"]
        self.commands.append(command)
        self.databases.discard(command.removeprefix("drop database "))
        return httpx.Response(200, json={"result": "ok"})


def probe_for(database: str, server: FakeServer) -> ArcadeDbProbe:
    config = StoreConfig.model_validate(
        {"backend": "arcadedb", "uri": URI, "user": "root", "password": SecretStr("synthetic"), "name": database}
    )
    probe = ArcadeDbProbe(config)
    probe._admin = AdminClient(ADMIN, "root", SecretStr("synthetic"), transport=server.transport())
    return probe


@pytest.fixture(autouse=True)
def empty_registry() -> Any:
    probe_module._CREATED.clear()
    yield
    probe_module._CREATED.clear()


def patched_transport(monkeypatch: pytest.MonkeyPatch, server: FakeServer) -> None:
    original = probe_module._SessionAdmin

    def build(url: str, user: str, password: SecretStr, *, transport: Any = None) -> Any:
        return original(url, user, password, transport=transport or server.transport())

    monkeypatch.setattr(probe_module, "_SessionAdmin", build)


async def test_a_database_the_session_created_is_dropped_at_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer("specflow")
    probe = probe_for("pytest_graph_1", server)
    await probe.before_session_schema()
    server.databases.add("pytest_graph_1")  # the schema setup creates it
    patched_transport(monkeypatch, server)
    assert await drop_session_databases() == ["pytest_graph_1"]
    assert server.commands == ["drop database pytest_graph_1"]
    assert server.databases == {"specflow"}
    assert await drop_session_databases() == []  # nothing is left to drop


async def test_a_database_that_existed_before_the_session_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer("shared_dev")
    await probe_for("shared_dev", server).before_session_schema()
    patched_transport(monkeypatch, server)
    assert await drop_session_databases() == []
    assert server.commands == [] and server.databases == {"shared_dev"}


async def test_specflow_is_never_noted_and_never_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer()  # even a missing specflow is not the session's to remove
    await probe_for("specflow", server).before_session_schema()
    assert probe_module._CREATED == {}
    probe_module._CREATED[(URI, "specflow")] = probe_for("specflow", server).config
    server.databases.add("specflow")
    patched_transport(monkeypatch, server)
    assert await drop_session_databases() == []
    assert server.commands == [] and server.databases == {"specflow"}


async def test_a_database_that_vanished_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer()
    await probe_for("pytest_graph_2", server).before_session_schema()
    patched_transport(monkeypatch, server)  # never created by the schema setup: nothing to drop
    assert await drop_session_databases() == []
    assert server.commands == []


async def test_a_probe_without_a_database_name_notes_nothing() -> None:
    server = FakeServer()
    config = StoreConfig.model_validate(
        {"backend": "arcadedb", "uri": URI, "user": "root", "password": SecretStr("synthetic")}
    )
    probe = ArcadeDbProbe(config)
    probe._admin = AdminClient(ADMIN, "root", SecretStr("synthetic"), transport=server.transport())
    await probe.before_session_schema()
    assert probe_module._CREATED == {}


async def test_the_session_schema_setup_notes_the_database_before_it_creates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def note() -> None:
        calls.append("note")

    async def setup(*_: Any, **__: Any) -> tuple[str, ...]:
        calls.append("setup")
        return ()

    probe = cast(
        GraphProbe,
        SimpleNamespace(
            backend="arcadedb",
            config=SimpleNamespace(uri="bolt://order.invalid:7687"),
            database="pytest_order",
            store=object(),
            before_session_schema=note,
        ),
    )
    monkeypatch.setattr(graph_support, "ensure_schema", setup)
    monkeypatch.setattr(graph_support, "shipped_ontology", lambda: None)
    monkeypatch.setattr(graph_support, "live_llm_config", lambda: None)
    monkeypatch.setattr(graph_support, "_SCHEMA_READY", set())
    await graph_support.ensure_session_schema(probe)
    await graph_support.ensure_session_schema(probe)
    assert calls == ["note", "setup"]  # once per store, the note first
