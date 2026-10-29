"""Spine reachability (ontology.md "Spine and inference"): which spine sources reach a spine root.

One implementation for inference (orphan subjects and ``spine_unreachable_before``) and verification
(the ``spine_unreachable`` metric before and after inference). ``SpineGraph`` holds the spec nodes
with exactly one build-owned label and the spine-relation edges between them; ``unreached`` walks the
ontology's spine paths in memory: ``{via, min, max}`` takes ``min``..``max`` edges of any of its
relations, ``{sequence}`` one edge of each in order. A symmetric relation counts in either direction
(it is stored in canonical order). ``bases`` limits the edges walked (``stated``/``implied`` for the
graph as inference reads it; None for every edge). Nodes with a label problem are left out.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from graph.build.queries import READ_SPINE_EDGES, READ_SPINE_NODES
from graph.core.db import Executor
from graph.core.ontology import Ontology, Spine

BUILD_OWNER = "build"


@dataclass(frozen=True, slots=True)
class SpineNode:
    """A node: its key (uuid, or the verification read's element id), ``spec_id`` and label."""

    key: str
    spec_id: str
    label: str


@dataclass(frozen=True, slots=True)
class SpineEdge:
    relation: str
    source: str
    target: str
    basis: str | None = None


@dataclass(frozen=True)
class SpineGraph:
    """Spec nodes and the spine-relation edges between them."""

    nodes: tuple[SpineNode, ...]
    edges: tuple[SpineEdge, ...]
    _by_key: dict[str, SpineNode] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_key", {node.key: node for node in self.nodes})

    def _forward_edges(self, ontology: Ontology, bases: Collection[str] | None) -> dict[str, dict[str, set[str]]]:
        """The edges to walk, by relation and source key (a symmetric relation both ways)."""

        relations = ontology.spine_relations
        forward: dict[str, dict[str, set[str]]] = {name: {} for name in relations}
        for edge in self.edges:
            if edge.relation not in relations or edge.source not in self._by_key or edge.target not in self._by_key:
                continue
            if bases is not None and (edge.basis or "stated") not in bases:
                continue
            forward[edge.relation].setdefault(edge.source, set()).add(edge.target)
            if ontology.is_symmetric(edge.relation):
                forward[edge.relation].setdefault(edge.target, set()).add(edge.source)
        return forward

    def _reaches(self, start: str, spine: Spine, forward: Mapping[str, Mapping[str, set[str]]]) -> bool:
        """Whether some spine path leads from ``start`` to a node with a spine-root label."""

        def step(frontier: set[str], names: Iterable[str]) -> set[str]:
            return {nxt for name in names for key in frontier for nxt in forward[name].get(key, ())}

        def at_root(frontier: set[str]) -> bool:
            return any(self._by_key[key].label in spine.roots for key in frontier)

        for path in spine.paths:
            frontier = {start}
            if path.sequence:
                for name in path.relations:
                    frontier = step(frontier, (name,))
                if at_root(frontier):
                    return True
                continue
            for hop in range(1, path.max_hops + 1):
                frontier = step(frontier, path.relations)
                if not frontier:
                    break
                if hop >= path.min_hops and at_root(frontier):
                    return True
        return False

    def unreached(
        self, ontology: Ontology, sources: Collection[str], bases: Collection[str] | None = None
    ) -> list[SpineNode]:
        """Nodes with a label in ``sources`` that reach no spine root by any spine path over edges
        whose ``basis`` is in ``bases`` (None: every edge; an edge without ``basis`` reads as ``stated``),
        in ``spec_id`` order."""

        candidates = [node for node in self.nodes if node.label in sources]
        if not candidates:
            return []
        forward = self._forward_edges(ontology, bases)
        spine = ontology.spine
        missed = [node for node in candidates if not self._reaches(node.key, spine, forward)]
        return sorted(missed, key=lambda node: (node.spec_id, node.key))


async def load_spine_graph(executor: Executor, project_id: str, ontology: Ontology) -> SpineGraph:
    """The project's spec nodes (with a ``uuid``, a ``spec_id`` and exactly one build-owned label),
    keyed by uuid, and the spine-relation edges between them."""

    labels = sorted(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER)
    relations = sorted(ontology.spine_relations)
    nodes = tuple(
        SpineNode(key=row["uuid"], spec_id=row["spec_id"], label=row["labels"][0])
        for row in await executor.read(READ_SPINE_NODES, project_id, labels=labels)
        if isinstance(row["uuid"], str) and isinstance(row["spec_id"], str) and len(row["labels"]) == 1
    )
    edges = tuple(
        SpineEdge(relation=row["relation"], source=row["source"], target=row["target"], basis=row["basis"])
        for row in await executor.read(READ_SPINE_EDGES, project_id, relations=relations)
    )
    return SpineGraph(nodes, edges)
