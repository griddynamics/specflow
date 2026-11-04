"""Graph stores: one folder per backend behind the ``GraphStore`` port (``graph.core.store``).

``build_store(config)`` is the one place that turns settings into a concrete store: it routes
``config.backend`` through ``_ADAPTERS`` and imports only the chosen adapter and its SDK (same shape as
``storage.factory``). A backend whose adapter is not shipped yet raises
``ConfigurationError("store_not_available")``; one whose SDK is not installed raises
``ConfigurationError("store_sdk_missing")`` naming the extra that installs it.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

# graph.core first: importing it loads the store port and the Bolt shims in an order that works whichever
# module of this package a process imports first (test_graph_stores.py runs each as the first import).
from graph.core.config import Neo4jConfig
from graph.core.errors import ConfigurationError
from graph.core.store import STORE_NOT_AVAILABLE, GraphStore
from graph.stores.config import BACKENDS, StoreConfig
from logger.logger import ERROR, get_logger, log_event

logger = get_logger("graph.stores")

STORE_SDK_MISSING: Final = "store_sdk_missing"

__all__ = ["BACKENDS", "StoreConfig", "build_store"]


@dataclass(frozen=True)
class _Adapter:
    module: str  # the adapter module, imported lazily
    cls: str  # the store class in it; ``cls.from_config(StoreConfig)`` builds the store
    extra: str  # the pyproject extra that installs the SDK ("" when the base install has it)
    sdk_roots: tuple[str, ...]  # top-level modules of the SDK


_ADAPTERS: Final[dict[str, _Adapter]] = {
    "neo4j": _Adapter("graph.stores.neo4j", "Neo4jStore", "", ("neo4j",)),
    "arcadedb": _Adapter("graph.stores.arcadedb", "ArcadeDbStore", "", ("neo4j", "httpx")),
    "postgres": _Adapter("graph.stores.postgres", "PostgresStore", "", ("sqlalchemy", "asyncpg")),
    "tigergraph": _Adapter("graph.stores.tigergraph", "TigerGraphStore", "tigergraph", ("pyTigerGraph",)),
}


def _load(backend: str) -> Callable[[StoreConfig], GraphStore]:
    adapter = _ADAPTERS[backend]
    try:
        module = importlib.import_module(adapter.module)
    except ImportError as error:
        name = error.name or ""
        if name == adapter.module:
            raise ConfigurationError(STORE_NOT_AVAILABLE, store=backend) from error
        # Only a missing SDK is a configuration problem; any other import failure is a bug.
        if name.split(".")[0] not in adapter.sdk_roots:
            raise
        log_event(logger, ERROR, "graph.store.sdk_missing", store=backend, install_extra=adapter.extra)
        raise ConfigurationError(STORE_SDK_MISSING, store=backend, install_extra=adapter.extra) from error
    store_class = getattr(module, adapter.cls)
    return cast(Callable[[StoreConfig], GraphStore], store_class.from_config)


def build_store(config: StoreConfig | Neo4jConfig) -> GraphStore:
    """The store for ``config.backend`` (a ``Neo4jConfig`` is the Neo4j store's settings, deprecated).

    No connection is opened until first use. Raises ``ConfigurationError`` with ``store_not_available``
    for a backend without an adapter and ``store_sdk_missing`` for one whose SDK is not installed.
    """

    settings = StoreConfig.from_neo4j(config) if isinstance(config, Neo4jConfig) else config
    return _load(settings.backend)(settings)
