"""Bolt access: driver factory, readiness check and the project-scoped executor of every Bolt store.

``BoltExecutor`` is the ``Executor`` of the store port for a backend reached through the ``neo4j`` driver.
Every project query runs through it: it validates and injects the project id, has the dialect plan the
statements (refusing a scoped one that does not bind the project id), runs them as a managed or an
explicit transaction, retries transient failures (``graph.core.store.retry_transient``) and maps driver
errors to ``GraphError``s (docs/graph/implementation/tech-specs.md sections 4.3 and 7). ``backend`` names
the store in errors and logs. The host owns a driver it hands in: it creates it with ``connect`` and
closes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from neo4j import (
    READ_ACCESS,
    WRITE_ACCESS,
    AsyncDriver,
    AsyncGraphDatabase,
    NotificationMinimumSeverity,
)
from neo4j.exceptions import (
    AuthError,
    ClientError,
    DriverError,
    Forbidden,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from graph.core.dialect import Dialect, ExecutionPlan, run_plan
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
from graph.stores._bolt.cypher import CYPHER_DIALECT, render_cypher
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.stores.bolt")

# AsyncGraphDatabase is part of this module's surface: tests patch the driver factory through it.
__all__ = [
    "CONNECTION_TIMEOUT_SECONDS",
    "DEFAULT_BACKEND",
    "DEFAULT_CAPABILITIES",
    "AsyncGraphDatabase",
    "BoltExecutor",
    "BoltScopedTransaction",
    "ErrorMapper",
    "check_connection",
    "connect",
]

CONNECTION_TIMEOUT_SECONDS: Final = 5.0
# The executor owns retries, so the driver's own managed-transaction retry loop is switched off.
_SESSION_RETRY_OFF: Final = {"max_transaction_retry_time": 0.0, "initial_retry_delay": 0.0}
_DATABASE_NOT_FOUND: Final = "Neo.ClientError.Database.DatabaseNotFound"
_PROCEDURE_FAILED: Final = "Neo.ClientError.Procedure.ProcedureCallFailed"
# Only a fulltext search can fail as "too complex" (Lucene's clause limit); any other procedure failure is a
# rejected query.
_FULLTEXT_QUERIES: Final = frozenset({SEARCH_NODES_FULLTEXT.name, SEARCH_FRAGMENTS.name})
DEFAULT_BACKEND: Final = "neo4j"
DEFAULT_CAPABILITIES: Final = StoreCapabilities()

Record = dict[str, Any]


def connect(config: Any) -> AsyncDriver:
    """Create the async driver of ``config`` (``uri``, ``user``, ``password`` secret, optional ``pool_size``);
    no connection is opened until first use."""

    options: dict[str, Any] = {}
    pool_size = getattr(config, "pool_size", None)
    if pool_size is not None:  # unset keeps the driver's default
        options["max_connection_pool_size"] = pool_size
    return AsyncGraphDatabase.driver(
        config.uri,
        auth=(config.user, config.password.get_secret_value()),
        connection_timeout=CONNECTION_TIMEOUT_SECONDS,
        warn_notification_severity=NotificationMinimumSeverity.OFF,
        **options,
    )


async def check_connection(driver: Any, database: str | None = None, backend: str = DEFAULT_BACKEND) -> None:
    """Raise ``InfrastructureUnavailableError`` when Neo4j is unreachable, ``ConfigurationError`` on bad
    credentials, denied access or an unknown database; other driver errors map as in ``BoltExecutor``."""

    try:
        await driver.verify_connectivity()
        await driver.execute_query(render_cypher(PING), database_=database, routing_="r")
    except (Neo4jError, DriverError, OSError) as error:
        raise _map_error(error, PING.name, write=False, backend=backend) from None


def _plan_scoped(dialect: Dialect, query: Query, project_id: str, params: Mapping[str, Any]) -> ExecutionPlan:
    """The dialect's plan of a scoped query with the validated ``project_id`` injected."""

    bound = bind_project(query, project_id, params, require_binding=dialect.require_project_binding)
    return dialect.plan(query, bound)


def _bind(
    query: Query, project_id: str, params: Mapping[str, Any], dialect: Dialect = CYPHER_DIALECT
) -> dict[str, Any]:
    """Parameters of a scoped query with the validated ``project_id`` injected; the dialect refuses a
    statement that does not bind it."""

    bound = bind_project(query, project_id, params, require_binding=dialect.require_project_binding)
    dialect.plan(query, bound)
    return bound


def _configuration_error(error: BaseException, code: Any, query: str, backend: str) -> ConfigurationError | None:
    """The ``ConfigurationError`` of a rejected login, a forbidden access or a missing database, else None."""

    if isinstance(error, AuthError):
        return ConfigurationError(STORE_AUTH_REJECTED, store=backend)
    if isinstance(error, Forbidden):
        return ConfigurationError(STORE_ACCESS_DENIED, store=backend, query=query)
    if code == _DATABASE_NOT_FOUND:
        return ConfigurationError(STORE_DATABASE_NOT_FOUND, store=backend)
    return None


def _map_error(
    error: BaseException, query: str, *, write: bool, attempts: int = 1, backend: str = DEFAULT_BACKEND
) -> GraphError:
    """The ``GraphError`` a driver error stands for (tech-specs.md section 7), with neutral codes
    (design D11) and ``store=<backend>``. A fulltext search the server refuses with a failed procedure
    call is ``ConformanceError("query_too_complex")`` (design D6); any other failed procedure call is
    ``store_query_rejected``. Both carry ``error_type``, ``backend_code`` and ``attempts``."""

    code = getattr(error, "code", None)
    fields: dict[str, Any] = {
        "store": backend,
        "query": query,
        "error_type": type(error).__name__,
        "backend_code": code if isinstance(code, str) else None,
        "attempts": attempts,
    }
    configuration = _configuration_error(error, code, query, backend)
    if configuration is not None:
        return configuration
    if isinstance(error, (ServiceUnavailable, SessionExpired, OSError)):
        return InfrastructureUnavailableError(STORE_UNREACHABLE, store=backend, error_type=type(error).__name__)
    if write:
        return GraphWriteError(STORE_WRITE_FAILED, **fields)
    if code == _PROCEDURE_FAILED and query in _FULLTEXT_QUERIES:
        return ConformanceError(QUERY_TOO_COMPLEX, **fields)
    if isinstance(error, (ClientError, DriverError)):
        return ConformanceError(STORE_QUERY_REJECTED, **fields)
    return InfrastructureUnavailableError(STORE_READ_FAILED, **fields)


ErrorMapper = Callable[..., GraphError]
"""``(error, query_name, *, write, attempts, backend)`` -> the ``GraphError`` a driver error stands for.
A store whose server reports some error differently passes its own (default: ``_map_error``)."""


def _is_transient(error: BaseException) -> bool:
    return isinstance(error, TransientError) and error.is_retryable()


class BoltScopedTransaction:
    """One explicit write transaction of one project; ``run`` injects ``project_id``."""

    def __init__(
        self,
        transaction: Any,
        project_id: str,
        dialect: Dialect = CYPHER_DIALECT,
        backend: str = DEFAULT_BACKEND,
        error_mapper: ErrorMapper = _map_error,
    ) -> None:
        self._transaction = transaction
        self._project_id = project_id
        self._dialect = dialect
        self._backend = backend
        self._error_mapper = error_mapper

    async def _plan(self, query: Query, params: Mapping[str, Any]) -> ExecutionPlan:
        """The plan of ``query``; a store whose plans need a read first overrides this."""

        return self._dialect.plan(query, params)

    async def run(self, query: Query, /, **params: Any) -> list[Record]:
        bound = bind_project(query, self._project_id, params, require_binding=self._dialect.require_project_binding)
        plan = await self._plan(query, bound)

        async def statement(text: str, bound: Mapping[str, Any]) -> list[Record]:
            result = await self._transaction.run(text, dict(bound))
            records: list[Record] = await result.data()
            return records

        try:
            return await run_plan(plan, statement)
        except (Neo4jError, DriverError, OSError) as error:
            raise self._error_mapper(error, query.name, write=True, backend=self._backend) from None


class BoltExecutor:
    """Runs project-scoped queries: reads and writes as managed transactions, ``tx`` as one explicit one.

    A transient failure is retried after each delay of ``retry_delays``; a write still failing then
    raises ``GraphWriteError``. ``tx`` is not retried: its body is caller code.
    """

    def __init__(
        self,
        driver: Any,
        database: str | None = None,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        dialect: Dialect = CYPHER_DIALECT,
        capabilities: StoreCapabilities = DEFAULT_CAPABILITIES,
        backend: str = DEFAULT_BACKEND,
        error_mapper: ErrorMapper = _map_error,
    ) -> None:
        self._driver = driver
        self._database = database
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep or asyncio.sleep
        self._dialect = dialect
        self.capabilities = capabilities
        self._backend = backend
        self._error_mapper = error_mapper

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a read query of one project; return its records as dicts."""

        return await self._managed(query, await self._planned(query, project_id, params), write=False)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a write query of one project in a managed write transaction; return its records."""

        return await self._managed(query, await self._planned(query, project_id, params), write=True)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[BoltScopedTransaction]:
        """One explicit write transaction: committed when the block ends, rolled back when it raises."""

        project_id = validate_project_id(project_id)
        try:
            async with self._session(write=True) as session:
                transaction = await session.begin_transaction()
                try:
                    yield self._scoped(transaction, project_id)
                except BaseException:
                    await _rollback(transaction)
                    raise
                await transaction.commit()
        except (Neo4jError, DriverError, OSError) as error:
            raise self._error_mapper(error, "tx", write=True, backend=self._backend) from None

    def _scoped(self, transaction: Any, project_id: str) -> BoltScopedTransaction:
        """The scoped transaction handed to the caller of ``tx`` (a store may return its own subclass)."""

        return BoltScopedTransaction(transaction, project_id, self._dialect, self._backend, self._error_mapper)

    def _session(self, *, write: bool) -> Any:
        return self._driver.session(
            database=self._database,
            default_access_mode=WRITE_ACCESS if write else READ_ACCESS,
            **_SESSION_RETRY_OFF,
        )

    def _plan(self, query: Query, project_id: str, params: Mapping[str, Any]) -> ExecutionPlan:
        return _plan_scoped(self._dialect, query, project_id, params)

    async def _planned(self, query: Query, project_id: str, params: Mapping[str, Any]) -> ExecutionPlan:
        """The plan of ``query``; a store whose plans need a read first overrides this."""

        return self._plan(query, project_id, params)

    async def _managed(self, query: Query, plan: ExecutionPlan, *, write: bool) -> list[Record]:
        async def work(transaction: Any) -> list[Record]:
            async def statement(text: str, bound: Mapping[str, Any]) -> list[Record]:
                result = await transaction.run(text, dict(bound))
                records: list[Record] = await result.data()
                return records

            return await run_plan(plan, statement)

        async def attempt() -> list[Record]:
            async with self._session(write=write) as session:
                run = session.execute_write if write else session.execute_read
                managed: list[Record] = await run(work)
                return managed

        def retrying(error: BaseException, number: int) -> None:
            log_event(
                logger,
                WARNING,
                "graph.db.retry",
                query=query.name,
                attempt=number,
                error_type=type(error).__name__,
                backend_code=getattr(error, "code", None),
            )

        return await retry_transient(
            attempt,
            errors=(Neo4jError, DriverError, OSError),
            is_transient=_is_transient,
            map_error=lambda error, attempts: self._error_mapper(
                error, query.name, write=write, attempts=attempts, backend=self._backend
            ),
            delays=self._retry_delays,
            sleep=self._sleep,
            on_retry=retrying,
        )


async def _rollback(transaction: Any) -> None:
    """Roll back a failed explicit transaction; the original error is the one that propagates."""

    if transaction.closed():
        return
    try:
        await transaction.rollback()
    except (Neo4jError, DriverError, OSError) as error:
        log_event(logger, WARNING, "graph.db.rollback_failed", error_type=type(error).__name__)
