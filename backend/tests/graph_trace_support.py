"""Trace test helpers: the shop fixture as plain nodes and edges, an in-memory executor answering the
reader's trace queries over it, and a writer of the same graph to a live store.

The in-memory executor implements the contract of the three queries a trace reads
(``read_node_uuids_by_spec_id``, ``read_nodes_by_uuid``, ``read_trace_hop``) over Python data, so the
walk is tested offline; the live suite runs the same scenarios against every store the switch selects.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from graph.core import NodeRef, Ontology, OwnedWriter, load_ontology
from graph.core.queries import Query
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph.stores.postgres.dialect import PostgresDialect
from graph_support import SHIPPED_PROMPTS

SHIPPED_FILE: Final = Path(str(SHIPPED_PROMPTS)).parent / "spec-ontology.yaml"
SHOP_FIXTURE: Final = Path(__file__).resolve().parent / "fixtures" / "graph" / "plan" / "shop.yaml"
SPEC_NODE: Final = "SpecNode"
TRACE_QUERIES: Final = frozenset({"read_node_uuids_by_spec_id", "read_nodes_by_uuid", "read_trace_hop"})


@dataclass
class TraceGraph:
    """Spec nodes (``spec_id`` -> label, uuid, name, aliases) and domain edges of one test graph."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)

    def node(self, spec_id: str, label: str, *, name: str | None = None, aliases: Sequence[str] = ()) -> TraceGraph:
        uuid = f"node-{spec_id.replace('::', '-').lower()}"
        self.nodes[spec_id] = {"label": label, "uuid": uuid, "name": name or spec_id, "aliases": list(aliases)}
        return self

    def edge(self, relation: str, source: str, target: str, **props: Any) -> TraceGraph:
        self.edges.append({"relation": relation, "source": source, "target": target, "props": props})
        return self

    def alias(self, spec_id: str, *aliases: str) -> TraceGraph:
        self.nodes[spec_id]["aliases"].extend(aliases)
        return self

    def by_uuid(self) -> dict[str, tuple[str, dict[str, Any]]]:
        return {node["uuid"]: (spec_id, node) for spec_id, node in self.nodes.items()}


def shop_graph() -> TraceGraph:
    """The shop fixture (``fixtures/graph/plan/shop.yaml``): its spec nodes and edges, copied."""

    data = yaml.safe_load(SHOP_FIXTURE.read_text(encoding="utf-8"))
    graph = TraceGraph()
    for node in data["nodes"]:
        graph.nodes[node["spec_id"]] = {
            "label": node["label"],
            "uuid": node["uuid"],
            "name": node["name"],
            "aliases": [],
        }
    for edge in data["edges"]:
        props = {key: edge[key] for key in ("basis", "confidence", "inference_rule") if key in edge}
        graph.edge(edge["relation"], edge["source"], edge["target"], **props)
    return copy.deepcopy(graph)


def without_impact(data: Any) -> Any:
    """A copy of parsed ontology ``data`` with every ``impact`` key removed."""

    if isinstance(data, dict):
        return {key: without_impact(value) for key, value in data.items() if key != "impact"}
    if isinstance(data, list):
        return [without_impact(item) for item in data]
    return data


def load_edited(directory: Path, data: Mapping[str, Any]) -> Ontology:
    """Load parsed ontology ``data`` (edited from the shipped file) with the shipped judge prompts beside it."""

    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "judge_prompts").exists():
        shutil.copytree(SHIPPED_PROMPTS, directory / "judge_prompts")
    path = directory / "spec-ontology.yaml"
    path.write_text(yaml.safe_dump(dict(data), sort_keys=False), encoding="utf-8")
    return load_ontology(path, submodule_files=[])


def shipped_data() -> dict[str, Any]:
    """The shipped ontology file, parsed."""

    return yaml.safe_load(SHIPPED_FILE.read_text(encoding="utf-8"))


class TraceExecutor:
    """An ``Executor`` answering the trace queries over a ``TraceGraph``; records every call.

    Each query is also rendered by the Cypher and the PostgreSQL dialects, so a template either store lacks,
    or a statement that does not bind the project id, fails the test.
    """

    def __init__(self, graph: TraceGraph) -> None:
        self.graph = graph
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._postgres = PostgresDialect("graph")

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        assert binds_project_id(render_cypher(query))
        self._postgres.plan(query, {**params, "project_id": project_id})
        assert query.name in TRACE_QUERIES, f"unexpected query {query.name}"
        self.calls.append((query.name, params))
        return getattr(self, f"_{query.name}")(**params)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        del project_id, params
        raise AssertionError(f"a trace never writes ({query.name})")

    def tx(self, project_id: str) -> Any:
        del project_id
        raise AssertionError("a trace never opens a transaction")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _read_node_uuids_by_spec_id(self, spec_ids: Sequence[str], labels: Sequence[str]) -> list[dict[str, Any]]:
        return [
            {"uuid": node["uuid"], "spec_id": spec_id}
            for spec_id, node in sorted(self.graph.nodes.items())
            if node["label"] in labels and (spec_id in spec_ids or set(node["aliases"]) & set(spec_ids))
        ]

    def _read_nodes_by_uuid(self, uuids: Sequence[str], retired: str) -> list[dict[str, Any]]:
        del retired
        found = self.graph.by_uuid()
        return [
            {
                "labels": [SPEC_NODE, node["label"]],
                "props": {"spec_id": spec_id, "uuid": uuid, "name": node["name"], "spec_id_aliases": node["aliases"]},
                "provenance": [],
            }
            for uuid in sorted(uuids, key=lambda item: found[item][0])
            if uuid in found
            for spec_id, node in (found[uuid],)
        ]

    def _read_trace_hop(
        self, frontier: Sequence[str], relations: Sequence[str], labels: Sequence[str]
    ) -> list[dict[str, Any]]:
        rows = []
        for edge in self.graph.edges:
            if edge["relation"] not in relations:
                continue
            ends = (self.graph.nodes[edge["source"]], self.graph.nodes[edge["target"]])
            for start, other, outgoing in ((ends[0], ends[1], True), (ends[1], ends[0], False)):
                if start["uuid"] in frontier and other["label"] in labels:
                    rows.append(self._hop_row(start, other, edge, outgoing=outgoing))
        return sorted(rows, key=lambda row: (row["frontier"], row["spec_id"], row["relation"]))

    def _hop_row(
        self, start: Mapping[str, Any], other: Mapping[str, Any], edge: Mapping[str, Any], *, outgoing: bool
    ) -> dict[str, Any]:
        props = edge["props"]
        spec_id = next(key for key, node in self.graph.nodes.items() if node is other)
        return {
            "frontier": start["uuid"],
            "uuid": other["uuid"],
            "spec_id": spec_id,
            "labels": [SPEC_NODE, other["label"]],
            "name": other["name"],
            "relation": edge["relation"],
            "outgoing": outgoing,
            "basis": props.get("basis"),
            "confidence": props.get("confidence"),
            "inference_rule": props.get("inference_rule"),
        }


async def write_trace_graph(writer: OwnedWriter, project_id: str, graph: TraceGraph) -> None:
    """Write ``graph`` to a live project through the build owner's writer (uuids made unique per project)."""

    for spec_id, node in sorted(graph.nodes.items()):
        properties = {"spec_id": spec_id, "name": node["name"]}
        if node["aliases"]:
            properties["spec_id_aliases"] = list(node["aliases"])
        await writer.upsert_node(node["label"], f"{project_id}-{node['uuid']}", properties)
    for index, edge in enumerate(graph.edges):
        source, target = graph.nodes[edge["source"]], graph.nodes[edge["target"]]
        await writer.upsert_relationship(
            edge["relation"],
            NodeRef(source["label"], f"{project_id}-{source['uuid']}"),
            NodeRef(target["label"], f"{project_id}-{target['uuid']}"),
            f"trace-{index}",
            dict(edge["props"]),
        )
