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

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DisconnectionError, InterfaceError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from graph.core.dialect import ExecutionPlan, Record, run_plan
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    GraphError,
    GraphWriteError,
    InfrastructureUnavailableError,
)
from graph.core.identity import validate_project_id
from graph.core.queries import PING, SEARCH_FRAGMENTS, SEARCH_NODES_FULLTEXT, Query
from graph.core.store import (
    QUERY_TOO_COMPLEX,
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
from postgres import SSL_MODES, new_engine, ssl_argument, transaction

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
MAX_OVERFLOW: Final = 0
POOL_TIMEOUT_SECONDS: Final = 30.0
DRIVER: Final = "postgresql+asyncpg"
APPLICATION_NAME: Final = "specflow-graph"
# Server-side limits set on every connection (asyncpg ``server_settings``): a statement that runs longer is
# cancelled (SQLSTATE 57014) and a statement waiting longer for a lock fails (55P03); neither is retried.
# ``?statement_timeout=<seconds>`` and ``?lock_timeout=<seconds>`` in the store URI override them, 0 disables.
STATEMENT_TIMEOUT_SECONDS: Final = 300.0
LOCK_TIMEOUT_SECONDS: Final = 60.0
MAX_TIMEOUT_SECONDS: Final = 86_400.0

# SQLSTATE codes (appendix A of the PostgreSQL manual) the error map names.
AUTH_FAILED: Final = frozenset({"28P01", "28000"})
ACCESS_DENIED: Final = "42501"
DATABASE_MISSING: Final = frozenset({"3D000", "3F000"})
SERIALIZATION_FAILURE: Final = "40001"
DEADLOCK: Final = "40P01"
CANNOT_CONNECT_NOW: Final = "57P03"
CONNECTION_CLASS: Final = "08"
# A statement the server refuses as too complex (stack depth, too many arguments): design D6.
STATEMENT_TOO_COMPLEX: Final = "54001"
# Only a fulltext search is reported as too complex; the same SQLSTATE elsewhere keeps its normal code.
_FULLTEXT_QUERIES: Final = frozenset({SEARCH_NODES_FULLTEXT.name, SEARCH_FRAGMENTS.name})
# Classes of a statement the server refuses: syntax or access rule (42), data exception (22), feature (0A).
REJECTED_CLASSES: Final = frozenset({"42", "22", "0A"})
# An administrator or a crash ended the session, or the server is out of connections: unreachable now.
UNREACHABLE: Final = frozenset({"57P01", "57P02", "53300", "53400"})
_TRANSIENT: Final = frozenset({SERIALIZATION_FAILURE, DEADLOCK, CANNOT_CONNECT_NOW})

# What a database call may raise: the SQLAlchemy layer (it wraps every asyncpg error) and the socket.
DATABASE_ERRORS: Final = (SQLAlchemyError, OSError)


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
    """Whether the error is a lost or refused connection (no SQLSTATE, or class 08). A pool timeout
    (``SQLAlchemyTimeoutError``) is not: it is a busy pool, not a broken connection, and is not retried."""

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
        if isinstance(current, SQLAlchemyTimeoutError):
            # the pool is exhausted (every connection is busy): waiting again would only double the wait
            return False
        if isinstance(current, (OSError, InterfaceError, DisconnectionError, asyncio.TimeoutError)):
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


def _configuration_error(error: BaseException, code: str | None, query: str, backend: str) -> GraphError | None:
    """The error of a rejected login, a denied access, a missing database or an unreachable server, else None."""

    if code in AUTH_FAILED:
        return ConfigurationError(STORE_AUTH_REJECTED, store=backend)
    if code == ACCESS_DENIED:
        return ConfigurationError(STORE_ACCESS_DENIED, store=backend, query=query)
    if code in DATABASE_MISSING:
        return ConfigurationError(STORE_DATABASE_NOT_FOUND, store=backend)
    if _is_connection_failure(error):
        return InfrastructureUnavailableError(STORE_UNREACHABLE, store=backend, error_type=type(error).__name__)
    return None


def map_error(
    error: BaseException, query: str, *, write: bool, attempts: int = 1, backend: str = BACKEND
) -> GraphError:
    """The ``GraphError`` a database error stands for, with neutral codes (design D11) and ``store=<backend>``.

    28P01 and 28000 are a rejected login, 42501 a denied access, 3D000 and 3F000 an unknown database or
    schema, a lost or refused connection is unreachable; anything else is a write or a read failure (a
    statement the server refuses as malformed is a query rejection)."""

    code = sqlstate(error)
    configuration = _configuration_error(error, code, query, backend)
    if configuration is not None:
        return configuration
    fields: dict[str, Any] = {
        "store": backend,
        "query": query,
        "error_type": type(error).__name__,
        "backend_code": code,
        "attempts": attempts,
    }
    if write:
        return GraphWriteError(STORE_WRITE_FAILED, **fields)
    if code == STATEMENT_TOO_COMPLEX and query in _FULLTEXT_QUERIES:
        return ConformanceError(QUERY_TOO_COMPLEX, **fields)
    if code is not None and code[:2] in REJECTED_CLASSES:
        return ConformanceError(STORE_QUERY_REJECTED, **fields)
    return InfrastructureUnavailableError(STORE_READ_FAILED, **fields)


def _milliseconds(value: Any) -> str:
    """A timeout of the URI query in seconds as the server's milliseconds; invalid values are a URI error."""

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ConfigurationError("store_uri_invalid", store=BACKEND) from None
    if not 0 <= seconds < MAX_TIMEOUT_SECONDS:
        raise ConfigurationError("store_uri_invalid", store=BACKEND)
    return str(round(seconds * 1000))


def connection_url(config: StoreConfig) -> tuple[URL, dict[str, Any]]:
    """The asyncpg URL of ``config`` (the host, port and database of ``uri``, the login of ``user`` and
    ``password``) and the connect arguments its query string asked for (``sslmode`` with its CA bundle
    ``sslrootcert``, ``statement_timeout`` and ``lock_timeout`` in seconds)."""

    try:
        url = make_url(config.uri)
    except ArgumentError:
        raise ConfigurationError("store_uri_invalid", store=BACKEND) from None
    if not url.drivername.startswith("postgres"):
        raise ConfigurationError("store_uri_invalid", store=BACKEND)
    query = dict(url.query)
    connect_args: dict[str, Any] = {}
    for option in ("statement_timeout", "lock_timeout"):
        value = query.pop(option, None)
        if value is not None:
            connect_args.setdefault("server_settings", {})[option] = _milliseconds(value)
    mode = query.pop("sslmode", None)
    rootcert = query.pop("sslrootcert", None)
    if mode is not None:
        if not isinstance(mode, str) or mode not in SSL_MODES or not isinstance(rootcert, str | None):
            raise ConfigurationError("store_uri_invalid", store=BACKEND)
        try:
            connect_args["ssl"] = ssl_argument(mode, rootcert)
        except OSError:  # the CA bundle is missing, unreadable or holds no certificate
            raise ConfigurationError("store_uri_invalid", store=BACKEND) from None
    url = url.set(
        drivername=DRIVER,
        username=config.user,
        password=config.password.get_secret_value(),
        query=query,
    )
    return url, connect_args


def _server_settings() -> dict[str, str]:
    return {
        "application_name": APPLICATION_NAME,
        "statement_timeout": _milliseconds(STATEMENT_TIMEOUT_SECONDS),
        "lock_timeout": _milliseconds(LOCK_TIMEOUT_SECONDS),
    }


def build_engine(config: StoreConfig, *, pool_size: int | None = None) -> AsyncEngine:
    """The store's own engine and small pool for ``config``; no connection is opened until first use.

    ``hide_parameters`` keeps bound values (document text, vectors) out of SQLAlchemy error messages.
    """

    url, connect_args = connection_url(config)
    size = pool_size or config.pool_size or DEFAULT_POOL_SIZE
    return new_engine(
        url,
        pool_size=size,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        connect_args={
            "timeout": CONNECTION_TIMEOUT_SECONDS,
            **connect_args,
            "server_settings": {**_server_settings(), **connect_args.get("server_settings", {})},
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
        plan = self._dialect.plan(
            query, bind_project(query, self._project_id, params, require_binding=self._dialect.require_project_binding)
        )
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
        """Run a read query of one project in a read-only transaction; its rows as dicts."""

        return await self._run(query, project_id, params, write=False)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a write query of one project in a managed write transaction; its rows as dicts."""

        return await self._run(query, project_id, params, write=True)

    async def _run(self, query: Query, project_id: str, params: Mapping[str, Any], *, write: bool) -> list[Record]:
        plan = self._dialect.plan(
            query, bind_project(query, project_id, params, require_binding=self._dialect.require_project_binding)
        )
        return await self._managed(query, plan, write=write)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[PostgresScopedTransaction]:
        """One explicit write transaction: committed when the block ends, rolled back when it raises."""

        project_id = validate_project_id(project_id)
        try:
            async with transaction(self._engine, on_rollback_error=_rollback_failed) as connection:
                yield PostgresScopedTransaction(connection, project_id, self._dialect, self._backend)
        except DATABASE_ERRORS as error:
            raise map_error(error, "tx", write=True, backend=self._backend) from None

    async def _managed(self, query: Query, plan: ExecutionPlan, *, write: bool) -> list[Record]:
        async def attempt() -> list[Record]:
            # a read cannot change data, whatever its statement says
            async with transaction(self._engine, read_only=not write) as connection:
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


def _rollback_failed(error: BaseException) -> None:
    """A failed explicit transaction whose rollback failed too: logged; the original error is the one that
    propagates."""

    log_event(logger, WARNING, "graph.db.rollback_failed", error_type=type(error).__name__)
