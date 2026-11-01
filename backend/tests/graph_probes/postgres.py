"""The PostgreSQL probe: every raw SQL statement of the live graph tests lives in this file.

Statements are written against the store's schema (``StoreConfig.name``, default ``graph``) over the probe's
own small pool; the code under test uses ``probe.store``. A node is a row of ``graph.node``: its ``SpecNode``
label is its ``kind``, its other labels are the ``labels`` array, its properties are ``props`` plus the
``group_id`` column and the ``embedding`` column. Values are always parameters; labels, relationship types and
property names too (they are bind values in this store).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from graph.core import load_ontology
from graph.core.queries import LiteralText, Query
from graph.stores.config import StoreConfig
from graph.stores.postgres.dialect import render_schema
from graph.stores.postgres.executor import build_engine
from graph_probes.base import (
    VECTOR_PROPERTIES,
    EdgeSpec,
    GraphProbe,
    Match,
    NodeSpec,
    ProbeEdge,
    ProbeError,
    ProbeNode,
    SchemaObject,
    check_flat,
)

SPEC_NODE: Final = "SpecNode"
DEFAULT_SCHEMA: Final = "graph"
POOL_SIZE: Final = 2
EMBEDDING: Final = "embedding"
NAME_EMBEDDING: Final = "name_embedding"
EDGE_KEY_INDEX: Final = "edge_key_idx"
EMBEDDING_INDEX: Final = "graph_spec_node_embedding"
NAME_EMBEDDING_INDEX: Final = "graph_spec_node_name_embedding"
DIMENSION_KEY: Final = "embedding_dim"
DIMENSION_ENV: Final = "GRAPH_EMBEDDING_DIM"
DATA_MODEL_DIM: Final = "3072"
_KINDS: Final = {"Document": "document", "Fragment": "fragment"}

# The rows of one node and of the two ends of an edge (the vector columns are read as real arrays).
NODE_COLUMNS: Final = "n.id, n.kind, n.labels, n.props, n.group_id, CAST(n.embedding AS real[]) AS vec"


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _kind(labels: Sequence[str]) -> str:
    """The stored kind of a node with these labels."""

    if SPEC_NODE in labels:
        return "spec"
    for label, kind in _KINDS.items():
        if label in labels:
            return kind
    return "foreign"


def _stored_labels(labels: Sequence[str]) -> list[str]:
    return [label for label in labels if label != SPEC_NODE]


def _vector_text(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def _node_conditions(alias: str, label: str | None, where: Mapping[str, Any], tag: str) -> tuple[list[str], dict[str, Any]]:
    """Conditions selecting nodes of one project (parameter ``p``) by label and equal properties."""

    conditions = [f"{alias}.group_id = :p"]
    params: dict[str, Any] = {}
    if label == SPEC_NODE:
        conditions.append(f"{alias}.kind = 'spec'")
    elif label is not None:
        conditions.append(f"CAST(:{tag}label AS text) = ANY({alias}.labels)")
        params[f"{tag}label"] = label
    for index, (name, value) in enumerate(where.items()):
        if value is None:
            conditions.append("FALSE")  # an equality with null never holds (Cypher)
            continue
        if name == "group_id":
            conditions.append(f"{alias}.group_id = :{tag}v{index}")
            params[f"{tag}v{index}"] = value
            continue
        conditions.append(f"{alias}.props -> :{tag}k{index} = CAST(:{tag}v{index} AS jsonb)")
        params[f"{tag}k{index}"] = name
        params[f"{tag}v{index}"] = _json(value)
    return conditions, params


def _node(row: Mapping[str, Any], *, vectors: bool = True) -> ProbeNode:
    labels = sorted([*([SPEC_NODE] if row["kind"] == "spec" else []), *row["labels"]])
    props = dict(row["props"])
    props["group_id"] = row["group_id"]
    if not vectors:
        for name in VECTOR_PROPERTIES:
            props.pop(name, None)
    elif row["vec"] is not None:
        props[EMBEDDING] = [float(f"{value:.7g}") for value in row["vec"]]  # a float4 holds 7 digits
    return ProbeNode(id=str(row["id"]), labels=tuple(labels), props=props)


class PostgresProbe(GraphProbe):
    backend = "postgres"
    lucene_semantics = False

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self._schema = config.name or DEFAULT_SCHEMA
        self._engine: AsyncEngine = build_engine(config, pool_size=POOL_SIZE)
        self._ready = False

    async def _close(self) -> None:
        await self._engine.dispose()

    async def _execute(
        self, sql: str, params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        async with self._engine.begin() as connection:
            result = await connection.execute(text(render_schema(sql, self._schema)), params or {})
            return [dict(row) for row in result.mappings().all()] if result.returns_rows else []

    async def _ensure_schema(self) -> None:
        """A fresh schema is created on first use (a Neo4j database keeps its indexes between runs; a new
        PostgreSQL schema has none): with the dimension of ``GRAPH_EMBEDDING_DIM``, else the data model's."""

        found = await self._execute(
            "SELECT 1 AS present FROM pg_class AS c JOIN pg_namespace AS ns ON ns.oid = c.relnamespace "
            "WHERE ns.nspname = :schema AND c.relname = 'node'",
            {"schema": self._schema},
        )
        if not found:
            await self.store.ensure_schema(load_ontology(submodule_files=[]), int(os.environ.get(DIMENSION_ENV, DATA_MODEL_DIM)))
        self._ready = True

    async def run(
        self, sql: str, params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Run one statement (the schema name rendered into it) in its own transaction; its rows."""

        if not self._ready:
            await self._ensure_schema()
        return await self._execute(sql, params)

    # -- reads -------------------------------------------------------------------------------------

    async def nodes(self, project_id: str, label: str | None = None, **where: Any) -> list[ProbeNode]:
        conditions, params = _node_conditions("n", label, where, "w")
        rows = await self.run(
            f"SELECT {NODE_COLUMNS} FROM graph.node AS n WHERE {' AND '.join(conditions)}", {"p": project_id, **params}
        )
        return sorted((_node(row) for row in rows), key=lambda n: n.id)

    async def edges(self, project_id: str, relation: str | None = None, **where: Any) -> list[ProbeEdge]:
        clauses = ["(s.group_id = :p OR t.group_id = :p OR e.group_id = :p)"]
        params: dict[str, Any] = {"p": project_id}
        if relation is not None:
            clauses.append("e.rel_type = :relation")
            params["relation"] = relation
        for index, (name, value) in enumerate(where.items()):
            if value is None:
                clauses.append("FALSE")
            elif name == "group_id":
                clauses.append(f"e.group_id = :v{index}")
                params[f"v{index}"] = value
            else:
                clauses.append(f"e.props -> :k{index} = CAST(:v{index} AS jsonb)")
                params[f"k{index}"] = name
                params[f"v{index}"] = _json(value)
        rows = await self.run(
            "SELECT e.id, e.rel_type, e.group_id, e.props, "
            "s.id AS sid, s.kind AS skind, s.labels AS slabels, s.props AS sprops, s.group_id AS sgroup, "
            "t.id AS tid, t.kind AS tkind, t.labels AS tlabels, t.props AS tprops, t.group_id AS tgroup "
            "FROM graph.edge AS e JOIN graph.node AS s ON s.id = e.source_id JOIN graph.node AS t ON t.id = e.target_id "
            f"WHERE {' AND '.join(clauses)}",
            params,
        )

        def end(row: Mapping[str, Any], side: str) -> ProbeNode:
            return _node(
                {
                    "id": row[f"{side}id"],
                    "kind": row[f"{side}kind"],
                    "labels": row[f"{side}labels"],
                    "props": row[f"{side}props"],
                    "group_id": row[f"{side}group"],
                    "vec": None,
                },
                vectors=False,
            )

        found = [
            ProbeEdge(
                str(row["id"]),
                row["rel_type"],
                end(row, "s"),
                end(row, "t"),
                {**row["props"], "group_id": row["group_id"]},
            )
            for row in rows
        ]
        return sorted(found, key=lambda e: e.id)

    async def count_nodes(self, project_id: str) -> int:
        rows = await self.run("SELECT count(*) AS n FROM graph.node WHERE group_id = :p", {"p": project_id})
        return int(rows[0]["n"])

    # -- seeding and fault injection ---------------------------------------------------------------

    async def create_nodes(self, project_id: str, specs: Sequence[NodeSpec]) -> int:
        rows: list[dict[str, Any]] = []
        for spec in specs:
            check_flat(spec.props)
            props = {name: value for name, value in spec.props.items() if value is not None}
            props.pop("group_id", None)
            vector = props.pop(EMBEDDING, None)
            rows.append(
                {
                    "p": project_id,
                    "kind": _kind(spec.labels),
                    "labels": _stored_labels(spec.labels),
                    "props": _json(props),
                    "vec": None if vector is None else _vector_text(vector),
                }
            )
        if rows:
            await self.run(
                "INSERT INTO graph.node (group_id, kind, labels, props, embedding) "
                "VALUES (:p, :kind, CAST(:labels AS text[]), CAST(:props AS jsonb), CAST(:vec AS vector))",
                rows,
            )
        return len(rows)

    async def _one_id(self, project_id: str, match: Match, what: str) -> int:
        conditions, params = _node_conditions("n", match.label, match.props, "w")
        rows = await self.run(
            f"SELECT n.id FROM graph.node AS n WHERE {' AND '.join(conditions)} LIMIT 2", {"p": project_id, **params}
        )
        if len(rows) != 1:
            raise ProbeError(f"{what}: the end must match one node, found {len(rows)}")
        return int(rows[0]["id"])

    async def create_edges(self, project_id: str, specs: Sequence[EdgeSpec]) -> int:
        rows: list[dict[str, Any]] = []
        for spec in specs:
            check_flat(spec.props)
            props = {name: value for name, value in spec.props.items() if value is not None}
            group = props.pop("group_id", project_id)
            rows.append(
                {
                    "g": group,
                    "type": spec.type,
                    "s": await self._one_id(project_id, spec.source, spec.type),
                    "t": await self._one_id(project_id, spec.target, spec.type),
                    "props": _json(props),
                }
            )
        if rows:
            await self.run(
                "INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props) "
                "VALUES (:g, :type, :s, :t, CAST(:props AS jsonb))",
                rows,
            )
        return len(rows)

    async def update_nodes(
        self,
        project_id: str,
        match: Match,
        props: Mapping[str, Any] | None = None,
        *,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> int:
        check_flat(props or {})
        patch = dict(props or {})
        has_vector = EMBEDDING in patch
        vector = patch.pop(EMBEDDING, None)
        conditions, params = _node_conditions("n", match.label, match.props, "w")
        rows = await self.run(
            "UPDATE graph.node AS n SET props = graph.props_merge(n.props, CAST(:patch AS jsonb)), "
            "embedding = CASE WHEN :has_vector THEN CAST(:vec AS vector) ELSE n.embedding END, "
            "labels = ARRAY(SELECT DISTINCT l FROM unnest(n.labels || CAST(:added AS text[])) AS l "
            "WHERE NOT l = ANY(CAST(:removed AS text[])) ORDER BY l) "
            f"WHERE {' AND '.join(conditions)} RETURNING n.id",
            {
                "p": project_id,
                "patch": _json(patch),
                "has_vector": has_vector,
                "vec": None if vector is None else _vector_text(vector),
                "added": [label for label in add_labels if label != SPEC_NODE],
                "removed": [label for label in remove_labels if label != SPEC_NODE],
                **params,
            },
        )
        return len(rows)

    async def delete_nodes(self, project_id: str, match: Match) -> int:
        conditions, params = _node_conditions("n", match.label, match.props, "w")
        rows = await self.run(
            f"DELETE FROM graph.node AS n WHERE {' AND '.join(conditions)} RETURNING n.id", {"p": project_id, **params}
        )
        return len(rows)

    async def update_edges(self, edge_ids: Sequence[str], props: Mapping[str, Any]) -> int:
        check_flat(props)
        rows = await self.run(
            "UPDATE graph.edge AS e SET props = graph.props_merge(e.props, CAST(:patch AS jsonb)) "
            "WHERE e.id = ANY(CAST(:ids AS bigint[])) RETURNING e.id",
            {"patch": _json(dict(props)), "ids": [int(edge_id) for edge_id in edge_ids]},
        )
        return len(rows)

    async def delete_edges(self, edge_ids: Sequence[str]) -> int:
        rows = await self.run(
            "DELETE FROM graph.edge AS e WHERE e.id = ANY(CAST(:ids AS bigint[])) RETURNING e.id",
            {"ids": [int(edge_id) for edge_id in edge_ids]},
        )
        return len(rows)

    def failing_write_query(self, name: str) -> Query:
        return Query(
            name,
            "core.literal",
            LiteralText(
                "SELECT 1 / 0 AS written, CAST(ARRAY[] AS text[]) AS refs "
                "FROM jsonb_array_elements(CAST(:rows AS jsonb)) AS r WHERE CAST(:project_id AS text) IS NOT NULL"
            ),
        )

    # -- schema ------------------------------------------------------------------------------------

    async def _recorded_dimension(self) -> int | None:
        found = await self.run(
            "SELECT 1 AS present FROM pg_class AS c JOIN pg_namespace AS ns ON ns.oid = c.relnamespace "
            "WHERE ns.nspname = :schema AND c.relname = 'meta'",
            {"schema": self._schema},
        )
        if not found:
            return None
        rows = await self.run("SELECT meta_value FROM graph.meta WHERE meta_key = :k", {"k": DIMENSION_KEY})
        return int(rows[0]["meta_value"]) if rows else None

    async def schema_objects(self) -> list[SchemaObject]:
        """The store's objects in the neutral terms of the contract: its unique and range indexes as they are,
        the generated ``tsvector`` columns as the full-text objects, the vector column as two vector objects
        of the recorded dimension, and the one edge-key index as a range object per relation type."""

        indexes = await self.run(
            "SELECT c.relname AS index_name, t.relname AS table_name, x.indisunique AS is_unique, "
            "x.indisvalid AS is_valid, pg_get_indexdef(x.indexrelid) AS definition "
            "FROM pg_index AS x JOIN pg_class AS c ON c.oid = x.indexrelid JOIN pg_class AS t ON t.oid = x.indrelid "
            "JOIN pg_namespace AS ns ON ns.oid = c.relnamespace WHERE ns.nspname = :schema",
            {"schema": self._schema},
        )
        by_name = {row["index_name"]: row for row in indexes}
        objects: list[SchemaObject] = []

        def unique(name: str, label: str, properties: tuple[str, ...]) -> None:
            row = by_name.get(name)
            if row is not None:
                objects.append(SchemaObject(name, "unique", "node", label, properties, bool(row["is_valid"])))

        unique("node_spec_specid_uq", SPEC_NODE, ("group_id", "spec_id"))
        unique("node_spec_uuid_uq", SPEC_NODE, ("group_id", "uuid"))
        unique("node_document_uq", "Document", ("group_id", "document_id"))
        unique("node_fragment_uq", "Fragment", ("group_id", "fragment_id"))
        identity = by_name.get("node_identity_key_idx")
        if identity is not None:
            objects.append(
                SchemaObject(
                    "node_identity_key_idx", "range", "node", SPEC_NODE, ("group_id", "identity_key"), bool(identity["is_valid"])
                )
            )
        columns = {
            row["column_name"]
            for row in await self.run(
                "SELECT a.attname AS column_name FROM pg_attribute AS a JOIN pg_class AS c ON c.oid = a.attrelid "
                "JOIN pg_namespace AS ns ON ns.oid = c.relnamespace "
                "WHERE ns.nspname = :schema AND c.relname = 'node' AND a.attnum > 0 AND NOT a.attisdropped",
                {"schema": self._schema},
            )
        }
        if {"name_tsv", "stmt_tsv"} <= columns:
            objects.append(SchemaObject("node.name_tsv+stmt_tsv", "fulltext", "node", SPEC_NODE, ("name", "statement_text"), True))
        if "text_tsv" in columns:
            objects.append(SchemaObject("node.text_tsv", "fulltext", "node", "Fragment", ("text",), True))
        dimension = await self._recorded_dimension()
        if "embedding" in columns and dimension is not None:
            for name, prop in ((EMBEDDING_INDEX, EMBEDDING), (NAME_EMBEDDING_INDEX, NAME_EMBEDDING)):
                objects.append(SchemaObject(name, "vector", "node", SPEC_NODE, (prop,), True, dimension, "cosine"))
        if EDGE_KEY_INDEX in by_name:
            online = bool(by_name[EDGE_KEY_INDEX]["is_valid"])
            seen = {row["rel_type"] for row in await self.run("SELECT DISTINCT rel_type FROM graph.edge")}
            relations = {*load_ontology(submodule_files=[]).relations, *seen}
            for relation in sorted(relations):
                objects.append(SchemaObject(EDGE_KEY_INDEX, "range", "relationship", relation, ("group_id", "edge_key"), online))
        return objects

    async def drop_edge_key_index(self, relation: str) -> str | None:
        return None  # one index serves every relation type, under a name that is not the library's per-relation one

    async def vector_index_dimension(self, name: str) -> int | None:
        if name not in (EMBEDDING_INDEX, NAME_EMBEDDING_INDEX):
            return None
        return await self._recorded_dimension()

    async def await_indexes(self, timeout_s: float = 60.0) -> None:
        return None  # indexes are built inside the statement that creates them; a read sees every committed write

    async def server_edition(self) -> str:
        return "postgresql"

    # -- cleanup and isolation ---------------------------------------------------------------------

    async def delete_projects(self, project_ids: Sequence[str]) -> int:
        ids = list(project_ids)
        await self.run("DELETE FROM graph.node WHERE group_id = ANY(CAST(:ids AS text[]))", {"ids": ids})
        await self.run("DELETE FROM graph.edge WHERE group_id = ANY(CAST(:ids AS text[]))", {"ids": ids})
        rows = await self.run("SELECT count(*) AS n FROM graph.node WHERE group_id = ANY(CAST(:ids AS text[]))", {"ids": ids})
        return int(rows[0]["n"])

    async def outside_counts(self, prefix: str) -> dict[str, int]:
        rows = await self.run(
            "SELECT "
            "(SELECT count(*) FROM graph.node WHERE group_id = '') AS unscoped, "
            "(SELECT count(*) FROM graph.node WHERE kind = 'spec' AND left(group_id, length(:prefix)) <> :prefix) "
            "AS foreign_spec_nodes, "
            "(SELECT count(*) FROM graph.node WHERE kind = 'document' AND left(group_id, length(:prefix)) <> :prefix) "
            "AS foreign_documents, "
            "(SELECT count(*) FROM graph.node WHERE kind = 'fragment' AND left(group_id, length(:prefix)) <> :prefix) "
            "AS foreign_fragments, "
            "(SELECT count(*) FROM graph.edge AS e JOIN graph.node AS s ON s.id = e.source_id "
            "JOIN graph.node AS t ON t.id = e.target_id "
            "WHERE NOT (left(s.group_id, length(:prefix)) = :prefix AND left(t.group_id, length(:prefix)) = :prefix)) "
            "AS foreign_relationships",
            {"prefix": prefix},
        )
        return {key: int(value) for key, value in rows[0].items()}
