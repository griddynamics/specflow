"""ArcadeDB store: the ``GraphStore`` of an ArcadeDB 26.10 server (Bolt plugin on), Cypher plus overrides.

Queries run over Bolt on the shared machinery of ``graph.stores._bolt`` with the ArcadeDB dialect (the
canonical Cypher and per-template overrides); the schema, the real server version and the database
itself are managed over the HTTP admin API (``admin.py``).
"""

from __future__ import annotations

from graph.stores.arcadedb.admin import MINIMUM_VERSION, AdminClient, admin_url
from graph.stores.arcadedb.dialect import ARCADEDB_DIALECT, ArcadeDbDialect
from graph.stores.arcadedb.executor import BACKEND, CAPABILITIES, ArcadeDbExecutor
from graph.stores.arcadedb.schema import ArcadeObject, schema_objects
from graph.stores.arcadedb.store import DEFAULT_DATABASE, ArcadeDbStore

__all__ = [
    "ARCADEDB_DIALECT",
    "BACKEND",
    "CAPABILITIES",
    "DEFAULT_DATABASE",
    "MINIMUM_VERSION",
    "AdminClient",
    "ArcadeDbDialect",
    "ArcadeDbExecutor",
    "ArcadeDbStore",
    "ArcadeObject",
    "admin_url",
    "schema_objects",
]
