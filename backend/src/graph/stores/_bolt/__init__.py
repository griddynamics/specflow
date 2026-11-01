"""Shared Bolt machinery: executor, Cypher dialect and Cypher schema manager (the ``neo4j`` driver).

Used by every store that speaks Bolt and Cypher; a backend folder (``graph.stores.neo4j``) adds its
version gate, error codes and defaults on top.
"""

from __future__ import annotations
