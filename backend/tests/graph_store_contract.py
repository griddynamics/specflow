"""What every graph store adapter must do, as data and a harness interface (design D1, D8, D10, D11).

``tests/test_graph_store_contract.py`` runs one suite over every ``AdapterHarness`` in ``HARNESSES``.
A harness wraps one adapter around a fake of its SDK (no server): it builds the adapter's
``GraphStore`` and lets the suite script what the backend answers or how it fails, and read back what
reached it. An adapter milestone adds its harness here and the whole suite runs against it.

What the suite proves offline (the neutral half of the contract): the project-id guard in front of the
backend, error classes and neutral codes with the store name, the retry policy, transactions (one
all-or-nothing group, or a commit per statement when ``multi_statement_tx`` is false), rows passing
through as plain data, and the property payloads (``None`` removes a property, lists keep their order,
scalars keep their type) reaching the backend unchanged. What only a real backend can show (the columns of
each query, the stored form of a value) is the live suites' and the golden parity's job.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, Protocol

from neo4j.exceptions import (
    AuthError,
    ClientError,
    DatabaseError,
    Forbidden,
    ServiceUnavailable,
    TransientError,
)

from graph.core import (
    ConfigurationError,
    ConformanceError,
    GraphWriteError,
    InfrastructureUnavailableError,
)
from graph.core.queries import PING, SERVER_COMPONENTS
from graph.core.store import (
    QUERY_TOO_COMPLEX,
    STORE_ACCESS_DENIED,
    STORE_AUTH_REJECTED,
    STORE_DATABASE_NOT_FOUND,
    STORE_QUERY_REJECTED,
    STORE_READ_FAILED,
    STORE_UNREACHABLE,
    STORE_WRITE_FAILED,
    GraphStore,
    StoreCapabilities,
)
from graph.stores._bolt.cypher import render_cypher
from graph.stores.neo4j import Neo4jStore

CANARY: Final = "message carrying secret-value-123"
FAILURES: Final = (
    "unreachable",
    "auth_rejected",
    "access_denied",
    "database_not_found",
    "query_rejected",
    "server_error",
    "too_complex",
)
# kind -> (read: class, code; write: class, code): the neutral mapping of design D11 and D6.
EXPECTED: Final[dict[str, tuple[tuple[type, str], tuple[type, str]]]] = {
    "unreachable": (
        (InfrastructureUnavailableError, STORE_UNREACHABLE),
        (InfrastructureUnavailableError, STORE_UNREACHABLE),
    ),
    "auth_rejected": ((ConfigurationError, STORE_AUTH_REJECTED), (ConfigurationError, STORE_AUTH_REJECTED)),
    "access_denied": ((ConfigurationError, STORE_ACCESS_DENIED), (ConfigurationError, STORE_ACCESS_DENIED)),
    "database_not_found": (
        (ConfigurationError, STORE_DATABASE_NOT_FOUND),
        (ConfigurationError, STORE_DATABASE_NOT_FOUND),
    ),
    "query_rejected": ((ConformanceError, STORE_QUERY_REJECTED), (GraphWriteError, STORE_WRITE_FAILED)),
    "server_error": ((InfrastructureUnavailableError, STORE_READ_FAILED), (GraphWriteError, STORE_WRITE_FAILED)),
    "too_complex": ((ConformanceError, QUERY_TOO_COMPLEX), (GraphWriteError, STORE_WRITE_FAILED)),
}
CHECK_FAILURES: Final = ("unreachable", "auth_rejected", "database_not_found")


@dataclass
class Executed:
    """One statement that reached the backend: how it ran and the parameters it was given."""

    mode: str  # "read" or "write"
    params: dict[str, Any]


class AdapterHarness(Protocol):
    """One adapter around a fake SDK."""

    backend: str
    capabilities: StoreCapabilities
    statements: list[Executed]
    events: list[str]  # "begin", "statement", "commit", "rollback" in order, one set per transaction
    sleeps: list[float]  # the waits between retries

    def store(self) -> GraphStore: ...

    def answer(self, rows: list[dict[str, Any]]) -> None:
        """Every following statement returns ``rows``."""

    def fail(self, *kinds: str | None) -> None:
        """The following statements fail as the SDK would, one kind each (``None``: succeeds); a kind of
        ``FAILURES`` or ``"transient"`` (a retryable failure)."""

    def fail_check(self, kind: str) -> None:
        """The next ``store.check()`` fails as the SDK would (a kind of ``CHECK_FAILURES``)."""


# The Bolt harness: Neo4j (and ArcadeDB, which speaks Bolt) over a fake ``neo4j`` driver ---------------


def _hydrate(cls: type[Exception], code: str) -> Exception:
    return cls._hydrate_neo4j(code=code, message=CANARY)


_ERRORS: Final[dict[str, Callable[[], Exception]]] = {
    "unreachable": lambda: ServiceUnavailable(CANARY),
    "auth_rejected": lambda: AuthError(CANARY),
    "access_denied": lambda: Forbidden(CANARY),
    "database_not_found": lambda: _hydrate(ClientError, "Neo.ClientError.Database.DatabaseNotFound"),
    "query_rejected": lambda: _hydrate(ClientError, "Neo.ClientError.Statement.SyntaxError"),
    "server_error": lambda: _hydrate(DatabaseError, "Neo.DatabaseError.General.UnknownError"),
    "too_complex": lambda: _hydrate(ClientError, "Neo.ClientError.Procedure.ProcedureCallFailed"),
    "transient": lambda: _hydrate(TransientError, "Neo.TransientError.Transaction.DeadlockDetected"),
}


def _error(kind: str) -> Exception:
    return _ERRORS[kind]()


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


class _Transaction:
    def __init__(self, harness: BoltHarness, mode: str) -> None:
        self._harness = harness
        self._mode = mode
        self._closed = False
        harness.events.append("begin")

    async def run(self, _text: str, params: dict[str, Any]) -> _Result:
        h = self._harness
        h.events.append("statement")
        h.statements.append(Executed(self._mode, dict(params)))
        kind = h.failures.pop(0) if h.failures else None
        if kind is not None:
            raise _error(kind)
        return _Result(h.rows)

    async def commit(self) -> None:
        self._closed = True
        self._harness.events.append("commit")

    async def rollback(self) -> None:
        self._closed = True
        self._harness.events.append("rollback")

    def closed(self) -> bool:
        return self._closed


class _Session:
    def __init__(self, harness: BoltHarness) -> None:
        self._harness = harness

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def _managed(self, mode: str, work: Callable[[_Transaction], Awaitable[Any]]) -> Any:
        transaction = _Transaction(self._harness, mode)
        try:
            result = await work(transaction)
        except BaseException:
            await transaction.rollback()
            raise
        await transaction.commit()
        return result

    async def execute_write(self, work: Callable[[_Transaction], Awaitable[Any]]) -> Any:
        return await self._managed("write", work)

    async def execute_read(self, work: Callable[[_Transaction], Awaitable[Any]]) -> Any:
        return await self._managed("read", work)

    async def begin_transaction(self) -> _Transaction:
        return _Transaction(self._harness, "write")


class _Driver:
    def __init__(self, harness: BoltHarness) -> None:
        self._harness = harness

    def session(self, **_options: Any) -> _Session:
        return _Session(self._harness)

    async def verify_connectivity(self) -> None:
        kind, self._harness.check_failure = self._harness.check_failure, None
        if kind is not None:
            raise _error(kind)

    async def execute_query(self, text: str, *_args: Any, **_kwargs: Any) -> Any:
        if text == render_cypher(SERVER_COMPONENTS):
            return SimpleNamespace(records=[{"name": "Neo4j Kernel", "versions": ["5.26.0"], "edition": "community"}])
        if text == render_cypher(PING):
            return SimpleNamespace(records=[{"ok": 1}])
        return SimpleNamespace(records=[])

    async def close(self) -> None:
        return None


BOLT_CAPABILITIES: Final = StoreCapabilities(multi_statement_tx=True)


@dataclass
class BoltHarness:
    """The Neo4j adapter (``backend="neo4j"``) over a fake driver; a Bolt-speaking store passes its own name."""

    backend: str = "neo4j"
    capabilities: StoreCapabilities = BOLT_CAPABILITIES
    statements: list[Executed] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str | None] = field(default_factory=list)
    check_failure: str | None = None

    def store(self) -> GraphStore:
        async def sleep(delay: float) -> None:
            self.sleeps.append(delay)

        return Neo4jStore(_Driver(self), "graphdb", sleep=sleep)

    def answer(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fail(self, *kinds: str | None) -> None:
        self.failures = list(kinds)

    def fail_check(self, kind: str) -> None:
        self.check_failure = kind


HARNESSES: Final[dict[str, Callable[[], AdapterHarness]]] = {
    "neo4j": BoltHarness,
}
