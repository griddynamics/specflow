"""An in-memory ``GraphProbe``: the reference behaviour of the probe contract, for offline tests.

It holds nodes and edges in dictionaries and follows the contract written in ``graph_probes.base``
(project scoping, ``None`` removes a property, sorted labels, ids that are opaque strings, an edge whose
ends must each select one node). The probe contract test runs against it offline and against the live
probe of the selected store; the derived probe logic (canonical snapshot, state, spine count) is tested
on it. It has no ``GraphStore``: ``store`` is not available.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import SecretStr

from graph.core.queries import Query
from graph.stores.config import StoreConfig
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


class MemoryProbe(GraphProbe):
    backend = "memory"

    async def before_session_schema(self) -> None:
        """Nothing to note: the schema setup creates no database the session must remove."""

    def __init__(self) -> None:
        super().__init__(StoreConfig(backend="neo4j", uri="memory://", user="memory", password=SecretStr("")))
        self._ids = itertools.count(1)
        self._nodes: dict[str, tuple[set[str], dict[str, Any]]] = {}
        self._edges: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        self.schema: list[SchemaObject] = []
        self.vector_dimensions: dict[str, int] = {}

    @property
    def store(self) -> Any:
        raise NotImplementedError("the memory probe has no graph store")

    async def _close(self) -> None:
        return None

    def _node(self, node_id: str, *, vectors: bool = True) -> ProbeNode:
        labels, props = self._nodes[node_id]
        kept = {k: copy.deepcopy(v) for k, v in props.items() if vectors or k not in VECTOR_PROPERTIES}
        return ProbeNode(node_id, tuple(sorted(labels)), kept)

    @staticmethod
    def _matches(props: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
        return all(name in props and props[name] == value for name, value in where.items())

    def _select(self, project_id: str, match: Match) -> list[str]:
        return [
            node_id
            for node_id, (labels, props) in self._nodes.items()
            if props.get("group_id") == project_id
            and (match.label is None or match.label in labels)
            and self._matches(props, match.props)
        ]

    # -- reads -------------------------------------------------------------------------------------

    async def nodes(self, project_id: str, label: str | None = None, **where: Any) -> list[ProbeNode]:
        return sorted((self._node(i) for i in self._select(project_id, Match(label, where))), key=lambda n: n.id)

    async def edges(self, project_id: str, relation: str | None = None, **where: Any) -> list[ProbeEdge]:
        found = []
        for edge_id, (kind, source, target, props) in self._edges.items():
            touches = project_id in (
                props.get("group_id"),
                self._nodes[source][1].get("group_id"),
                self._nodes[target][1].get("group_id"),
            )
            if touches and (relation is None or kind == relation) and self._matches(props, where):
                found.append(
                    ProbeEdge(
                        edge_id,
                        kind,
                        self._node(source, vectors=False),
                        self._node(target, vectors=False),
                        copy.deepcopy(props),
                    )
                )
        return sorted(found, key=lambda e: e.id)

    async def count_nodes(self, project_id: str) -> int:
        return len(self._select(project_id, Match()))

    # -- writes ------------------------------------------------------------------------------------

    async def create_nodes(self, project_id: str, specs: Sequence[NodeSpec]) -> int:
        for spec in specs:
            check_flat(spec.props)
            props = {k: copy.deepcopy(v) for k, v in spec.props.items() if v is not None}
            props["group_id"] = project_id
            self._nodes[f"n{next(self._ids)}"] = (set(spec.labels), props)
        return len(specs)

    async def create_edges(self, project_id: str, specs: Sequence[EdgeSpec]) -> int:
        for spec in specs:
            check_flat(spec.props)
            ends = [self._select(project_id, spec.source), self._select(project_id, spec.target)]
            if len(ends[0]) != 1 or len(ends[1]) != 1:
                raise ProbeError(f"{spec.type}: the ends must each match one node")
            props = {k: copy.deepcopy(v) for k, v in spec.props.items() if v is not None}
            self._edges[f"e{next(self._ids)}"] = (spec.type, ends[0][0], ends[1][0], props)
        return len(specs)

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
        selected = self._select(project_id, match)
        for node_id in selected:
            labels, stored = self._nodes[node_id]
            for name, value in (props or {}).items():
                if value is None:
                    stored.pop(name, None)
                else:
                    stored[name] = copy.deepcopy(value)
            labels.update(add_labels)
            labels.difference_update(remove_labels)
        return len(selected)

    async def delete_nodes(self, project_id: str, match: Match) -> int:
        selected = self._select(project_id, match)
        for node_id in selected:
            del self._nodes[node_id]
            for edge_id in [e for e, (_, s, t, _) in self._edges.items() if node_id in (s, t)]:
                del self._edges[edge_id]
        return len(selected)

    async def update_edges(self, edge_ids: Sequence[str], props: Mapping[str, Any]) -> int:
        check_flat(props)
        found = [i for i in edge_ids if i in self._edges]
        for edge_id in found:
            stored = self._edges[edge_id][3]
            for name, value in props.items():
                if value is None:
                    stored.pop(name, None)
                else:
                    stored[name] = copy.deepcopy(value)
        return len(found)

    async def delete_edges(self, edge_ids: Sequence[str]) -> int:
        found = [i for i in edge_ids if i in self._edges]
        for edge_id in found:
            del self._edges[edge_id]
        return len(found)

    def failing_write_query(self, name: str) -> Query:
        return Query.literal(name, "-- fails when run")

    # -- schema ------------------------------------------------------------------------------------

    async def schema_objects(self) -> list[SchemaObject]:
        return list(self.schema)

    async def drop_edge_key_index(self, relation: str) -> str | None:
        name = f"graph_edge_key_{relation.lower()}"
        found = [o for o in self.schema if o.name == name]
        self.schema = [o for o in self.schema if o.name != name]
        return name if found else None

    async def vector_index_dimension(self, name: str) -> int | None:
        return self.vector_dimensions.get(name)

    async def await_indexes(self, timeout_s: float = 60.0) -> None:
        assert timeout_s > 0  # nothing to wait for: every write is visible at once

    async def server_edition(self) -> str:
        return "memory"

    # -- cleanup and isolation ---------------------------------------------------------------------

    async def delete_projects(self, project_ids: Sequence[str]) -> int:
        ids = set(project_ids)
        for node_id in [i for i, (_, p) in self._nodes.items() if p.get("group_id") in ids]:
            del self._nodes[node_id]
            for edge_id in [e for e, (_, s, t, _) in self._edges.items() if node_id in (s, t)]:
                del self._edges[edge_id]
        return len([1 for _, p in self._nodes.values() if p.get("group_id") in ids])

    async def outside_counts(self, prefix: str) -> dict[str, int]:
        def foreign(group: Any) -> bool:
            return group is None or not str(group).startswith(prefix)

        counts = {
            "unscoped": 0,
            "foreign_spec_nodes": 0,
            "foreign_documents": 0,
            "foreign_fragments": 0,
            "foreign_relationships": 0,
        }
        for labels, props in self._nodes.values():
            group = props.get("group_id")
            counts["unscoped"] += group is None
            for label, key in (
                ("SpecNode", "foreign_spec_nodes"),
                ("Document", "foreign_documents"),
                ("Fragment", "foreign_fragments"),
            ):
                counts[key] += label in labels and foreign(group)
        for _, source, target, _ in self._edges.values():
            ends = (self._nodes[source][1].get("group_id"), self._nodes[target][1].get("group_id"))
            counts["foreign_relationships"] += any(foreign(group) for group in ends)
        return counts
