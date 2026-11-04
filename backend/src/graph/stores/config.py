"""Store settings: which backend and how to reach it (data only; the host maps its settings onto it).

``StoreConfig`` follows the library's settings pattern (``graph.core.config.FrozenModel``): frozen, no
unknown fields, invalid values raise ``ConfigurationError`` naming the fields, never their values. The
library never reads environment variables; the secret is a ``SecretStr``. What ``uri`` and ``name`` mean
is the backend's (database for Neo4j and ArcadeDB, schema for PostgreSQL, graph for TigerGraph).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, SecretStr

from graph.core.config import FrozenModel, Neo4jConfig

Backend = Literal["neo4j", "arcadedb", "postgres", "tigergraph"]
BACKENDS: Final[tuple[Backend, ...]] = ("neo4j", "arcadedb", "postgres", "tigergraph")
DEFAULT_BACKEND: Final[Backend] = "neo4j"


class StoreConfig(FrozenModel):
    """One graph store connection.

    ``name`` None means the backend's default (Neo4j: the server's home database). ``admin_uri`` is an
    optional second endpoint for backends with a separate admin API; ``pool_size`` an optional cap on
    the adapter's own connection pool.
    """

    backend: Backend = DEFAULT_BACKEND
    uri: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr
    name: str | None = None
    admin_uri: str | None = Field(default=None, min_length=1)
    pool_size: int | None = Field(default=None, gt=0)

    @classmethod
    def from_neo4j(cls, config: Neo4jConfig) -> StoreConfig:
        """The Neo4j store settings of a ``Neo4jConfig`` (``database`` becomes ``name``)."""

        return cls(
            backend="neo4j",
            uri=config.uri,
            user=config.user,
            password=config.password,
            name=config.database,
        )
