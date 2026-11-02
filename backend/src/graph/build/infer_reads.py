"""Ordered rules with ``reads``: the ``realization`` walk in code."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from graph.build.spine import SpineEdge, SpineGraph
from graph.core.ontology import InferenceRule, Ontology, SpinePath

_Step = dict[str, dict[str, str | None]]
"""Per relation: source key to its targets, each with the id of the overlay edge it takes (None: a read edge)."""
_State = tuple[str, frozenset[str]]
"""A walk's position: the node reached and the overlay edges taken on the way."""


def edge_id(edge: SpineEdge) -> str:
    """How an overlay edge is named in a candidate's ``reads`` (and in the judgement input)."""

    return f"{edge.source}|{edge.relation}|{edge.target}"


def edge_parts(found: str) -> tuple[str, str, str]:
    """The source key, relation and target key of an ``edge_id``."""

    source, relation, target = found.split("|")
    return source, relation, target


@dataclass(frozen=True)
class Container:
    """A candidate of one subject: the members it is reached from (``via``) and the overlay edges
    behind it (``reads``, ``edge_id``s; none when a walk without overlay edges reaches it), both sorted."""

    via: tuple[str, ...]
    reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class RealizationRow:
    """One subject with every member and the containers its members reach (by uuid)."""

    uuid: str
    spec_id: str
    members: tuple[str, ...]
    containers: Mapping[str, Container] = field(default_factory=dict)


def _steps(
    ontology: Ontology,
    graph: SpineGraph,
    relations: Collection[str],
    bases: Collection[str],
    overlay: Iterable[SpineEdge],
) -> dict[str, _Step]:
    """The edges a walk may take, by relation and source key: ``graph``'s edges of ``bases`` (an edge
    without ``basis`` reads as ``stated``) and the ``overlay``; a symmetric relation both ways. An end
    linked by a read edge and an overlay edge takes the read edge."""

    steps: dict[str, _Step] = {name: {} for name in relations}

    def add(edge: SpineEdge, taken: str | None) -> None:
        if edge.relation not in steps:
            return
        ends = [(edge.source, edge.target)]
        if ontology.is_symmetric(edge.relation):
            ends.append((edge.target, edge.source))
        for source, target in ends:
            targets = steps[edge.relation].setdefault(source, {})
            if taken is None or targets.get(target, taken) is not None:
                targets[target] = taken

    for edge in graph.edges:
        if (edge.basis or "stated") in bases:
            add(edge, None)
    for edge in overlay:
        add(edge, edge_id(edge))
    return steps


def _advance(states: set[_State], names: Iterable[str], steps: Mapping[str, _Step]) -> set[_State]:
    found: set[_State] = set()
    for name in names:
        for node, taken in states:
            for target, overlay in steps[name].get(node, {}).items():
                found.add((target, taken | {overlay} if overlay is not None else taken))
    return found


def _reached(start: _State, path: SpinePath, steps: Mapping[str, _Step]) -> set[_State]:
    """Where ``path`` leads from ``start``, each end with the overlay edges taken."""

    states = {start}
    if path.sequence:
        for name in path.relations:
            states = _advance(states, (name,), steps)
        return states
    found: set[_State] = set()
    for hop in range(1, path.max_hops + 1):
        states = _advance(states, path.relations, steps)
        if not states:
            break
        if hop >= path.min_hops:
            found |= states
    return found


def realization_rows(
    ontology: Ontology,
    rule: InferenceRule,
    graph: SpineGraph,
    *,
    bases: Collection[str],
    overlay: Iterable[SpineEdge] = (),
) -> list[RealizationRow]:
    """Each subject of ``rule`` (template ``realization``) with every member and the containers any
    container path reaches from them (module doc), in ``spec_id`` order; subjects without a container
    are left out."""

    member = rule.member_relation or ""
    relations = {member, *(name for path in rule.container_paths for name in path.relations)}
    steps = _steps(ontology, graph, relations, bases, overlay)
    members_of: dict[str, dict[str, str | None]] = {}
    for source, targets in steps[member].items():
        for target, taken in targets.items():
            members_of.setdefault(target, {})[source] = taken
    labels = {node.key: node.label for node in graph.nodes}
    rows: list[RealizationRow] = []
    for node in sorted(graph.nodes, key=lambda found: (found.spec_id, found.key)):
        if node.label not in rule.subject or not members_of.get(node.key):
            continue
        containers = _containers(node.key, members_of[node.key], rule, steps, labels)
        if containers:
            rows.append(RealizationRow(node.key, node.spec_id, tuple(sorted(members_of[node.key])), containers))
    return rows


def _containers(
    subject: str,
    members: Mapping[str, str | None],
    rule: InferenceRule,
    steps: Mapping[str, _Step],
    labels: Mapping[str, str],
) -> dict[str, Container]:
    """The containers ``rule``'s container paths reach from ``members`` (each with the overlay edge
    it takes to ``subject``, None for a read edge), by uuid: the members each is reached from and the
    overlay edges behind it, none when a walk over read edges alone reaches it."""

    via: dict[str, set[str]] = {}
    reads: dict[str, set[str]] = {}
    free: set[str] = set()  # reached by a walk over read edges only
    for uuid, taken in members.items():
        start: _State = (uuid, frozenset({taken}) if taken is not None else frozenset())
        for path in rule.container_paths:
            for target, behind in _reached(start, path, steps):
                if target == subject or labels.get(target) not in rule.targets:
                    continue
                via.setdefault(target, set()).add(uuid)
                reads.setdefault(target, set()).update(behind)
                if not behind:
                    free.add(target)
    return {
        uuid: Container(tuple(sorted(via[uuid])), () if uuid in free else tuple(sorted(reads[uuid])))
        for uuid in sorted(via)
    }
