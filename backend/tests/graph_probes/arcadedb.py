"""The ArcadeDB probe: the live graph tests' raw statements for an ArcadeDB 26.10 server.

ArcadeDB speaks the Cypher of the Neo4j probe over Bolt, so this probe is that one with the differences
of the server: schema objects and index dimensions are read over the HTTP admin API (``SHOW INDEXES`` has
no options for a vector index), the derived ``spec_key`` property the adapter keeps on spec nodes is hidden
from every read (it is the adapter's, not data), an index drop is SQL, and there is nothing to wait for
after an index create (it is synchronous). ``(group_id, spec_id)`` uniqueness is reported as the logical
unique object it realises.
"""

from __future__ import annotations

from typing import Any, Final

from graph.core.queries import Query
from graph.stores.arcadedb.admin import AdminClient, admin_url
from graph.stores.arcadedb.names import index_names
from graph.stores.arcadedb.schema import SPEC_KEY, SPEC_KEY_INDEX
from graph.stores.config import StoreConfig
from graph_probes.base import ProbeEdge, ProbeNode, SchemaObject, identifier
from graph_probes.neo4j import Neo4jProbe

_LIBRARY_EDGE_KEY_INDEX: Final = "graph_edge_key_{}"
_FULLTEXT: Final = "FULL_TEXT"
_VECTOR: Final = "LSM_VECTOR"


def _visible(node: ProbeNode) -> ProbeNode:
    """``node`` without the adapter's derived ``spec_key``."""

    if SPEC_KEY not in node.props:
        return node
    return ProbeNode(node.id, node.labels, {k: v for k, v in node.props.items() if k != SPEC_KEY})


class ArcadeDbProbe(Neo4jProbe):
    backend = "arcadedb"

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self._admin = AdminClient(admin_url(config.uri, config.admin_uri), config.user, config.password)

    async def _close(self) -> None:
        await self._admin.close()
        await super()._close()

    # -- reads -------------------------------------------------------------------------------------

    async def nodes(self, project_id: str, label: str | None = None, **where: Any) -> list[ProbeNode]:
        return [_visible(node) for node in await super().nodes(project_id, label, **where)]

    async def edges(self, project_id: str, relation: str | None = None, **where: Any) -> list[ProbeEdge]:
        return [
            ProbeEdge(edge.id, edge.type, _visible(edge.source), _visible(edge.target), edge.props)
            for edge in await super().edges(project_id, relation, **where)
        ]

    # -- fault injection ---------------------------------------------------------------------------

    def failing_write_query(self, name: str) -> Query:
        # Two nodes with one key in one statement: the unique (group_id, uuid) index refuses the second.
        return Query.literal(
            name,
            "UNWIND $rows AS row WITH row WHERE $project_id IS NOT NULL "
            "CREATE (a:SpecNode {group_id: $project_id, uuid: '__duplicate__'}), "
            "(b:SpecNode {group_id: $project_id, uuid: '__duplicate__'}) RETURN 1 AS written, [] AS refs",
        )

    # -- schema ------------------------------------------------------------------------------------

    async def _types(self) -> list[dict[str, Any]]:
        return await self._admin.types(self.database or "specflow")

    async def schema_objects(self) -> list[SchemaObject]:
        objects: list[SchemaObject] = []
        for entry in await self._types():
            entity = "relationship" if entry.get("type") == "edge" else "node"
            for index in entry.get("indexes", ()):
                properties = tuple(index.get("properties", ()))
                kind = index["type"]
                if kind == _VECTOR:
                    shape = (
                        "vector",
                        index.get("dimensions"),
                        str(index.get("scoring", "")).rsplit(":", 1)[-1].lower(),
                    )
                elif kind == _FULLTEXT:
                    shape = ("fulltext", None, None)
                elif index.get("unique"):
                    shape = ("unique", None, None)
                else:
                    shape = ("range", None, None)
                name = index["name"]
                if properties == (SPEC_KEY,):  # the logical (group_id, spec_id) uniqueness
                    name, properties = SPEC_KEY_INDEX, ("group_id", "spec_id")
                objects.append(
                    SchemaObject(
                        name=name,
                        kind=shape[0],
                        entity=entity,
                        label=entry["name"],
                        properties=properties,
                        online=True,  # an index is usable when its create returns
                        dimensions=int(shape[1]) if shape[1] is not None else None,
                        similarity=shape[2],
                    )
                )
        return objects

    async def drop_edge_key_index(self, relation: str) -> str | None:
        name = _LIBRARY_EDGE_KEY_INDEX.format(identifier(relation).lower())
        if not [o for o in await self.schema_objects() if o.name == name]:
            return None
        await self._admin.sql(self.database or "specflow", f"DROP INDEX `{name}`")
        return name

    async def vector_index_dimension(self, name: str) -> int | None:
        wanted = index_names().get(name, name)
        for entry in await self._types():
            for index in entry.get("indexes", ()):
                if index["name"] == wanted and index.get("dimensions") is not None:
                    return int(index["dimensions"])
        return None

    async def await_indexes(self, timeout_s: float = 60.0) -> None:
        del timeout_s  # index builds are synchronous: nothing to wait for

    async def server_edition(self) -> str:
        return "arcadedb"
