"""Compatibility shim: the Neo4j access of earlier releases, now spread over the store port.

Kept for one release so existing imports (``graph.core.db``, ``graph.core``) keep working. The port is
``graph.core.store``; the Bolt executor lives in ``graph.stores._bolt.executor``; the Neo4j version gate in
``graph.stores.neo4j``. ``Executor`` and ``ScopedTransaction`` are the port's protocols: build one from a
driver with ``graph.stores.neo4j.Neo4jStore(driver, database).executor()``.
"""

from __future__ import annotations

from graph.core.store import RETRY_DELAYS_SECONDS, Executor, Record, ScopedTransaction, check
from graph.stores._bolt.executor import (
    CONNECTION_TIMEOUT_SECONDS,
    AsyncGraphDatabase,
    BoltExecutor,
    BoltScopedTransaction,
    _bind,
    _map_error,
    connect,
)
from graph.stores.neo4j.version import KERNEL_COMPONENT, MINIMUM_VERSION, require_version, server_version

__all__ = [
    "CONNECTION_TIMEOUT_SECONDS",
    "KERNEL_COMPONENT",
    "MINIMUM_VERSION",
    "RETRY_DELAYS_SECONDS",
    "AsyncGraphDatabase",
    "BoltExecutor",
    "BoltScopedTransaction",
    "Executor",
    "Record",
    "ScopedTransaction",
    "_bind",
    "_map_error",
    "check",
    "connect",
    "require_version",
    "server_version",
]
