"""The Neo4j store: a ``GraphStore`` over the shared Bolt machinery with Neo4j's version gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from graph.core.ontology import Ontology
from graph.core.store import RETRY_DELAYS_SECONDS, StoreCapabilities, StoreInfo
from graph.stores._bolt.executor import BoltExecutor, check_connection, connect
from graph.stores._bolt.schema import ensure_cypher_schema
from graph.stores.neo4j.version import BACKEND, MINIMUM_VERSION, require_version

CAPABILITIES: Final = StoreCapabilities(multi_statement_tx=True, shared_vector_index=True)


async def _gate(driver: Any, database: str | None) -> tuple[int, ...]:
    return await require_version(driver, MINIMUM_VERSION, database)


class Neo4jStore:
    """One Neo4j database reached through a ``neo4j`` async driver.

    ``Neo4jStore(driver, database)`` wraps a driver the caller owns (``close`` leaves it open);
    ``Neo4jStore.from_config`` creates and owns one. The version is read by ``ensure_schema`` (the
    readiness ``check`` stays one cheap round trip).
    """

    def __init__(
        self,
        driver: Any,
        database: str | None = None,
        *,
        owns_driver: bool = False,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._owns_driver = owns_driver
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._version: str | None = None
        self._executor: BoltExecutor | None = None

    @classmethod
    def from_config(cls, config: Any) -> Neo4jStore:
        """A store with its own driver for ``config`` (``uri``, ``user``, ``password``, ``name`` = database)."""

        return cls(connect(config), config.name, owns_driver=True)

    @property
    def info(self) -> StoreInfo:
        return StoreInfo(BACKEND, CAPABILITIES, self._version)

    def executor(self) -> BoltExecutor:
        if self._executor is None:
            self._executor = BoltExecutor(
                self._driver,
                self._database,
                retry_delays=self._retry_delays,
                sleep=self._sleep,
                capabilities=CAPABILITIES,
                backend=BACKEND,
            )
        return self._executor

    async def check(self) -> None:
        await check_connection(self._driver, self._database, BACKEND)

    async def ensure_schema(
        self, ontology: Ontology, embedding_dim: int, *, ddl: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        found: list[tuple[int, ...]] = []

        async def gate(driver: Any, database: str | None) -> tuple[int, ...]:
            version = await _gate(driver, database)
            found.append(version)
            return version

        created = await ensure_cypher_schema(
            self._driver, ontology, embedding_dim, self._database, ddl=ddl, require_version=gate, backend=BACKEND
        )
        if found:
            self._version = ".".join(map(str, found[0]))
        return created

    async def close(self) -> None:
        if self._owns_driver:
            await self._driver.close()
