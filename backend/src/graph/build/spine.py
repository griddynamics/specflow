"""Spine reachability: which spine sources reach a spine root."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from graph.build.queries import READ_SPINE_EDGES, READ_SPINE_NODES
from graph.core.db import Executor
from graph.core.ontology import Condition, NodeView, Ontology, Spine

BUILD_OWNER = "build"
ATTRIBUTE_PAIR_LEN = 2
"""Items of one ``[name, value]`` pair of a read's ``attributes`` column."""


@dataclass(frozen=True, slots=True)
class SpineNode:
    """A node: its key (uuid, or the verification read's element id), ``spec_id``, label and the
    stored values of the attributes the ontology's conditions name (``condition_attributes``)."""

    key: str
    spec_id: str
    label: str
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True, slots=True)
class SpineEdge:
    """An edge: relation, source and target keys, ``basis`` (None reads as ``stated``) and, for an
    ``inferred`` edge, the ``inference_rule`` that wrote it (``rule``; ordered rules with ``reads``)."""

    relation: str
    source: str
    target: str
    basis: str | None = None
    rule: str | None = None


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
        """Nodes with a label in ``sources`` that reach no spine root by any spine path over edges whose ``basis`` is in
        ``bases`` (None: every edge; an edge without ``basis`` reads as ``stated``), in ``spec_id`` order; sources the
        spine ``exclude`` matches (``excluded``) are left out.
        """

        candidates = [node for node in self.nodes if node.label in sources]
        if candidates and ontology.spine_exclude():
            skipped = self.excluded(ontology, candidates, bases)
            candidates = [node for node in candidates if node.key not in skipped]
        if not candidates:
            return []
        forward = self._forward_edges(ontology, bases)
        spine = ontology.spine
        missed = [node for node in candidates if not self._reaches(node.key, spine, forward)]
        return sorted(missed, key=lambda node: (node.spec_id, node.key))

    def excluded(
        self, ontology: Ontology, nodes: Iterable[SpineNode], bases: Collection[str] | None = None
    ) -> frozenset[str]:
        """Keys of ``nodes`` the spine ``exclude`` matches, themselves or through an owner: a target of
        theirs on an owner pair, over edges whose ``basis`` is in ``bases``. Empty without ``exclude``."""

        condition = ontology.spine_exclude()
        if not condition:
            return frozenset()
        owners: dict[str, set[str]] = {}
        for edge in self.edges:
            source, target = self._by_key.get(edge.source), self._by_key.get(edge.target)
            if source is None or target is None:
                continue
            if bases is not None and (edge.basis or "stated") not in bases:
                continue
            pair = ontology.pair_rule(edge.relation, source.label, target.label)
            if pair is not None and pair.owner:
                owners.setdefault(source.key, set()).add(target.key)
        return frozenset(
            node.key
            for node in nodes
            if _matches(ontology, condition, node)
            or any(_matches(ontology, condition, self._by_key[key]) for key in owners.get(node.key, ()))
        )


def _matches(ontology: Ontology, condition: Condition, node: SpineNode) -> bool:
    return ontology.condition_matches(condition, NodeView(node.label, node.attributes))


def condition_attributes(ontology: Ontology) -> tuple[str, ...]:
    """The attribute names the ontology's node conditions read, sorted: the spine ``exclude``, the
    pairs' ``source_where``, ``target_where`` and ``not_across``, and the inference ``exclude_where`` and
    ``target_exclude_where``."""

    names = set(ontology.spine_exclude())
    for relation in ontology.relations.values():
        for pair in relation.pairs:
            names.update(pair.source_where, pair.target_where, pair.not_across)
    for rule in ontology.inference.rules:
        names.update(rule.exclude_where, rule.target_exclude_where)
    return tuple(sorted(names))


def owner_relations(ontology: Ontology) -> frozenset[str]:
    """The relations of the ontology's owner pairs."""

    return frozenset(pair.relation for relation in ontology.relations.values() for pair in relation.pairs if pair.owner)


def realization_relations(ontology: Ontology) -> frozenset[str]:
    """The relations ``realization`` rules walk: their ``member_relation`` and container path relations
    (``graph.build.infer_reads``)."""

    return frozenset(
        name
        for rule in ontology.inference.rules
        for name in (
            *((rule.member_relation,) if rule.member_relation else ()),
            *(relation for path in rule.container_paths for relation in path.relations),
        )
    )


def attribute_values(value: Any) -> dict[str, Any]:
    """A read's ``attributes`` column (``[name, value]`` pairs) as a mapping; anything else as empty."""

    if not isinstance(value, list | tuple):
        return {}
    return {item[0]: item[1] for item in value if isinstance(item, list | tuple) and len(item) == ATTRIBUTE_PAIR_LEN}


async def load_spine_graph(executor: Executor, project_id: str, ontology: Ontology) -> SpineGraph:
    """The project's spec nodes (with a ``uuid``, a ``spec_id`` and exactly one build-owned label), keyed by uuid, with
    the attributes the ontology's conditions name, and the edges of the spine, owner and ``realization`` relations
    between them, each with its ``basis`` and ``inference_rule``.
    """

    labels = sorted(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER)
    relations = sorted(ontology.spine_relations | owner_relations(ontology) | realization_relations(ontology))
    attributes = list(condition_attributes(ontology))
    nodes = tuple(
        SpineNode(
            key=row["uuid"],
            spec_id=row["spec_id"],
            label=row["labels"][0],
            attributes=attribute_values(row["attributes"]),
        )
        for row in await executor.read(READ_SPINE_NODES, project_id, labels=labels, attributes=attributes)
        if isinstance(row["uuid"], str) and isinstance(row["spec_id"], str) and len(row["labels"]) == 1
    )
    edges = tuple(
        SpineEdge(
            relation=row["relation"],
            source=row["source"],
            target=row["target"],
            basis=row["basis"],
            rule=row.get("inference_rule"),
        )
        for row in await executor.read(READ_SPINE_EDGES, project_id, relations=relations)
    )
    return SpineGraph(nodes, edges)
