"""Compatibility shim: the Neo4j access of earlier releases, now spread over the store port.

Kept for one release so existing imports (``graph.core.db``, ``graph.core``) keep working. The port is
``graph.core.store``; the Bolt executor lives in ``graph.stores._bolt.executor``; the Neo4j version gate in
``graph.stores.neo4j``. ``Executor`` and ``ScopedTransaction`` are the port's protocols: build one from a
driver with ``graph.stores.neo4j.Neo4jStore(driver, database).executor()``.
"""

from __future__ import annotations

import importlib
from typing import Any

from graph.core.store import RETRY_DELAYS_SECONDS, Executor, Record, ScopedTransaction, check

# The names below load the Neo4j SDK, so they resolve on first access (PEP 562): importing this module, or
# ``graph.core``, must not import ``neo4j``.
_LAZY: dict[str, str] = {
    "CONNECTION_TIMEOUT_SECONDS": "graph.stores._bolt.executor",
    "AsyncGraphDatabase": "graph.stores._bolt.executor",
    "BoltExecutor": "graph.stores._bolt.executor",
    "BoltScopedTransaction": "graph.stores._bolt.executor",
    "_bind": "graph.stores._bolt.executor",
    "_map_error": "graph.stores._bolt.executor",
    "connect": "graph.stores._bolt.executor",
    "KERNEL_COMPONENT": "graph.stores.neo4j.version",
    "MINIMUM_VERSION": "graph.stores.neo4j.version",
    "require_version": "graph.stores.neo4j.version",
    "server_version": "graph.stores.neo4j.version",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


# The lazily resolved names (see above) stay out of ``__all__``: the linter cannot see them defined.
__all__ = [
    "RETRY_DELAYS_SECONDS",
    "Executor",
    "Record",
    "ScopedTransaction",
    "check",
]
