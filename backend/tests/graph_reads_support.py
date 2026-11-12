"""An in-memory executor for the reads ``similar``, ``evidence``, ``paths`` and ``expand`` of ``GraphReader``.

``ReadsExecutor`` extends the trace fixture's executor (``graph_trace_support.TraceExecutor``) with the queries these
reads run, over a ``TraceGraph`` plus per-node attributes, provenance and stored vectors, and the fragments of the
documents. Like the trace executor it renders every query with the Cypher and the PostgreSQL dialects, so a template
either store lacks fails the test.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Sequence
from typing import Any, Final

from graph.core import GraphReader
from graph.core.hops import HopNode
from graph.core.queries import Query
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph_support import shipped_ontology
from graph_trace_support import SPEC_NODE, TraceExecutor, TraceGraph

PROJECT: Final = "offline-reads"
MODEL: Final = "test/embedding"


class Gateway:
    """Names the embedding model; the reads under test never embed."""

    def __init__(self, model: str = MODEL) -> None:
        self.config = type("Config", (), {"embedding_model": model})()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError(f"no embedding call ({len(texts)} texts)")


def cosine_score(first: Sequence[float], second: Sequence[float]) -> float:
    norm = math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(y * y for y in second))
    return (1 + sum(x * y for x, y in zip(first, second, strict=True)) / norm) / 2


class EdgeRead:
    """A ``HopRead`` over ``(source, target)`` pairs whose uuid is the ``spec_id``; ``roots`` are spine roots. Records
    each frontier it reads in ``calls``."""

    def __init__(self, edges: Iterable[tuple[str, str]], roots: Collection[str] = ()) -> None:
        self.edges = list(edges)
        self.roots = set(roots)
        self.calls: list[list[str]] = []

    def node(self, spec_id: str) -> HopNode:
        return HopNode(spec_id, spec_id, spec_id in self.roots)

    async def __call__(self, uuids: list[str]) -> list[tuple[str, HopNode]]:
        self.calls.append(sorted(uuids))
        found = [(first, second) for first, second in self.edges] + [(second, first) for first, second in self.edges]
        return [(start, self.node(other)) for start, other in found if start in uuids]


class ReadsExecutor(TraceExecutor):
    """The trace executor plus node attributes, provenance, vectors (``spec_id`` -> (model, vector)) and fragments."""

    def __init__(self, graph: TraceGraph) -> None:
        super().__init__(graph)
        self.attributes: dict[str, dict[str, Any]] = {}
        self.provenance: dict[str, list[dict[str, Any]]] = {}
        self.vectors: dict[str, tuple[str, tuple[float, ...]]] = {}
        self.fragments: list[dict[str, Any]] = []

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        assert binds_project_id(render_cypher(query))
        self._postgres.plan(query, {**params, "project_id": project_id})
        self.calls.append((query.name, params))
        return getattr(self, f"_{query.name}")(**params)

    def reader(self, gateway: Any = None, **options: Any) -> GraphReader:
        return GraphReader(self, shipped_ontology(), gateway, **options)

    # nodes

    def _row(self, spec_id: str) -> dict[str, Any]:
        node = self.graph.nodes[spec_id]
        props = {
            "spec_id": spec_id,
            "uuid": node["uuid"],
            "name": node["name"],
            "spec_id_aliases": node["aliases"],
            **self.attributes.get(spec_id, {}),
        }
        retired = {item["fragment_id"] for item in self.fragments if item.get("status") == "retired"}
        provenance = [item for item in self.provenance.get(spec_id, []) if item["fragment_id"] not in retired]
        return {"labels": [SPEC_NODE, node["label"]], "props": props, "provenance": provenance}  # as the store reads

    def _read_node_by_spec_id(self, spec_id: str, **_: Any) -> list[dict[str, Any]]:
        return [self._row(spec_id)] if spec_id in self.graph.nodes else []

    def _read_node_by_alias(self, spec_id: str, **_: Any) -> list[dict[str, Any]]:
        return [self._row(key) for key, node in sorted(self.graph.nodes.items()) if spec_id in node["aliases"]]

    def _read_nodes_by_uuid(self, uuids: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        return [self._row(key) for key, node in sorted(self.graph.nodes.items()) if node["uuid"] in uuids]

    def _read_nodes(self, labels: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        return [self._row(key) for key, node in sorted(self.graph.nodes.items()) if node["label"] in labels]

    def _read_owner_values(self, **_: Any) -> list[dict[str, Any]]:
        return []

    # edges

    def _edge_row(self, edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "relation": edge["relation"],
            "source": edge["source"],
            "target": edge["target"],
            "props": edge["props"],
        }

    def _read_edges(self, relations: Sequence[str], *, include_inferred: bool, **_: Any) -> list[dict[str, Any]]:
        return [
            self._edge_row(edge)
            for edge in self.graph.edges
            if edge["relation"] in relations and (include_inferred or edge["props"].get("basis") != "inferred")
        ]

    def _read_neighbours(self, spec_id: str, relations: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        return [
            self._edge_row(edge)
            for edge in self.graph.edges
            if edge["relation"] in relations and spec_id in (edge["source"], edge["target"])
        ]

    # vectors

    def _model_vectors(self, model: str) -> list[tuple[str, tuple[float, ...]]]:
        return [(key, vector) for key, (made_by, vector) in sorted(self.vectors.items()) if made_by == model]

    def _count_search_vectors(self, model: str, **_: Any) -> list[dict[str, Any]]:
        return [{"matching": len(self._model_vectors(model)), "stored": len(self.vectors)}]

    def _search_nodes_vector_exact(
        self, vector: Sequence[float], model: str, limit: int, **_: Any
    ) -> list[dict[str, Any]]:
        hits = [
            {"uuid": self.graph.nodes[key]["uuid"], "spec_id": key, "score": cosine_score(vector, stored)}
            for key, stored in self._model_vectors(model)
        ]
        return sorted(hits, key=lambda hit: (-hit["score"], hit["spec_id"]))[:limit]

    def _read_node_vectors(self, uuids: Sequence[str], model: str) -> list[dict[str, Any]]:
        return [
            {"uuid": self.graph.nodes[key]["uuid"], "spec_id": key, "embedding": list(vector)}
            for key, vector in self._model_vectors(model)
            if self.graph.nodes[key]["uuid"] in uuids
        ]

    # lines

    def _read_fragments(self, document_id: str | None) -> list[dict[str, Any]]:
        return [{"props": dict(item)} for item in self.fragments if document_id in (None, item["document_id"])]

    def _read_nodes_for_lines(
        self, document_id: str, line_start: int, line_end: int, labels: Sequence[str], retired: str
    ) -> list[dict[str, Any]]:
        fragments = {item["fragment_id"]: item for item in self.fragments if item.get("status") != retired}

        def overlaps(item: dict[str, Any]) -> bool:
            own = fragments.get(item["fragment_id"])
            if own is None or item.get("document_id") != document_id:
                return False
            start = item["line_start"] if item["line_start"] is not None else own["line_start"]
            end = item["line_end"] if item["line_end"] is not None else own["line_end"]
            return bool(start <= line_end and end >= line_start)

        return [
            self._row(key)
            for key, node in sorted(self.graph.nodes.items())
            if node["label"] in labels and any(overlaps(item) for item in self.provenance.get(key, []))
        ]
