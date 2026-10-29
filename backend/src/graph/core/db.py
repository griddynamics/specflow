"""Neo4j access: driver factory, readiness check, version gate and the project-scoped executor.

The host owns the driver: it calls ``connect`` once per process and closes the driver on shutdown.
Every project query runs through ``Executor``, which refuses a scoped query that does not bind
``$project_id``, validates and injects the project id, and maps driver errors to ``GraphError``s
(docs/graph/implementation/tech-specs.md sections 4.3 and 7).
"""

from __future__ import annotations

import asyncio
import re
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

from graph.core.config import Neo4jConfig
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    GraphError,
    GraphWriteError,
    InfrastructureUnavailableError,
)
from graph.core.identity import validate_project_id
from graph.core.queries import PING, PROJECT_ID_PARAMETER, SERVER_COMPONENTS, Query
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.core.db")

CONNECTION_TIMEOUT_SECONDS: Final = 5.0
MINIMUM_VERSION: Final = (5, 26)
KERNEL_COMPONENT: Final = "Neo4j Kernel"
# Backoff before each retry of a transient failure; its length is the number of retries.
RETRY_DELAYS_SECONDS: Final = (0.1, 0.3, 1.0)
# The executor owns retries, so the driver's own managed-transaction retry loop is switched off.
_SESSION_RETRY_OFF: Final = {"max_transaction_retry_time": 0.0, "initial_retry_delay": 0.0}
_VERSION: Final = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
_DATABASE_NOT_FOUND: Final = "Neo.ClientError.Database.DatabaseNotFound"

Record = dict[str, Any]


def connect(config: Neo4jConfig) -> AsyncDriver:
    """Create the async driver; no connection is opened until first use."""

    return AsyncGraphDatabase.driver(
        config.uri,
        auth=(config.user, config.password.get_secret_value()),
        connection_timeout=CONNECTION_TIMEOUT_SECONDS,
        warn_notification_severity=NotificationMinimumSeverity.OFF,
    )


async def check(driver: Any, database: str | None = None) -> None:
    """Raise ``InfrastructureUnavailableError`` when Neo4j is unreachable, ``ConfigurationError`` on bad
    credentials, denied access or an unknown database; other driver errors map as in ``Executor``."""

    try:
        await driver.verify_connectivity()
        await driver.execute_query(PING.text, database_=database, routing_="r")
    except (Neo4jError, DriverError, OSError) as error:
        raise _map_error(error, PING.name, write=False) from None


async def server_version(driver: Any, database: str | None = None) -> tuple[int, int, int]:
    """The Neo4j kernel version as ``(major, minor, patch)``, read through ``dbms.components()``."""

    try:
        result = await driver.execute_query(SERVER_COMPONENTS.text, database_=database, routing_="r")
    except (Neo4jError, DriverError, OSError) as error:
        raise _map_error(error, SERVER_COMPONENTS.name, write=False) from None
    for record in result.records:
        if record["name"] == KERNEL_COMPONENT and record["versions"]:
            match = _VERSION.match(str(record["versions"][0]))
            if match:
                major, minor, patch = match.groups()
                return int(major), int(minor), int(patch or 0)
    raise ConfigurationError("neo4j_version_unknown")


async def require_version(
    driver: Any, minimum: tuple[int, ...] = MINIMUM_VERSION, database: str | None = None
) -> tuple[int, int, int]:
    """Return the server version; raise ``ConfigurationError`` when it is older than ``minimum``."""

    found = await server_version(driver, database)
    if found < tuple(minimum):
        raise ConfigurationError(
            "neo4j_version_unsupported",
            found=".".join(map(str, found)),
            minimum=".".join(map(str, minimum)),
        )
    return found


def _bind(query: Query, project_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Parameters of a scoped query with the validated ``project_id`` injected."""

    if not query.project_scoped:
        raise ConformanceError("query_not_project_scoped", query=query.name)
    if not query.binds_project_id():
        raise ConformanceError("query_missing_project_id", query=query.name)
    if PROJECT_ID_PARAMETER in params:
        raise ConformanceError("project_id_parameter_reserved", query=query.name)
    return {**params, PROJECT_ID_PARAMETER: validate_project_id(project_id)}


def _configuration_error(error: Exception, code: Any, query: str) -> ConfigurationError | None:
    """The ``ConfigurationError`` of a rejected login, a forbidden access or a missing database, else None."""

    if isinstance(error, AuthError):
        return ConfigurationError("neo4j_auth_rejected")
    if isinstance(error, Forbidden):
        return ConfigurationError("neo4j_access_denied", query=query)
    if code == _DATABASE_NOT_FOUND:
        return ConfigurationError("neo4j_database_not_found")
    return None


def _map_error(error: Exception, query: str, *, write: bool, attempts: int = 1) -> GraphError:
    """The ``GraphError`` a driver error stands for (tech-specs.md section 7)."""

    code = getattr(error, "code", None)
    fields: dict[str, Any] = {
        "query": query,
        "error_type": type(error).__name__,
        "neo4j_code": code if isinstance(code, str) else None,
        "attempts": attempts,
    }
    configuration = _configuration_error(error, code, query)
    if configuration is not None:
        return configuration
    if isinstance(error, (ServiceUnavailable, SessionExpired, OSError)):
        return InfrastructureUnavailableError("neo4j_unreachable", error_type=type(error).__name__)
    if write:
        return GraphWriteError("neo4j_write_failed", **fields)
    if isinstance(error, (ClientError, DriverError)):
        return ConformanceError("neo4j_query_rejected", **fields)
    return InfrastructureUnavailableError("neo4j_read_failed", **fields)


def _is_transient(error: Exception) -> bool:
    return isinstance(error, TransientError) and error.is_retryable()


class ScopedTransaction:
    """One explicit write transaction of one project; ``run`` injects ``project_id``."""

    def __init__(self, transaction: Any, project_id: str) -> None:
        self._transaction = transaction
        self._project_id = project_id

    async def run(self, query: Query, /, **params: Any) -> list[Record]:
        bound = _bind(query, self._project_id, params)
        try:
            result = await self._transaction.run(query.text, bound)
            records: list[Record] = await result.data()
        except (Neo4jError, DriverError, OSError) as error:
            raise _map_error(error, query.name, write=True) from None
        return records


class Executor:
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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._driver = driver
        self._database = database
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a read query of one project; return its records as dicts."""

        return await self._managed(query, _bind(query, project_id, params), write=False)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[Record]:
        """Run a write query of one project in a managed write transaction; return its records."""

        return await self._managed(query, _bind(query, project_id, params), write=True)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[ScopedTransaction]:
        """One explicit write transaction: committed when the block ends, rolled back when it raises."""

        project_id = validate_project_id(project_id)
        try:
            async with self._session(write=True) as session:
                transaction = await session.begin_transaction()
                try:
                    yield ScopedTransaction(transaction, project_id)
                except BaseException:
                    await _rollback(transaction)
                    raise
                await transaction.commit()
        except (Neo4jError, DriverError, OSError) as error:
            raise _map_error(error, "tx", write=True) from None

    def _session(self, *, write: bool) -> Any:
        return self._driver.session(
            database=self._database,
            default_access_mode=WRITE_ACCESS if write else READ_ACCESS,
            **_SESSION_RETRY_OFF,
        )

    async def _managed(self, query: Query, params: dict[str, Any], *, write: bool) -> list[Record]:
        async def work(transaction: Any) -> list[Record]:
            result = await transaction.run(query.text, params)
            records: list[Record] = await result.data()
            return records

        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._session(write=write) as session:
                    run = session.execute_write if write else session.execute_read
                    managed: list[Record] = await run(work)
                    return managed
            except (Neo4jError, DriverError, OSError) as error:
                if _is_transient(error) and attempt <= len(self._retry_delays):
                    log_event(
                        logger,
                        WARNING,
                        "graph.db.retry",
                        query=query.name,
                        attempt=attempt,
                        error_type=type(error).__name__,
                        neo4j_code=getattr(error, "code", None),
                    )
                    await self._sleep(self._retry_delays[attempt - 1])
                    continue
                raise _map_error(error, query.name, write=write, attempts=attempt) from None


async def _rollback(transaction: Any) -> None:
    """Roll back a failed explicit transaction; the original error is the one that propagates."""

    if transaction.closed():
        return
    try:
        await transaction.rollback()
    except (Neo4jError, DriverError, OSError) as error:
        log_event(logger, WARNING, "graph.db.rollback_failed", error_type=type(error).__name__)
