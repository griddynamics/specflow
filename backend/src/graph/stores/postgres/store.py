"""The PostgreSQL store: a ``GraphStore`` over a schema of a PostgreSQL database with pgvector (design D7).

One store is one schema (``StoreConfig.name``, default ``graph``) in the database ``StoreConfig.uri`` names, reached
through the store's own small pool. The administrator creates the ``vector`` extension once per database and gives
the login ``CREATE`` on it; the store creates the schema, tables and indexes on first use and never the extension.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from graph.core.errors import ConfigurationError
from graph.core.ontology import Ontology
from graph.core.store import RETRY_DELAYS_SECONDS, StoreInfo
from graph.stores.config import StoreConfig
from graph.stores.postgres.dialect import BACKEND, DEFAULT_SCHEMA, SCHEMA_NAME, PostgresDialect
from graph.stores.postgres.executor import CAPABILITIES, PostgresExecutor, build_engine, check_connection
from graph.stores.postgres.schema import ensure_postgres_schema

__all__ = ["BACKEND", "CAPABILITIES", "PostgresStore"]

STORE_NAME_INVALID: Final = "store_name_invalid"


class PostgresStore:
    """One graph schema of a PostgreSQL database.

    ``PostgresStore(engine, schema)`` wraps an engine the caller owns (``close`` leaves it open);
    ``PostgresStore.from_config`` creates and owns one. The server version is read by ``ensure_schema``
    (the readiness ``check`` stays one cheap round trip).
    """

    def __init__(
        self,
        engine: AsyncEngine,
        schema: str | None = None,
        *,
        owns_engine: bool = False,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        name = schema or DEFAULT_SCHEMA
        if not SCHEMA_NAME.fullmatch(name):
            raise ConfigurationError(STORE_NAME_INVALID, store=BACKEND)
        self._engine = engine
        self._dialect = PostgresDialect(name)
        self._owns_engine = owns_engine
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._version: str | None = None
        self._executor: PostgresExecutor | None = None

    @classmethod
    def from_config(cls, config: StoreConfig) -> PostgresStore:
        """A store with its own engine for ``config`` (``uri`` the database, ``name`` the schema)."""

        name = config.name or DEFAULT_SCHEMA
        if not SCHEMA_NAME.fullmatch(name):
            raise ConfigurationError(STORE_NAME_INVALID, store=BACKEND)
        return cls(build_engine(config), name, owns_engine=True)

    @property
    def schema(self) -> str:
        return self._dialect.schema

    @property
    def info(self) -> StoreInfo:
        return StoreInfo(BACKEND, CAPABILITIES, self._version)

    def executor(self) -> PostgresExecutor:
        if self._executor is None:
            self._executor = PostgresExecutor(
                self._engine,
                self._dialect,
                retry_delays=self._retry_delays,
                sleep=self._sleep,
                capabilities=CAPABILITIES,
                backend=BACKEND,
            )
        return self._executor

    async def check(self) -> None:
        await check_connection(self._engine, self._dialect, BACKEND)

    async def ensure_schema(
        self, ontology: Ontology, embedding_dim: int, *, ddl: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        created, version = await ensure_postgres_schema(self._engine, self._dialect, ontology, embedding_dim, ddl=ddl)
        self._version = version
        return created

    async def close(self) -> None:
        if self._owns_engine:
            await self._engine.dispose()
