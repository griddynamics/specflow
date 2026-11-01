"""PostgreSQL access: the engine of the store and the project-scoped executor.

``PostgresExecutor`` is the ``Executor`` of the store port for a PostgreSQL schema reached through SQLAlchemy's
``AsyncEngine`` over asyncpg. Every project query runs through it: it validates and injects the project id,
has the dialect plan the statements (refusing a scoped one that does not bind the project id), runs them in
one transaction (a managed read or write, or the explicit ``tx``), retries transient failures with the shared
helper (``graph.core.store.retry_transient``) and maps database errors to ``GraphError`` s by SQLSTATE with
the neutral codes of design D11.

The engine is the store's own small pool built from ``StoreConfig``; it never touches the host's engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from graph.core.dialect import ExecutionPlan, Record, run_plan
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    GraphError,
    GraphWriteError,
    InfrastructureUnavailableError,
)
from graph.core.identity import validate_project_id
from graph.core.queries import PING, Query
from graph.core.store import (
    RETRY_DELAYS_SECONDS,
    STORE_ACCESS_DENIED,
    STORE_AUTH_REJECTED,
    STORE_DATABASE_NOT_FOUND,
    STORE_QUERY_REJECTED,
    STORE_READ_FAILED,
    STORE_UNREACHABLE,
    STORE_WRITE_FAILED,
    StoreCapabilities,
    bind_project,
    retry_transient,
)
from graph.stores.config import StoreConfig
from graph.stores.postgres.dialect import BACKEND, POSTGRES_DIALECT, PostgresDialect
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.stores.postgres")

__all__ = [
    "CAPABILITIES",
    "CONNECTION_TIMEOUT_SECONDS",
    "DEFAULT_POOL_SIZE",
    "PostgresExecutor",
    "PostgresScopedTransaction",
    "build_engine",
    "check_connection",
    "map_error",
    "sqlstate",
]

CAPABILITIES: Final = StoreCapabilities(multi_statement_tx=True)
CONNECTION_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_POOL_SIZE: Final = 5
DRIVER: Final = "postgresql+asyncpg"
APPLICATION_NAME: Final = "specflow-graph"
# asyncpg takes the libpq ``sslmode`` values as its ``ssl`` argument.
_SSL_MODES: Final = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})

# SQLSTATE codes (appendix A of the PostgreSQL manual) the error map names.
AUTH_FAILED: Final = frozenset({"28P01", "28000"})
ACCESS_DENIED: Final = "42501"
DATABASE_MISSING: Final = frozenset({"3D000", "3F000"})
SERIALIZATION_FAILURE: Final = "40001"
DEADLOCK: Final = "40P01"
CANNOT_CONNECT_NOW: Final = "57P03"
CONNECTION_CLASS: Final = "08"
# Classes of a statement the server refuses: syntax or access rule (42), data exception (22), feature (0A).
REJECTED_CLASSES: Final = frozenset({"42", "22", "0A"})
# An administrator or a crash ended the session, or the server is out of connections: unreachable now.
UNREACHABLE: Final = frozenset({"57P01", "57P02", "53300", "53400"})
_TRANSIENT: Final = frozenset({SERIALIZATION_FAILURE, DEADLOCK, CANNOT_CONNECT_NOW})

# What a database call may raise: the SQLAlchemy layer, asyncpg itself and the socket.
DATABASE_ERRORS: Final = (SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


def sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE code of a database error (the asyncpg error SQLAlchemy wrapped, or the error itself)."""

    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            code = getattr(current, attribute, None)
            if isinstance(code, str) and code:
                return code
        pending.extend([getattr(current, "orig", None), current.__cause__, current.__context__])
    return None


def _is_connection_failure(error: BaseException) -> bool:
    """Whether the error is a lost or refused connection (no SQLSTATE, or class 08)."""

    code = sqlstate(error)
    if code is not None:
        return code.startswith(CONNECTION_CLASS) or code in UNREACHABLE or code == CANNOT_CONNECT_NOW
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (OSError, asyncpg.InterfaceError, asyncio.TimeoutError)):
            return True
        pending.extend([getattr(current, "orig", None), current.__cause__])
    return False


def is_transient(error: BaseException) -> bool:
    """Whether a failure is worth a retry: serialization failure, deadlock, the server not yet accepting
    connections, a lost connection (class 08 or a socket error)."""

    code = sqlstate(error)
    if code is not None and code in _TRANSIENT:
        return True
    return _is_connection_failure(error)


def map_error(error: BaseException, query: str, *, write: bool, attempts: int = 1, backend: str = BACKEND) -> GraphError:
    """The ``GraphError`` a database error stands for, with neutral codes (design D11) and ``store=<backend>``.

    28P01 and 28000 are a rejected login, 42501 a denied access, 3D000 and 3F000 an unknown database or
    schema, a lost or refused connection is unreachable; anything else is a write or a read failure (a
    statement the server refuses as malformed is a query rejection)."""

    code = sqlstate(error)
    fields: dict[str, Any] = {
        "store": backend,
        "query": query,
        "error_type": type(error).__name__,
        "backend_code": code,
        "attempts": attempts,
    }
    if code in AUTH_FAILED:
        return ConfigurationError(STORE_AUTH_REJECTED, store=backend)
    if code == ACCESS_DENIED:
        return ConfigurationError(STORE_ACCESS_DENIED, store=backend, query=query)
    if code in DATABASE_MISSING:
        return ConfigurationError(STORE_DATABASE_NOT_FOUND, store=backend)
    if _is_connection_failure(error):
        return InfrastructureUnavailableError(STORE_UNREACHABLE, store=backend, error_type=type(error).__name__)
    if write:
        return GraphWriteError(STORE_WRITE_FAILED, **fields)
    if code is not None and code[:2] in REJECTED_CLASSES:
        return ConformanceError(STORE_QUERY_REJECTED, **fields)
    return InfrastructureUnavailableError(STORE_READ_FAILED, **fields)


def connection_url(config: StoreConfig) -> tuple[URL, dict[str, Any]]:
    """The asyncpg URL of ``config`` (the host, port and database of ``uri``, the login of ``user`` and
    ``password``) and the connect arguments its query string asked for (``sslmode``)."""

    try:
        url = make_url(config.uri)
    except Exception:
        raise ConfigurationError("store_uri_invalid", store=BACKEND) from None
    if not url.drivername.startswith("postgres"):
        raise ConfigurationError("store_uri_invalid", store=BACKEND)
    query = dict(url.query)
    connect_args: dict[str, Any] = {}
    mode = query.pop("sslmode", None)
    if mode is not None:
        if not isinstance(mode, str) or mode not in _SSL_MODES:
            raise ConfigurationError("store_uri_invalid", store=BACKEND)
        connect_args["ssl"] = mode
    url = url.set(
        drivername=DRIVER,
        username=config.user,
        password=config.password.get_secret_value(),
        query=query,
    )
    return url, connect_args


def build_engine(config: StoreConfig, *, pool_size: int | None = None) -> AsyncEngine:
    """The store's own engine and small pool for ``config``; no connection is opened until first use.

    ``hide_parameters`` keeps bound values (document text, vectors) out of SQLAlchemy error messages.
    """

    url, connect_args = connection_url(config)
    size = pool_size or config.pool_size or DEFAULT_POOL_SIZE
    return create_async_engine(
        url,
        pool_size=size,
        max_overflow=0,
        pool_pre_ping=True,
        pool_timeout=CONNECTION_TIMEOUT_SECONDS * 6,
        hide_parameters=True,
        connect_args={
            "timeout": CONNECTION_TIMEOUT_SECONDS,
            "server_settings": {"application_name": APPLICATION_NAME},
            **connect_args,
        },
    )


async def check_connection(engine: AsyncEngine, dialect: PostgresDialect, backend: str = BACKEND) -> None:
    """Raise ``InfrastructureUnavailableError`` when the server is unreachable and ``ConfigurationError`` for
    a rejected login, denied access or an unknown database; other errors map as in ``PostgresExecutor``."""

    plan = dialect.plan(PING, {})
    try:
        async with engine.connect() as connection:
            for statement in plan.statements:
                await connection.execute(text(statement.text), dict(statement.params))
    except DATABASE_ERRORS as error:
        raise map_error(error, PING.name, write=False, backend=backend) from None


async def _run(connection: AsyncConnection, plan: ExecutionPlan) -> list[Record]:
    async def statement(sql: str, bound: Mapping[str, Any]) -> list[Record]:
        result = await connection.execute(text(sql), dict(bound))
        if not result.returns_rows:
            return []
        return [dict(row) for row in result.mappings().all()]

    return await run_plan(plan, statement)


class PostgresScopedTransaction:
    """One explicit write transaction of one project; ``run`` injects ``project_id``."""

    def __init__(
        self, connection: AsyncConnection, project_id: str, dialect: PostgresDialect, backend: str = BACKEND
    ) -> None:
        self._connection = connection
        self._project_id = project_id
        self._dialect = dialect
        self._backend = backend

    async def run(self, query: Query, /, **params: Any) -> list[Record]:
        plan = self._dialect.plan(query, bind_project(query, self._project_id, params))
        try:
            return await _run(self._connection, plan)
        except DATABASE_ERRORS as error:
            raise map_error(error, query.name, write=True, backend=self._backend) from None


class PostgresExecutor:
    """Runs project-scoped queries: reads and writes as managed transactions, ``tx`` as one explicit one.

    A transient failure is retried after each delay of ``retry_delays``; a write still failing then
    raises ``GraphWriteError``. ``tx`` is not retried: its body is caller code. Reads and writes use the
    same (writer) endpoint, so a read sees the writes before it.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        dialect: PostgresDialect = POSTGRES_DIALECT,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        capabilities: StoreCapabilities = CAPABILITIES,
        backend: str = BACKEND,
    ) -> None:
        self._engine = engine
        self._dialect = dialect
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep or asyncio.sleep
        self.capabilities = capabilities
        self._backend = backend

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a read query of one project; return its records as dicts."""

        return await self._managed(query, self._plan(query, project_id, params), write=False)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a write query of one project in a managed write transaction; return its records."""

        return await self._managed(query, self._plan(query, project_id, params), write=True)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[PostgresScopedTransaction]:
        """One explicit write transaction: committed when the block ends, rolled back when it raises."""

        project_id = validate_project_id(project_id)
        try:
            async with self._engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    yield PostgresScopedTransaction(connection, project_id, self._dialect, self._backend)
                except BaseException:
                    await _rollback(transaction)
                    raise
                await transaction.commit()
        except DATABASE_ERRORS as error:
            raise map_error(error, "tx", write=True, backend=self._backend) from None

    def _plan(self, query: Query, project_id: str, params: Mapping[str, Any]) -> ExecutionPlan:
        return self._dialect.plan(query, bind_project(query, project_id, params))

    async def _managed(self, query: Query, plan: ExecutionPlan, *, write: bool) -> list[Record]:
        async def attempt() -> list[Record]:
            async with self._engine.connect() as connection, connection.begin():
                return await _run(connection, plan)

        def retrying(error: BaseException, number: int) -> None:
            log_event(
                logger,
                WARNING,
                "graph.db.retry",
                query=query.name,
                attempt=number,
                error_type=type(error).__name__,
                backend_code=sqlstate(error),
            )

        return await retry_transient(
            attempt,
            errors=DATABASE_ERRORS,
            is_transient=is_transient,
            map_error=lambda error, attempts: map_error(
                error, query.name, write=write, attempts=attempts, backend=self._backend
            ),
            delays=self._retry_delays,
            sleep=self._sleep,
            on_retry=retrying,
        )


async def _rollback(transaction: Any) -> None:
    """Roll back a failed explicit transaction; the original error is the one that propagates."""

    if not transaction.is_active:
        return
    try:
        await transaction.rollback()
    except DATABASE_ERRORS as error:
        log_event(logger, WARNING, "graph.db.rollback_failed", error_type=type(error).__name__)
