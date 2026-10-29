"""An in-memory ``ExtractionStore`` shared by the graph build step tests. No tests here.

``test_graph_graphiti``, ``test_graph_resolve`` and ``test_graph_conform`` hand it to the build steps
(``StoredResults``, ``VectorCache``, decision memories) and read its entries back.
"""

from __future__ import annotations

import copy
from typing import Any


class MemoryStore:
    """In-memory ``ExtractionStore`` (get/put only) whose entries a test can inspect."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    async def get(self, kind: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        found = self.entries.get((kind, key))
        return copy.deepcopy(found[1]) if found and found[0] == fingerprint else None

    async def put(self, kind: str, key: str, fingerprint: str, payload: dict[str, Any]) -> None:
        self.entries[(kind, key)] = (fingerprint, copy.deepcopy(payload))

    def of(self, kind: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """The entries of one kind, by key."""

        return {key: value for (found, key), value in self.entries.items() if found == kind}
