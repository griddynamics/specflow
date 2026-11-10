"""The graph store port: what the library needs from a graph database (design D1, D10, D11).

A ``GraphStore`` is one connection to one graph backend (Neo4j today). It hands out an ``Executor``
(project-scoped named queries, reads, writes and one explicit write transaction), checks that the
backend is reachable and usable, and owns the schema (``ensure_schema``). Graph build steps, the
reader and the owned writer talk to the ``Executor`` protocol only; each backend lives in its own
folder under ``graph.stores`` with its own SDK, executor, schema manager and query dialect.

This module is neutral: it imports no database SDK. It holds

- the protocols (``GraphStore``, ``Executor``, ``ScopedTransaction``) and ``StoreInfo``,
- the backend-neutral error codes of design D11,
- the shared retry helper every executor uses for transient failures (``retry_transient``),
- the project-id binding every scoped query goes through (``bind_project``),
- ``as_executor``, which accepts a store, an executor or (deprecated) a Neo4j driver.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast, runtime_checkable

from graph.core.errors import ConformanceError, GraphError
from graph.core.identity import validate_project_id
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.queries import PROJECT_ID_PARAMETER, Query

Record = dict[str, Any]

# Neutral error codes (design D11); each error also carries ``store=<backend name>``.
STORE_UNREACHABLE: Final = "store_unreachable"
STORE_AUTH_REJECTED: Final = "store_auth_rejected"
STORE_ACCESS_DENIED: Final = "store_access_denied"
STORE_DATABASE_NOT_FOUND: Final = "store_database_not_found"
STORE_VERSION_UNSUPPORTED: Final = "store_version_unsupported"
STORE_WRITE_FAILED: Final = "store_write_failed"
STORE_QUERY_REJECTED: Final = "store_query_rejected"
STORE_READ_FAILED: Final = "store_read_failed"
STORE_NOT_AVAILABLE: Final = "store_not_available"
STORE_TRANSACTIONS_UNSUPPORTED: Final = "store_transactions_unsupported"
# Raised by an adapter when the backend refuses a search text as too complex (design D6).
QUERY_TOO_COMPLEX: Final = "query_too_complex"

# The search indexes the reader names in its queries; every store's schema defines them.
SPEC_NODE_EMBEDDING_INDEX: Final = "graph_spec_node_embedding"
SPEC_NODE_TEXT_INDEX: Final = "graph_spec_node_text"
FRAGMENT_TEXT_INDEX: Final = "graph_fragment_text"

# Backoff before each retry of a transient failure; its length is the number of retries.
RETRY_DELAYS_SECONDS: Final = (0.1, 0.3, 1.0)


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    """What a backend can do beyond the common contract.

    ``multi_statement_tx`` false: ``Executor.tx`` commits each statement on its own, so callers
    that need an all-or-nothing group must refuse the store (``OwnedWriter.transaction()`` does).
    ``shared_vector_index`` true: one vector index holds the vectors of every project and a page of it is
    filtered to the project afterwards, so ``GraphReader`` fetches a larger first page from it.
    """

    multi_statement_tx: bool = True
    shared_vector_index: bool = False


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """Which backend a store is. ``server_version`` is ``None`` until the store has read it (``check``
    or ``ensure_schema``); its form is the backend's own (Neo4j: ``5.26.30``)."""

    backend: str
    capabilities: StoreCapabilities = StoreCapabilities()
    server_version: str | None = None


@runtime_checkable
class ScopedTransaction(Protocol):
    """One explicit write transaction of one project; ``run`` injects ``project_id``."""

    async def run(self, query: Query, /, **params: Any) -> list[Record]: ...


@runtime_checkable
class Executor(Protocol):
    """Runs project-scoped named queries and maps backend errors to ``GraphError``.

    ``read`` and ``write`` are retried on transient failures; ``tx`` is one explicit write
    transaction (committed when the block ends, rolled back when it raises) and is not retried.
    """

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[Record]: ...

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[Record]: ...

    def tx(self, project_id: str) -> AbstractAsyncContextManager[ScopedTransaction]: ...


@runtime_checkable
class GraphStore(Protocol):
    """One connection to one graph backend."""

    @property
    def info(self) -> StoreInfo: ...

    def executor(self) -> Executor: ...

    async def check(self) -> None:
        """Raise ``InfrastructureUnavailableError`` when the backend is unreachable and
        ``ConfigurationError`` for rejected credentials, denied access, an unknown database or an
        unsupported version."""
        ...

    async def ensure_schema(
        self,
        ontology: Ontology,
        embedding_dim: int,
        *,
        ddl: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        """Create what is missing of the graph schema (idempotent); return the names created. Refuses a
        vector dimension or similarity mismatch."""
        ...

    async def close(self) -> None: ...


def foreign_labels(ontology: Ontology) -> tuple[str, ...]:
    """The declared labels owned by another owner than build, sorted (stored without ``SpecNode``)."""

    return tuple(label for label in ontology.labels if ontology.owner_of(label) != BUILD_OWNER)


def bind_project(
    query: Query,
    project_id: str,
    params: Mapping[str, Any],
    *,
    require_binding: Callable[[Query], None] | None = None,
) -> dict[str, Any]:
    """Parameters of a scoped query with the validated ``project_id`` injected.

    The checks run in a fixed order: the query must be scoped, its statement must bind the project id
    (``require_binding``, the dialect's proof; raises ``ConformanceError``), the caller may not pass
    ``project_id`` itself, and the project id must be valid.
    """

    if not query.project_scoped:
        raise ConformanceError("query_not_project_scoped", query=query.name)
    if require_binding is not None:
        require_binding(query)
    if PROJECT_ID_PARAMETER in params:
        raise ConformanceError("project_id_parameter_reserved", query=query.name)
    return {**params, PROJECT_ID_PARAMETER: validate_project_id(project_id)}


async def retry_transient[T](
    operation: Callable[[], Awaitable[T]],
    *,
    errors: tuple[type[BaseException], ...],
    is_transient: Callable[[BaseException], bool],
    map_error: Callable[[BaseException, int], GraphError],
    delays: Sequence[float] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T:
    """Run ``operation``; retry a transient failure after each delay of ``delays``.

    A failure in ``errors`` for which ``is_transient`` holds is retried while retries remain
    (``on_retry(error, attempt)`` is called before each wait); any other failure, or a transient one
    after the last retry, is raised as ``map_error(error, attempts)``. ``attempts`` counts calls of
    ``operation`` including the failed one.
    """

    wait = sleep or asyncio.sleep
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except errors as error:
            if is_transient(error) and attempt <= len(delays):
                if on_retry is not None:
                    on_retry(error, attempt)
                await wait(delays[attempt - 1])
                continue
            raise map_error(error, attempt) from None


def is_store(source: Any) -> bool:
    """Whether ``source`` is a ``GraphStore`` (it hands out an executor and owns the schema)."""

    return callable(getattr(source, "executor", None)) and callable(getattr(source, "ensure_schema", None))


def is_executor(source: Any) -> bool:
    """Whether ``source`` is an ``Executor`` (read, write and tx)."""

    return all(callable(getattr(source, name, None)) for name in ("read", "write", "tx"))


def _wrap_driver(driver: Any, database: str | None) -> GraphStore:
    """The Neo4j store of a driver the caller owns (the deprecated way to hand the library a database)."""

    from graph.stores.neo4j import Neo4jStore  # imported late: core never needs a backend otherwise

    return cast(GraphStore, Neo4jStore(driver, database))


def as_store(source: Any, database: str | None = None) -> GraphStore:
    """``source`` when it is a store; otherwise, deprecated, a Neo4j driver wrapped in the Neo4j store."""

    if is_store(source):
        return cast(GraphStore, source)
    return _wrap_driver(source, database)


def multi_statement_tx(source: Any, executor: Any) -> bool:
    """Whether ``Executor.tx`` of ``source`` is one all-or-nothing transaction: the store's
    ``info.capabilities``, else the executor's ``capabilities`` attribute, else true."""

    if is_store(source):
        return bool(source.info.capabilities.multi_statement_tx)
    capabilities = getattr(executor, "capabilities", None)
    return True if capabilities is None else bool(capabilities.multi_statement_tx)


def shared_vector_index(source: Any, executor: Any) -> bool:
    """Whether the vector index of ``source`` is shared by every project: the store's ``info.capabilities``,
    else the executor's ``capabilities`` attribute, else false."""

    if is_store(source):
        return bool(source.info.capabilities.shared_vector_index)
    capabilities = getattr(executor, "capabilities", None)
    return False if capabilities is None else bool(capabilities.shared_vector_index)


def as_executor(source: Any, database: str | None = None) -> Executor:
    """The executor of ``source``: a store's own, an executor itself, or, deprecated, the executor of
    a Neo4j driver wrapped in the Neo4j adapter (``database`` names its database; ignored otherwise)."""

    if is_executor(source) and not is_store(source):
        return cast(Executor, source)
    executor: Executor = as_store(source, database).executor()
    return executor


async def check(source: Any, database: str | None = None) -> None:
    """``GraphStore.check`` of a store, or, deprecated, of a Neo4j driver (``database`` as in ``Neo4jStore``)."""

    await as_store(source, database).check()


async def ensure_schema(
    source: Any,
    ontology: Ontology,
    llm_config: Any,
    database: str | None = None,
    *,
    ddl: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Create the missing ``graph_*`` constraints and indexes; return the names created.

    ``source`` is a ``GraphStore`` or, deprecated, a Neo4j driver (with ``database``). ``llm_config``
    gives the vector size (``embedding_dim``). Errors are those of the store's ``ensure_schema``.
    """

    return await as_store(source, database).ensure_schema(ontology, llm_config.embedding_dim, ddl=ddl)
