"""The ArcadeDB executor: the shared Bolt executor with the ArcadeDB dialect and error mapping.

Two things differ from Neo4j. A merge or a move of relationships needs the relationship types of the
database (ArcadeDB cannot create an edge of a type only known at run time), so the executor reads them
before it plans one of those templates. And a fulltext call the server refuses because of its text is a
``SyntaxError`` here (Neo4j: ``ProcedureCallFailed``): it maps to the neutral ``query_too_complex``.
Everything else (retries, transaction handling, the other error codes) is the shared machinery; a
deadlock or a concurrent change is a ``TransientError`` and stays retryable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, override

from neo4j.exceptions import ClientError, DriverError, Neo4jError

from graph.core.dialect import ExecutionPlan
from graph.core.errors import ConformanceError, GraphError
from graph.core.queries import Query
from graph.core.store import QUERY_TOO_COMPLEX, RETRY_DELAYS_SECONDS, StoreCapabilities, bind_project
from graph.stores._bolt.executor import BoltExecutor, BoltScopedTransaction, _map_error
from graph.stores.arcadedb.dialect import (
    ARCADEDB_DIALECT,
    NEEDS_EDGE_TYPES,
    NEEDS_NODE_LABELS,
    RELATIONSHIP_TYPES,
    node_labels_statement,
)

BACKEND: Final = "arcadedb"
CAPABILITIES: Final = StoreCapabilities(multi_statement_tx=True)
# The templates that call a fulltext index procedure: a malformed search text fails these with SyntaxError.
FULLTEXT_QUERIES: Final = frozenset({"search_nodes_fulltext", "search_fragments"})
_SYNTAX_ERROR: Final = "Neo.ClientError.Statement.SyntaxError"


def map_error(
    error: BaseException, query: str, *, write: bool, attempts: int = 1, backend: str = BACKEND
) -> GraphError:
    """``_map_error`` of the shared executor, except that a fulltext search text the server cannot parse
    is ``ConformanceError("query_too_complex")`` (design D6)."""

    if (
        not write
        and query in FULLTEXT_QUERIES
        and isinstance(error, ClientError)
        and getattr(error, "code", None) == _SYNTAX_ERROR
    ):
        return ConformanceError(
            QUERY_TOO_COMPLEX,
            store=backend,
            query=query,
            error_type=type(error).__name__,
            backend_code=_SYNTAX_ERROR,
            attempts=attempts,
        )
    return _map_error(error, query, write=write, attempts=attempts, backend=backend)


def _types(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(str(row["relationshipType"]) for row in rows)


def _with_node_labels(params: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``params`` with ``node_labels``: the labels of each keyed node of the rows (by uuid)."""

    return {**params, "node_labels": {str(row["id"]): list(row["labels"]) for row in rows}}


class ArcadeDbScopedTransaction(BoltScopedTransaction):
    """The explicit transaction of one project; it reads the relationship types in the same transaction."""

    @override
    async def _plan(self, query: Query, params: Mapping[str, Any]) -> ExecutionPlan:
        types = None
        if query.template in NEEDS_EDGE_TYPES:
            result = await self._transaction.run(RELATIONSHIP_TYPES)
            types = _types(await result.data())
        if query.template in NEEDS_NODE_LABELS:
            read = node_labels_statement(params)
            result = await self._transaction.run(read.text, dict(read.params))
            params = _with_node_labels(params, await result.data())
        return ARCADEDB_DIALECT.plan(query, params, types)


class ArcadeDbExecutor(BoltExecutor):
    """``BoltExecutor`` with the ArcadeDB dialect, reading the relationship types a plan needs first."""

    def __init__(
        self,
        driver: Any,
        database: str,
        *,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            driver,
            database,
            retry_delays=retry_delays,
            sleep=sleep,
            dialect=ARCADEDB_DIALECT,
            capabilities=CAPABILITIES,
            backend=BACKEND,
            error_mapper=map_error,
        )

    @override
    async def _planned(self, query: Query, project_id: str, params: Mapping[str, Any]) -> ExecutionPlan:
        if query.template not in NEEDS_EDGE_TYPES:
            return self._plan(query, project_id, params)
        bound = bind_project(query, project_id, params, require_binding=self._dialect.require_project_binding)
        if query.template in NEEDS_NODE_LABELS:
            bound = _with_node_labels(bound, await self._node_labels(bound))
        return ARCADEDB_DIALECT.plan(query, bound, await self._relationship_types())

    async def _node_labels(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The labels of the keyed nodes of a label repair run as a managed write (read just before it)."""

        read = node_labels_statement(params)

        async def work(transaction: Any) -> list[dict[str, Any]]:
            result = await transaction.run(read.text, dict(read.params))
            rows: list[dict[str, Any]] = await result.data()
            return rows

        try:
            async with self._session(write=False) as session:
                rows: list[dict[str, Any]] = await session.execute_read(work)
        except (Neo4jError, DriverError, OSError) as error:
            raise self._error_mapper(error, "node_labels", write=False, backend=BACKEND) from None
        return rows

    async def _relationship_types(self) -> tuple[str, ...]:
        async def work(transaction: Any) -> list[dict[str, Any]]:
            result = await transaction.run(RELATIONSHIP_TYPES)
            rows: list[dict[str, Any]] = await result.data()
            return rows

        try:
            async with self._session(write=False) as session:
                return _types(await session.execute_read(work))
        except (Neo4jError, DriverError, OSError) as error:
            raise self._error_mapper(error, "relationship_types", write=False, backend=BACKEND) from None

    @override
    def _scoped(self, transaction: Any, project_id: str) -> ArcadeDbScopedTransaction:
        return ArcadeDbScopedTransaction(transaction, project_id, self._dialect, self._backend, self._error_mapper)
