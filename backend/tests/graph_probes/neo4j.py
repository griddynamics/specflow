"""The Neo4j probe: every raw Cypher statement of the live graph tests lives in this file.

Statements are written against one database through the ``neo4j`` driver (the probe owns its own
driver; the code under test uses ``probe.store``). Labels, relationship types and property names are
inlined after ``identifier`` validation, values are always parameters.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Final

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
    identifier,
)
from graph.core import connect
from graph.core.queries import Query
from graph.stores.config import StoreConfig

BATCH: Final = 500
_UNIQUE_TYPES: Final = {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS", "NODE_KEY"}
_LIBRARY_EDGE_KEY_INDEX: Final = "graph_edge_key_{}"

# One end of an edge, without its vector properties (they are large and no test reads them there).
_END: Final = (
    "{id: elementId(%(v)s), labels: labels(%(v)s), "
    "props: [k IN keys(%(v)s) WHERE NOT k IN $vectors | [k, %(v)s[k]]]}"
)

OUTSIDE_TEST_PROJECTS: Final = (
    "CALL () { MATCH (n) WHERE n.group_id IS NULL RETURN count(n) AS unscoped } "
    "CALL () { MATCH (s:SpecNode) WHERE s.group_id IS NULL OR NOT s.group_id STARTS WITH $prefix "
    "RETURN count(s) AS foreign_spec_nodes } "
    "CALL () { MATCH (d:Document) WHERE d.group_id IS NULL OR NOT d.group_id STARTS WITH $prefix "
    "RETURN count(d) AS foreign_documents } "
    "CALL () { MATCH (f:Fragment) WHERE f.group_id IS NULL OR NOT f.group_id STARTS WITH $prefix "
    "RETURN count(f) AS foreign_fragments } "
    "CALL () { MATCH (a)-[r]->(b) "
    "WHERE NOT (coalesce(a.group_id, '') STARTS WITH $prefix AND coalesce(b.group_id, '') STARTS WITH $prefix) "
    "RETURN count(r) AS foreign_relationships } "
    "RETURN unscoped, foreign_spec_nodes, foreign_documents, foreign_fragments, foreign_relationships"
)


def _where(variable: str, props: Mapping[str, Any], prefix: str) -> tuple[list[str], dict[str, Any]]:
    """Equality conditions on ``variable`` for ``props`` and the parameters they bind."""

    conditions: list[str] = []
    params: dict[str, Any] = {}
    for index, (name, value) in enumerate(props.items()):
        key = f"{prefix}{index}"
        conditions.append(f"{variable}.{identifier(name)} = ${key}")
        params[key] = value
    return conditions, params


def _node(row: Mapping[str, Any]) -> ProbeNode:
    return ProbeNode(id=row["id"], labels=tuple(sorted(row["labels"])), props=dict(row["props"]))


def _end(row: Mapping[str, Any]) -> ProbeNode:
    return ProbeNode(id=row["id"], labels=tuple(sorted(row["labels"])), props={k: v for k, v in row["props"]})


class Neo4jProbe(GraphProbe):
    backend = "neo4j"

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self._driver = connect(config)

    @property
    def driver(self) -> Any:
        """The probe's own driver (Neo4j-only checks, e.g. the version gate, use it)."""

        return self._driver

    async def _close(self) -> None:
        await self._driver.close()

    async def run(self, text: str, /, **params: Any) -> list[dict[str, Any]]:
        result = await self._driver.execute_query(text, params, database_=self.database)
        return [dict(record) for record in result.records]

    # -- reads -------------------------------------------------------------------------------------

    async def nodes(self, project_id: str, label: str | None = None, **where: Any) -> list[ProbeNode]:
        conditions, params = _where("n", where, "w")
        scope = f":{identifier(label)}" if label else ""
        clause = "".join(f" AND {condition}" for condition in conditions)
        rows = await self.run(
            f"MATCH (n{scope}) WHERE n.group_id = $p{clause} "
            "RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props",
            p=project_id,
            **params,
        )
        return sorted((_node(row) for row in rows), key=lambda n: n.id)

    async def edges(self, project_id: str, type: str | None = None, **where: Any) -> list[ProbeEdge]:
        conditions, params = _where("r", where, "w")
        kind = f":{identifier(type)}" if type else ""
        clause = "".join(f" AND {condition}" for condition in conditions)
        rows = await self.run(
            f"MATCH (s)-[r{kind}]->(t) WHERE (s.group_id = $p OR t.group_id = $p OR r.group_id = $p){clause} "
            f"RETURN elementId(r) AS id, type(r) AS type, {_END % {'v': 's'}} AS s, {_END % {'v': 't'}} AS t, "
            "properties(r) AS props",
            p=project_id,
            vectors=list(VECTOR_PROPERTIES),
            **params,
        )
        return sorted(
            (ProbeEdge(row["id"], row["type"], _end(row["s"]), _end(row["t"]), dict(row["props"])) for row in rows),
            key=lambda e: e.id,
        )

    async def count_nodes(self, project_id: str) -> int:
        rows = await self.run("MATCH (n) WHERE n.group_id = $p RETURN count(n) AS n", p=project_id)
        return int(rows[0]["n"])

    # -- seeding and fault injection ---------------------------------------------------------------

    async def create_nodes(self, project_id: str, specs: Sequence[NodeSpec]) -> int:
        by_labels: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for spec in specs:
            by_labels[tuple(identifier(label) for label in spec.labels)].append({**spec.props, "group_id": project_id})
        created = 0
        for labels, rows in by_labels.items():
            text = ":".join(labels)
            for start in range(0, len(rows), BATCH):
                batch = rows[start : start + BATCH]
                result = await self.run(
                    f"UNWIND $rows AS row CREATE (n:{text}) SET n = row RETURN count(n) AS n", rows=batch
                )
                created += int(result[0]["n"])
        return created

    @staticmethod
    def _end_clause(variable: str, match: Match, key: str) -> tuple[str, str]:
        """The ``MATCH`` pattern and ``WHERE`` text of one edge end; the values are read from ``row``."""

        scope = f":{identifier(match.label)}" if match.label else ""
        conditions = [f"{variable}.group_id = $p"]
        conditions += [f"{variable}.{identifier(name)} = row.{key}_{index}" for index, name in enumerate(match.props)]
        return f"({variable}{scope})", " AND ".join(conditions)

    async def create_edges(self, project_id: str, specs: Sequence[EdgeSpec]) -> int:
        groups: dict[tuple[Any, ...], list[EdgeSpec]] = defaultdict(list)
        for spec in specs:
            shape = (
                identifier(spec.type),
                spec.source.label,
                tuple(spec.source.props),
                spec.target.label,
                tuple(spec.target.props),
            )
            groups[shape].append(spec)
        created = 0
        for (kind, _, _, _, _), members in groups.items():
            source_pattern, source_where = self._end_clause("s", members[0].source, "s")
            target_pattern, target_where = self._end_clause("t", members[0].target, "t")
            for start in range(0, len(members), BATCH):
                batch = members[start : start + BATCH]
                rows = []
                for spec in batch:
                    row: dict[str, Any] = {"props": dict(spec.props)}
                    row.update({f"s_{i}": v for i, v in enumerate(spec.source.props.values())})
                    row.update({f"t_{i}": v for i, v in enumerate(spec.target.props.values())})
                    rows.append(row)
                bad = await self.run(
                    "UNWIND $rows AS row "
                    f"CALL (row) {{ MATCH {source_pattern} WHERE {source_where} RETURN count(s) AS sources }} "
                    f"CALL (row) {{ MATCH {target_pattern} WHERE {target_where} RETURN count(t) AS targets }} "
                    "WITH row, sources, targets WHERE sources <> 1 OR targets <> 1 RETURN count(row) AS n",
                    p=project_id,
                    rows=rows,
                )
                if bad[0]["n"]:
                    raise ProbeError(f"{kind}: {bad[0]['n']} of {len(batch)} edges have an end that is not one node")
                result = await self.run(
                    f"UNWIND $rows AS row MATCH {source_pattern} WHERE {source_where} "
                    f"MATCH {target_pattern} WHERE {target_where} "
                    f"CREATE (s)-[r:{kind}]->(t) SET r = row.props RETURN count(r) AS n",
                    p=project_id,
                    rows=rows,
                )
                made = int(result[0]["n"])
                if made != len(batch):
                    raise ProbeError(f"{kind}: {len(batch)} edges asked for, {made} created (ends must match one node)")
                created += made
        return created

    async def update_nodes(
        self,
        project_id: str,
        match: Match,
        props: Mapping[str, Any] | None = None,
        *,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> int:
        conditions, params = _where("n", match.props, "m")
        scope = f":{identifier(match.label)}" if match.label else ""
        clause = "".join(f" AND {condition}" for condition in conditions)
        text = f"MATCH (n{scope}) WHERE n.group_id = $p{clause}"
        if props:
            text += " SET n += $props"
        for label in add_labels:
            text += f" SET n:{identifier(label)}"
        for label in remove_labels:
            text += f" REMOVE n:{identifier(label)}"
        rows = await self.run(text + " RETURN count(n) AS n", p=project_id, props=dict(props or {}), **params)
        return int(rows[0]["n"])

    async def delete_nodes(self, project_id: str, match: Match) -> int:
        conditions, params = _where("n", match.props, "m")
        scope = f":{identifier(match.label)}" if match.label else ""
        clause = "".join(f" AND {condition}" for condition in conditions)
        rows = await self.run(
            f"MATCH (n{scope}) WHERE n.group_id = $p{clause} WITH n, count(n) AS c DETACH DELETE n RETURN sum(c) AS n",
            p=project_id,
            **params,
        )
        return int(rows[0]["n"] or 0)

    async def update_edges(self, edge_ids: Sequence[str], props: Mapping[str, Any]) -> int:
        rows = await self.run(
            "MATCH ()-[r]->() WHERE elementId(r) IN $ids SET r += $props RETURN count(r) AS n",
            ids=list(edge_ids),
            props=dict(props),
        )
        return int(rows[0]["n"])

    async def delete_edges(self, edge_ids: Sequence[str]) -> int:
        rows = await self.run(
            "MATCH ()-[r]->() WHERE elementId(r) IN $ids WITH r, count(r) AS c DELETE r RETURN sum(c) AS n",
            ids=list(edge_ids),
        )
        return int(rows[0]["n"] or 0)

    def failing_write_query(self, name: str) -> Query:
        return Query.literal(
            name, "UNWIND $rows AS row WITH row WHERE $project_id IS NOT NULL RETURN 1 / 0 AS written, [] AS refs"
        )

    # -- schema ------------------------------------------------------------------------------------

    async def schema_objects(self) -> list[SchemaObject]:
        constraints = await self.run(
            "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
            "RETURN name, type, entityType, labelsOrTypes, properties"
        )
        indexes = await self.run(
            "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, options, state "
            "RETURN name, type, entityType, labelsOrTypes, properties, options, state"
        )
        objects: list[SchemaObject] = []
        for row in constraints + indexes:
            labels = list(row["labelsOrTypes"] or [])
            if len(labels) != 1:
                continue  # lookup indexes carry no label; the library's objects each have exactly one
            config = (row.get("options") or {}).get("indexConfig") or {}
            kind = "unique" if row["type"] in _UNIQUE_TYPES else str(row["type"]).lower()
            dimensions = config.get("vector.dimensions")
            similarity = config.get("vector.similarity_function")
            objects.append(
                SchemaObject(
                    name=row["name"],
                    kind=kind,
                    entity="node" if row["entityType"] == "NODE" else "relationship",
                    label=labels[0],
                    properties=tuple(row["properties"] or ()),
                    online=row.get("state", "ONLINE") == "ONLINE",
                    dimensions=int(dimensions) if dimensions is not None else None,
                    similarity=str(similarity).lower() if similarity is not None else None,
                )
            )
        return objects

    async def drop_edge_key_index(self, relation: str) -> str | None:
        name = _LIBRARY_EDGE_KEY_INDEX.format(identifier(relation).lower())
        found = [o for o in await self.schema_objects() if o.name == name]
        if not found:
            return None
        await self.run(f"DROP INDEX {name}")
        return name

    async def vector_index_dimension(self, name: str) -> int | None:
        rows = await self.run(
            "SHOW VECTOR INDEXES YIELD name, options WHERE name = $name "
            "RETURN options.indexConfig['vector.dimensions'] AS dimension",
            name=name,
        )
        return int(rows[0]["dimension"]) if rows else None

    async def await_indexes(self, timeout_s: float = 60.0) -> None:
        await self.run("CALL db.awaitIndexes($timeout)", timeout=int(timeout_s))

    async def server_edition(self) -> str:
        rows = await self.run("CALL dbms.components() YIELD name, edition RETURN name, edition")
        return str({row["name"]: row["edition"] for row in rows}["Neo4j Kernel"])

    # -- cleanup and isolation ---------------------------------------------------------------------

    async def delete_projects(self, project_ids: Sequence[str]) -> int:
        ids = list(project_ids)
        await self.run("MATCH (n) WHERE n.group_id IN $ids DETACH DELETE n", ids=ids)
        rows = await self.run("MATCH (n) WHERE n.group_id IN $ids RETURN count(n) AS n", ids=ids)
        return int(rows[0]["n"])

    async def outside_counts(self, prefix: str) -> dict[str, int]:
        rows = await self.run(OUTSIDE_TEST_PROJECTS, prefix=prefix)
        return {key: int(value) for key, value in rows[0].items()}

