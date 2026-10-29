"""Offline tests of the graph core's Neo4j layer (plan 1.T4): version gate, ``Executor``, error mapping,
``check``, ``connect``.

The driver is a hand-written fake; nothing connects to Neo4j. Schema setup has its own offline
module (``test_graph_schema.py``); the ``ScriptedGateway`` self-tests live in ``test_graph_support.py``.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from neo4j.exceptions import (
    AuthError,
    ClientError,
    Forbidden,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

import graph.core.db as db_module
from graph.core import (
    ConfigurationError,
    ConformanceError,
    Executor,
    GraphWriteError,
    InfrastructureUnavailableError,
    Neo4jConfig,
    Query,
    check,
    connect,
    ensure_schema,
    require_version,
    server_version,
    validate_project_id,
)
from graph.core.queries import PING, SERVER_COMPONENTS
from graph_support import llm_config, shipped_ontology
from logger_harness import LogHarness

SCOPED = Query("scoped", "MATCH (n:SpecNode {group_id: $project_id}) RETURN n.uuid AS uuid")
UNBOUND = Query("unbound", "MATCH (n:SpecNode) RETURN n.uuid AS uuid")
LOOKALIKE = Query("lookalike", "MATCH (n {group_id: $project_ids}) RETURN n")

TRANSIENT_CODE = "Neo.TransientError.Transaction.DeadlockDetected"
CLIENT_CODE = "Neo.ClientError.Schema.ConstraintValidationFailed"
LEAK_PROBE = "message carrying secret-value-123"
PROBE_LOGIN = "pw-not-shown-42"
UNUSED_LOGIN = "pw"


def neo4j_error(cls: type[Exception], code: str) -> Exception:
    """A server error as the driver builds it (code and message)."""

    return cls._hydrate_neo4j(code=code, message=LEAK_PROBE)


def transient() -> Exception:
    return neo4j_error(TransientError, TRANSIENT_CODE)


def client_error() -> Exception:
    return neo4j_error(ClientError, CLIENT_CODE)


# Fake driver


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class FakeTransaction:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver
        self._closed = False

    async def run(self, text: str, params: dict[str, Any]) -> FakeResult:
        self._driver.runs.append((text, dict(params)))
        if self._driver.errors:
            error = self._driver.errors.pop(0)
            if error is not None:
                raise error
        return FakeResult([{"project_id": params.get("project_id")}])

    async def commit(self) -> None:
        self._closed = True
        self._driver.commits += 1

    async def rollback(self) -> None:
        self._closed = True
        self._driver.rollbacks += 1

    def closed(self) -> bool:
        return self._closed


class FakeSession:
    def __init__(self, driver: FakeDriver, options: dict[str, Any]) -> None:
        self._driver = driver
        driver.sessions.append(options)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute_write(self, work: Any) -> Any:
        self._driver.modes.append("write")
        return await work(FakeTransaction(self._driver))

    async def execute_read(self, work: Any) -> Any:
        self._driver.modes.append("read")
        return await work(FakeTransaction(self._driver))

    async def begin_transaction(self) -> FakeTransaction:
        return FakeTransaction(self._driver)


class FakeDriver:
    """Sessions run the queued ``errors`` (None = success) in order; ``execute_query`` answers
    ``dbms.components()`` with ``version`` and the ping, and records every other text."""

    def __init__(self, errors: list[Exception | None] | None = None, version: str = "5.26.0") -> None:
        self.errors = list(errors or [])
        self.version = version
        self.runs: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.modes: list[str] = []
        self.sessions: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.connectivity_error: Exception | None = None
        self.query_error: Exception | None = None

    def session(self, **options: Any) -> FakeSession:
        return FakeSession(self, options)

    async def verify_connectivity(self) -> None:
        if self.connectivity_error is not None:
            raise self.connectivity_error

    async def execute_query(self, text: str, *_args: Any, **_kwargs: Any) -> Any:
        self.queries.append(text)
        if self.query_error is not None:
            raise self.query_error
        if text == SERVER_COMPONENTS.text:
            records = [{"name": "Neo4j Kernel", "versions": [self.version], "edition": "community"}]
            return SimpleNamespace(records=records)
        if text == PING.text:
            return SimpleNamespace(records=[{"ok": 1}])
        return SimpleNamespace(records=[])


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def executor(driver: FakeDriver, sleeps: Sleeps | None = None) -> Executor:
    return Executor(driver, "neo4j", sleep=sleeps or Sleeps())


# Version gate (FR-CORE-03)


@pytest.mark.parametrize(
    ("version", "expected"),
    [("5.26.0", (5, 26, 0)), ("5.26.1-aura", (5, 26, 1)), ("5.27", (5, 27, 0)), ("2025.01.0", (2025, 1, 0))],
)
async def test_version_gate_accepts_5_26_and_later(version: str, expected: tuple[int, int, int]) -> None:
    assert await require_version(FakeDriver(version=version)) == expected


@pytest.mark.parametrize("version", ["5.25.0", "5.25.9", "4.4.30"])
async def test_version_gate_refuses_older_than_5_26(version: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        await require_version(FakeDriver(version=version))
    assert raised.value.code == "neo4j_version_unsupported"
    assert raised.value.fields["minimum"] == "5.26"


async def test_ensure_schema_on_5_25_raises_and_creates_nothing() -> None:
    driver = FakeDriver(version="5.25.0")
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, shipped_ontology(), llm_config())
    assert raised.value.code == "neo4j_version_unsupported"
    assert driver.queries == [SERVER_COMPONENTS.text]


async def test_server_version_unknown_kernel_is_configuration_error() -> None:
    driver = FakeDriver(version="not-a-version")
    with pytest.raises(ConfigurationError) as raised:
        await server_version(driver)
    assert raised.value.code == "neo4j_version_unknown"


@pytest.mark.parametrize(
    ("error", "expected", "code"),
    [
        (ServiceUnavailable("down"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (AuthError("rejected"), ConfigurationError, "neo4j_auth_rejected"),
    ],
)
async def test_server_version_maps_driver_errors(error: Exception, expected: type, code: str) -> None:
    driver = FakeDriver()
    driver.query_error = error
    with pytest.raises(expected) as raised:
        await server_version(driver)
    assert raised.value.code == code


# Executor: project scoping (FR-CORE-01, NFR-SEC-02)


async def test_executor_injects_validated_project_id() -> None:
    driver = FakeDriver()
    rows = await executor(driver).read(SCOPED, "proj-1", limit=3)
    assert rows == [{"project_id": "proj-1"}]
    assert driver.runs == [(SCOPED.text, {"limit": 3, "project_id": "proj-1"})]
    assert driver.modes == ["read"]
    assert driver.sessions[-1]["database"] == "neo4j"

    await executor(driver).write(SCOPED, "proj-1")
    assert driver.modes[-1] == "write"
    assert driver.runs[-1][1] == {"project_id": "proj-1"}


@pytest.mark.parametrize("method", ["read", "write"])
@pytest.mark.parametrize(
    ("query", "code"),
    [
        (UNBOUND, "query_missing_project_id"),
        (LOOKALIKE, "query_missing_project_id"),
        (PING, "query_not_project_scoped"),
    ],
)
async def test_executor_refuses_query_without_project_id(method: str, query: Query, code: str) -> None:
    driver = FakeDriver()
    with pytest.raises(ConformanceError) as raised:
        await getattr(executor(driver), method)(query, "proj-1")
    assert raised.value.code == code
    assert driver.runs == []


async def test_executor_refuses_caller_project_id_parameter() -> None:
    driver = FakeDriver()
    with pytest.raises(ConformanceError) as raised:
        await executor(driver).read(SCOPED, "proj-1", project_id="other")
    assert raised.value.code == "project_id_parameter_reserved"
    assert driver.runs == []


@pytest.mark.parametrize(
    "project_id", ["", "-starts-with-dash", "has space", "x" * 65, "ümlaut", "a/b", "a$b", None, 7]
)
async def test_invalid_project_ids_rejected(project_id: Any) -> None:
    driver = FakeDriver()
    with pytest.raises(ConfigurationError) as raised:
        await executor(driver).read(SCOPED, project_id)
    assert raised.value.code == "invalid_project_id"
    with pytest.raises(ConfigurationError):
        async with executor(driver).tx(project_id):
            pass
    assert driver.runs == []


@pytest.mark.parametrize("project_id", ["p", "proj-1", "Proj_2", "0abc", "x" * 64])
def test_valid_project_ids_accepted(project_id: str) -> None:
    assert validate_project_id(project_id) == project_id


async def test_tx_commits_and_injects_project_id() -> None:
    driver = FakeDriver()
    async with executor(driver).tx("proj-1") as tx:
        await tx.run(SCOPED, a=1)
        await tx.run(SCOPED, b=2)
    assert [params for _, params in driver.runs] == [
        {"a": 1, "project_id": "proj-1"},
        {"b": 2, "project_id": "proj-1"},
    ]
    assert (driver.commits, driver.rollbacks) == (1, 0)


async def test_tx_refuses_unscoped_query_and_rolls_back() -> None:
    driver = FakeDriver()
    with pytest.raises(ConformanceError) as raised:
        async with executor(driver).tx("proj-1") as tx:
            await tx.run(UNBOUND)
    assert raised.value.code == "query_missing_project_id"
    assert driver.runs == []
    assert (driver.commits, driver.rollbacks) == (0, 1)


async def test_tx_body_error_rolls_back() -> None:
    driver = FakeDriver()

    async def failing_body() -> None:
        async with executor(driver).tx("proj-1") as tx:
            await tx.run(SCOPED)
            raise ValueError("caller failure")

    with pytest.raises(ValueError, match="caller failure"):
        await failing_body()
    assert (driver.commits, driver.rollbacks) == (0, 1)


async def test_tx_driver_error_maps_and_rolls_back() -> None:
    driver = FakeDriver(errors=[client_error()])
    with pytest.raises(GraphWriteError) as raised:
        async with executor(driver).tx("proj-1") as tx:
            await tx.run(SCOPED)
    assert raised.value.code == "neo4j_write_failed"
    assert (driver.commits, driver.rollbacks) == (0, 1)


# Executor: driver error mapping (tech-specs section 7)


async def test_transient_write_error_is_retried_then_succeeds(central_log: LogHarness) -> None:
    central_log.configure()
    driver, sleeps = FakeDriver(errors=[transient(), transient(), None]), Sleeps()
    assert await executor(driver, sleeps).write(SCOPED, "p") == [{"project_id": "p"}]
    assert len(driver.runs) == 3
    assert sleeps.delays == [0.1, 0.3]
    # NFR-SEC-01: the retry is logged with ids and codes, never the server's message text.
    retries = central_log.events("graph.db.retry")
    assert [(r["attempt"], r["neo4j_code"], r["query"]) for r in retries] == [
        (1, TRANSIENT_CODE, "scoped"),
        (2, TRANSIENT_CODE, "scoped"),
    ]
    output = central_log.stdout() + central_log.stderr()
    assert LEAK_PROBE not in output and "secret-value-123" not in output


async def test_transient_write_error_exhausted_is_graph_write_error() -> None:
    driver, sleeps = FakeDriver(errors=[transient()] * 10), Sleeps()
    with pytest.raises(GraphWriteError) as raised:
        await executor(driver, sleeps).write(SCOPED, "p")
    assert raised.value.code == "neo4j_write_failed"
    assert raised.value.fields["attempts"] == 4
    assert raised.value.fields["neo4j_code"] == TRANSIENT_CODE
    assert len(driver.runs) == 4
    assert sleeps.delays == [0.1, 0.3, 1.0]


async def test_client_error_on_write_is_not_retried() -> None:
    driver = FakeDriver(errors=[client_error(), None])
    with pytest.raises(GraphWriteError) as raised:
        await executor(driver).write(SCOPED, "p")
    assert len(driver.runs) == 1
    assert raised.value.fields["query"] == "scoped"
    assert "secret" not in str(raised.value)


async def test_client_error_on_read_is_conformance_error() -> None:
    driver = FakeDriver(errors=[client_error()])
    with pytest.raises(ConformanceError) as raised:
        await executor(driver).read(SCOPED, "p")
    assert raised.value.code == "neo4j_query_rejected"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("method", ["read", "write"])
@pytest.mark.parametrize(
    ("make_error", "expected", "code"),
    [
        (lambda: ServiceUnavailable("down"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: SessionExpired("gone"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: OSError("socket"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (
            lambda: neo4j_error(AuthError, "Neo.ClientError.Security.Unauthorized"),
            ConfigurationError,
            "neo4j_auth_rejected",
        ),
        (
            lambda: neo4j_error(Forbidden, "Neo.ClientError.Security.Forbidden"),
            ConfigurationError,
            "neo4j_access_denied",
        ),
        (
            lambda: neo4j_error(ClientError, "Neo.ClientError.Database.DatabaseNotFound"),
            ConfigurationError,
            "neo4j_database_not_found",
        ),
    ],
)
async def test_driver_errors_map_to_graph_errors(method: str, make_error: Any, expected: type, code: str) -> None:
    driver = FakeDriver(errors=[make_error()])
    with pytest.raises(expected) as raised:
        await getattr(executor(driver), method)(SCOPED, "p")
    assert raised.value.code == code
    assert "secret" not in str(raised.value)


# The reachability check


async def test_check_succeeds_on_reachable_server() -> None:
    driver = FakeDriver()
    await check(driver)
    assert driver.queries == [PING.text]


@pytest.mark.parametrize(
    ("make_error", "expected", "code"),
    [
        (lambda: ServiceUnavailable("down"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: SessionExpired("gone"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: OSError("refused"), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: AuthError("rejected"), ConfigurationError, "neo4j_auth_rejected"),
        (
            lambda: neo4j_error(Forbidden, "Neo.ClientError.Security.Forbidden"),
            ConfigurationError,
            "neo4j_access_denied",
        ),
        (
            lambda: neo4j_error(ClientError, "Neo.ClientError.Database.DatabaseNotFound"),
            ConfigurationError,
            "neo4j_database_not_found",
        ),
    ],
)
@pytest.mark.parametrize("stage", ["connectivity", "ping"])
async def test_check_maps_failures(make_error: Any, expected: type, code: str, stage: str) -> None:
    driver = FakeDriver()
    if stage == "connectivity":
        driver.connectivity_error = make_error()
    else:
        driver.query_error = make_error()
    with pytest.raises(expected) as raised:
        await check(driver)
    assert raised.value.code == code


class RecordingDriverFactory:
    """Stands in for ``AsyncGraphDatabase.driver``: records the call and every use of the driver."""

    def __init__(self) -> None:
        self.created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.used: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.created.append((args, kwargs))
        used = self.used

        class Driver:
            def __getattr__(self, name: str) -> Any:
                used.append(name)
                raise AssertionError(f"connect() used driver.{name}")

        return Driver()


async def test_connect_builds_the_driver_without_using_it(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = RecordingDriverFactory()
    monkeypatch.setattr(db_module.AsyncGraphDatabase, "driver", factory)
    config = Neo4jConfig(uri="bolt://db.invalid:7687", user="neo4j", password=PROBE_LOGIN)
    assert "pw-not-shown-42" not in repr(config)
    connect(config)
    assert factory.used == []
    [(args, kwargs)] = factory.created
    assert args == ("bolt://db.invalid:7687",)
    assert kwargs["auth"] == ("neo4j", "pw-not-shown-42")
    assert kwargs["connection_timeout"] == db_module.CONNECTION_TIMEOUT_SECONDS


async def test_connect_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real driver is lazy: creating and closing it touches no socket."""

    opened: list[str] = []

    def refuse(name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            opened.append(name)
            raise AssertionError(f"network use: {name}")

        return fail

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(loop, "create_connection", refuse("loop.create_connection"))
    monkeypatch.setattr(loop, "sock_connect", refuse("loop.sock_connect"))
    monkeypatch.setattr(loop, "getaddrinfo", refuse("loop.getaddrinfo"))
    monkeypatch.setattr(asyncio, "open_connection", refuse("asyncio.open_connection"))
    driver = connect(Neo4jConfig(uri="bolt://127.0.0.1:1", user="neo4j", password=UNUSED_LOGIN))
    await driver.close()
    assert opened == []
