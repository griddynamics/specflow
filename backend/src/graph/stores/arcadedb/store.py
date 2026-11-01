"""The ArcadeDB store: a ``GraphStore`` over Bolt (queries) and the HTTP admin API (schema, version)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from urllib.parse import urlsplit

from graph.core.ontology import Ontology
from graph.core.store import RETRY_DELAYS_SECONDS, StoreInfo
from graph.stores._bolt.executor import check_connection, connect
from graph.stores.arcadedb.admin import AdminClient, admin_url
from graph.stores.arcadedb.dialect import verify_coverage
from graph.stores.arcadedb.executor import BACKEND, CAPABILITIES, ArcadeDbExecutor
from graph.stores.arcadedb.schema import ensure_arcadedb_schema

DEFAULT_DATABASE: Final = "specflow"


class ArcadeDbStore:
    """One ArcadeDB database reached through a ``neo4j`` async driver (Bolt plugin) and the HTTP admin API.

    ``ArcadeDbStore(driver, admin, database)`` wraps a driver and an admin client the caller owns
    (``close`` leaves them open); ``ArcadeDbStore.from_config`` creates and owns both. The server version
    is read by ``ensure_schema`` (the readiness ``check`` stays one cheap Bolt round trip). The database
    name is always passed to the driver: a pooled connection otherwise keeps the one it used before.
    Construction refuses to proceed when a required dialect override is missing (``verify_coverage``).
    """

    def __init__(
        self,
        driver: Any,
        admin: AdminClient,
        database: str = DEFAULT_DATABASE,
        *,
        owns_clients: bool = False,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        verify_coverage()
        self._driver = driver
        self._admin = admin
        self._database = database
        self._owns_clients = owns_clients
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._version: str | None = None
        self._executor: ArcadeDbExecutor | None = None

    @classmethod
    def from_config(cls, config: Any) -> ArcadeDbStore:
        """A store with its own driver and admin client for ``config``: ``uri`` (Bolt), ``user``,
        ``password``, ``name`` = database (default ``specflow``), ``admin_uri`` = HTTP endpoint (default:
        the Bolt host on port 2480)."""

        insecure = urlsplit(config.uri).scheme.endswith("+ssc")
        admin = AdminClient(
            admin_url(config.uri, config.admin_uri), config.user, config.password, verify=not insecure
        )
        return cls(connect(config), admin, config.name or DEFAULT_DATABASE, owns_clients=True)

    @property
    def info(self) -> StoreInfo:
        return StoreInfo(BACKEND, CAPABILITIES, self._version)

    def executor(self) -> ArcadeDbExecutor:
        if self._executor is None:
            self._executor = ArcadeDbExecutor(
                self._driver, self._database, retry_delays=self._retry_delays, sleep=self._sleep
            )
        return self._executor

    async def check(self) -> None:
        await check_connection(self._driver, self._database, BACKEND)

    async def ensure_schema(
        self, ontology: Ontology, embedding_dim: int, *, ddl: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        created, version = await ensure_arcadedb_schema(
            self._driver, self._admin, self._database, ontology, embedding_dim, ddl=ddl
        )
        self._version = ".".join(map(str, version))
        return created

    async def close(self) -> None:
        if self._owns_clients:
            await self._driver.close()
            await self._admin.close()
