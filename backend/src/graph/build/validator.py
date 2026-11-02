"""The shared validator of the ontology's hard rules."""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict

from graph.core.errors import ConformanceError
from graph.core.ontology import NodeView, Ontology, Pair

CheckName = Literal["illegal_pair", "where", "owner", "exclusive", "depth", "across", "acyclic", "stated_only"]
CHECKS: Final[tuple[CheckName, ...]] = get_args(CheckName)
"""The checks in report order."""

STATED: Final = "stated"
"""The basis an edge without one is read as."""
INFERRED: Final = "inferred"

UNKNOWN_RELATION: Final = "unknown_relation"
UNKNOWN_NODE: Final = "unknown_node"
SOURCE_WHERE: Final = "source_where"
TARGET_WHERE: Final = "target_where"

MAX_CYCLES: Final = 1000
"""Cycles reported per acyclic relation at most; one is enough to break D7."""

_CHECK_ORDER: Final = {name: index for index, name in enumerate(CHECKS)}


def _no_attributes() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ViewNode:
    """A node as the validator reads it: its id in the view, its domain label and attribute values."""

    id: str
    label: str
    attributes: Mapping[str, object] = field(default_factory=_no_attributes, hash=False, compare=False)


@dataclass(frozen=True)
class ViewEdge:
    """A domain edge between two view node ids; ``basis`` None reads as ``stated``. ``rule``: the
    inference rule of an ``inferred`` edge (its ``inference_rule``), None when unknown."""

    relation: str
    source: str
    target: str
    basis: str | None = None
    rule: str | None = None

    @property
    def effective_basis(self) -> str:
        return self.basis or STATED


class GraphView(Protocol):
    """A read-only graph the validator checks: Conform's target graph, Infer's or Verify's loaded graph."""

    def nodes(self, label: str | None = None) -> Iterable[ViewNode]:
        """Every node, or those of one label."""
        ...

    def node(self, node_id: str) -> ViewNode | None: ...

    def edges(self, relation: str | None = None) -> Iterable[ViewEdge]:
        """Every domain edge, or those of one relation."""
        ...

    def attrs(self, node_id: str) -> Mapping[str, object]:
        """The node's attribute values; empty for an unknown node."""
        ...

    def owner(self, node_id: str) -> str | None:
        """The node's owner: its target on an ``owner`` pair (lowest ``owner_rank``, then relation and
        target id when there are several); None when it has none."""
        ...


class InMemoryGraphView:
    """A ``GraphView`` over plain node and edge records."""

    def __init__(self, nodes: Iterable[ViewNode], edges: Iterable[ViewEdge], ontology: Ontology) -> None:
        by_id: dict[str, ViewNode] = {}
        for node in nodes:
            if node.id in by_id:
                raise ConformanceError("validator_duplicate_node", node=node.id)
            by_id[node.id] = node
        self._nodes: Mapping[str, ViewNode] = MappingProxyType(by_id)
        self._edges: tuple[ViewEdge, ...] = tuple(edges)
        self._ontology = ontology
        self._owners: Mapping[str, str] | None = None

    @classmethod
    def from_records(
        cls,
        nodes: Iterable[Mapping[str, object]],
        edges: Iterable[Mapping[str, object]],
        ontology: Ontology,
    ) -> InMemoryGraphView:
        """Nodes as ``{id, label, attributes?}``, edges as ``{relation, source, target, basis?}``."""

        return cls(
            (
                ViewNode(
                    id=str(record["id"]),
                    label=str(record["label"]),
                    attributes=MappingProxyType(dict(_mapping(record.get("attributes")))),
                )
                for record in nodes
            ),
            (
                ViewEdge(
                    relation=str(record["relation"]),
                    source=str(record["source"]),
                    target=str(record["target"]),
                    basis=None if record.get("basis") is None else str(record["basis"]),
                )
                for record in edges
            ),
            ontology,
        )

    def with_edges(self, edges: Iterable[ViewEdge]) -> InMemoryGraphView:
        """This view with ``edges`` added."""

        return InMemoryGraphView(self._nodes.values(), (*self._edges, *edges), self._ontology)

    def nodes(self, label: str | None = None) -> Iterator[ViewNode]:
        return (node for node in self._nodes.values() if label is None or node.label == label)

    def node(self, node_id: str) -> ViewNode | None:
        return self._nodes.get(node_id)

    def edges(self, relation: str | None = None) -> Iterator[ViewEdge]:
        return (edge for edge in self._edges if relation is None or edge.relation == relation)

    def attrs(self, node_id: str) -> Mapping[str, object]:
        node = self._nodes.get(node_id)
        return node.attributes if node is not None else MappingProxyType({})

    def owner(self, node_id: str) -> str | None:
        if self._owners is None:
            self._owners = MappingProxyType(_owners(self._nodes, self._edges, self._ontology))
        return self._owners.get(node_id)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _owners(nodes: Mapping[str, ViewNode], edges: Iterable[ViewEdge], ontology: Ontology) -> dict[str, str]:
    best: dict[str, tuple[int, str, str]] = {}
    for edge in edges:
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if source is None or target is None:
            continue
        pair = ontology.pair_rule(edge.relation, source.label, target.label)
        if pair is None or not pair.owner:
            continue
        rank = (pair.owner_rank, edge.relation, edge.target)
        if edge.source not in best or rank < best[edge.source]:
            best[edge.source] = rank
    return {node_id: rank[2] for node_id, rank in best.items()}


class Violation(BaseModel):
    """One edge that breaks a hard rule. ``source`` and ``target`` are view node ids; for ``acyclic``
    the edge closes the cycle ``cycle`` (node ids, from its least id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: CheckName
    relation: str
    source: str
    target: str
    basis: str
    detail: str | None = None
    cycle: tuple[str, ...] = ()

    def sort_key(self) -> tuple[int, str, str, str, str, str, tuple[str, ...]]:
        return (
            _CHECK_ORDER[self.check],
            self.relation,
            self.source,
            self.target,
            self.basis,
            self.detail or "",
            self.cycle,
        )


def violations(view: GraphView, ontology: Ontology, *, proposals: Sequence[ViewEdge] = ()) -> list[Violation]:
    """Hard-rule violations, in a deterministic order."""

    if not proposals:
        return _Graph.of(view, ontology).violations()
    checker = ProposalChecker(view, ontology)
    found: set[Violation] = set()
    for proposal in proposals:
        found.update(checker.offer(proposal))
    return sorted(found, key=Violation.sort_key)


class ProposalChecker:
    """Checks proposed edges one at a time against the view plus the edges accepted so far."""

    def __init__(self, view: GraphView, ontology: Ontology) -> None:
        self._graph = _Graph.of(view, ontology)
        self._baseline = frozenset(self._graph.violations())

    def check(self, edge: ViewEdge) -> list[Violation]:
        """The violations adding ``edge`` would introduce (violations the graph already had are left
        out); empty when it is valid. Nothing is added."""

        found = self._graph.plus(edge).violations()
        return [violation for violation in found if violation not in self._baseline]

    def accept(self, edge: ViewEdge) -> None:
        """Adds ``edge`` to the graph later proposals are checked against."""

        self._graph = self._graph.plus(edge)
        self._baseline = frozenset(self._graph.violations())

    def offer(self, edge: ViewEdge) -> list[Violation]:
        """``check`` the edge and ``accept`` it when it is valid; returns its violations."""

        found = self.check(edge)
        if not found:
            self.accept(edge)
        return found


@dataclass(frozen=True)
class _Graph:
    nodes: Mapping[str, ViewNode]
    edges: tuple[ViewEdge, ...]
    ontology: Ontology

    @classmethod
    def of(cls, view: GraphView, ontology: Ontology) -> _Graph:
        return cls(MappingProxyType({node.id: node for node in view.nodes()}), tuple(view.edges()), ontology)

    def plus(self, edge: ViewEdge) -> _Graph:
        return dataclasses.replace(self, edges=(*self.edges, edge))

    def violations(self) -> list[Violation]:
        found: set[Violation] = set()
        legal: list[tuple[ViewEdge, Pair]] = []
        for edge in self.edges:
            pair, problems = self._edge_problems(edge)
            found.update(problems)
            if pair is not None:
                legal.append((edge, pair))
        found.update(self._group_problems(legal))
        found.update(self._depth_problems(legal))
        found.update(self._cycle_problems())
        return sorted(found, key=Violation.sort_key)

    # One edge at a time: illegal_pair, stated_only, where, across

    def _edge_problems(self, edge: ViewEdge) -> tuple[Pair | None, list[Violation]]:
        ontology = self.ontology
        problems: list[Violation] = []
        relation = ontology.relations.get(edge.relation)
        if relation is not None and relation.stated_only and not self._stated_ok(edge):
            problems.append(_violation("stated_only", edge))
        source, target = self.nodes.get(edge.source), self.nodes.get(edge.target)
        if relation is None:
            return None, [*problems, _violation("illegal_pair", edge, UNKNOWN_RELATION)]
        if source is None or target is None:
            return None, [*problems, _violation("illegal_pair", edge, UNKNOWN_NODE)]
        pair = ontology.pair_rule(edge.relation, source.label, target.label)
        if pair is None:
            return None, [*problems, _violation("illegal_pair", edge)]
        source_view, target_view = NodeView(source.label, source.attributes), NodeView(target.label, target.attributes)
        if not ontology.where_holds(pair, source_view, target_view):
            failed = []
            if not ontology.where_holds(dataclasses.replace(pair, target_where={}), source_view, target_view):
                failed.append(SOURCE_WHERE)
            if not ontology.where_holds(dataclasses.replace(pair, source_where={}), source_view, target_view):
                failed.append(TARGET_WHERE)
            problems.append(_violation("where", edge, ",".join(failed)))
        differing = [name for name in pair.not_across if self._differ(source, target, name)]
        if differing:
            problems.append(_violation("across", edge, ",".join(differing)))
        return pair, problems

    def _stated_ok(self, edge: ViewEdge) -> bool:
        """Whether ``edge`` of a ``stated_only`` relation may stand: ``stated``, or ``inferred`` by a rule
        the ontology lets write it (the cue-gated exception)."""

        if edge.effective_basis == STATED:
            return True
        return (
            edge.basis == INFERRED
            and edge.rule is not None
            and edge.rule in self.ontology.cue_gated_rules(edge.relation)
        )

    def _differ(self, source: ViewNode, target: ViewNode, attribute: str) -> bool:
        values = []
        for node in (source, target):
            value = node.attributes.get(attribute)
            if value is None:
                value = self.ontology.attribute_default(node.label, attribute)
            if value is None:
                return False
            values.append(str(value))
        return values[0] != values[1]

    # Per source: owner, exclusive

    def _group_problems(self, legal: Sequence[tuple[ViewEdge, Pair]]) -> list[Violation]:
        owned: dict[str, list[ViewEdge]] = defaultdict(list)
        exclusive: dict[tuple[str, str, str, str], list[ViewEdge]] = defaultdict(list)
        for edge, pair in legal:
            if pair.owner:
                owned[edge.source].append(edge)
            if pair.exclusive:
                exclusive[(pair.relation, pair.source, pair.target, edge.source)].append(edge)
        problems: list[Violation] = []
        checks: tuple[tuple[CheckName, Iterable[list[ViewEdge]]], ...] = (
            ("owner", owned.values()),
            ("exclusive", exclusive.values()),
        )
        for check, groups in checks:
            for group in groups:
                if len({edge.target for edge in group}) > 1:
                    problems.extend(_violation(check, edge) for edge in group)
        return problems

    # Containment depth

    def _depth_problems(self, legal: Sequence[tuple[ViewEdge, Pair]]) -> list[Violation]:
        up: dict[tuple[str, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for edge, pair in legal:
            if pair.max_depth is not None:
                up[(pair.relation, pair.source, pair.target)][edge.source].add(edge.target)
        problems: list[Violation] = []
        for edge, pair in legal:
            if pair.max_depth is None:
                continue
            parents = up[(pair.relation, pair.source, pair.target)]
            if _chain_longer(parents, edge.source, edge.target, pair.max_depth):
                problems.append(_violation("depth", edge, f"max_depth={pair.max_depth}"))
        return problems

    # Cycles of acyclic relations, D7

    def _cycle_problems(self) -> list[Violation]:
        problems: list[Violation] = []
        for relation in sorted(self.ontology.acyclic_relations()):
            bases: dict[tuple[str, str], str] = {}
            adjacency: dict[str, set[str]] = defaultdict(set)
            for edge in self.edges:
                if edge.relation != relation or edge.source not in self.nodes or edge.target not in self.nodes:
                    continue
                key = (edge.source, edge.target)
                basis = edge.effective_basis
                bases[key] = min(basis, bases.get(key, basis))
                adjacency[edge.source].add(edge.target)
                adjacency.setdefault(edge.target, set())
            for cycle in simple_cycles(adjacency, limit=MAX_CYCLES):
                source, target = cycle[-1], cycle[0]
                problems.append(
                    Violation(
                        check="acyclic",
                        relation=relation,
                        source=source,
                        target=target,
                        basis=bases[(source, target)],
                        cycle=cycle,
                    )
                )
        return problems


def _violation(check: CheckName, edge: ViewEdge, detail: str | None = None) -> Violation:
    return Violation(
        check=check,
        relation=edge.relation,
        source=edge.source,
        target=edge.target,
        basis=edge.effective_basis,
        detail=detail,
    )


def _chain_longer(parents: Mapping[str, set[str]], source: str, target: str, max_depth: int) -> bool:
    """True when a simple chain of more than ``max_depth`` edges starts with ``source -> target``."""

    stack: list[tuple[str, int, frozenset[str]]] = [(target, 1, frozenset({source, target}))]
    while stack:
        node, length, seen = stack.pop()
        if length > max_depth:
            return True
        stack.extend(
            (parent, length + 1, seen | {parent}) for parent in sorted(parents.get(node, ())) if parent not in seen
        )
    return False


def simple_cycles(adjacency: Mapping[str, Iterable[str]], *, limit: int = MAX_CYCLES) -> list[tuple[str, ...]]:
    """Every elementary cycle of a directed graph once, each from its least node and in sorted order
    (Johnson's algorithm); at most ``limit`` cycles."""

    graph = {node: frozenset(targets) for node, targets in adjacency.items()}
    for targets in list(graph.values()):
        for node in targets:
            graph.setdefault(node, frozenset())
    cycles: list[tuple[str, ...]] = [(node,) for node in sorted(graph) if node in graph[node]]
    successors = {node: frozenset(target for target in targets if target != node) for node, targets in graph.items()}
    components = [component for component in _components(successors, set(successors)) if len(component) > 1]
    while components and len(cycles) < limit:
        component = components.pop()
        start = min(component)
        cycles.extend(_cycles_through(start, component, successors, limit - len(cycles)))
        rest = component - {start}
        components.extend(c for c in _components(successors, rest) if len(c) > 1)
    return sorted(cycles[:limit])


def _cycles_through(
    start: str, component: set[str], successors: Mapping[str, frozenset[str]], limit: int
) -> list[tuple[str, ...]]:
    def next_of(node: str) -> list[str]:
        return sorted((n for n in successors[node] if n in component), reverse=True)

    found: list[tuple[str, ...]] = []
    path = [start]
    blocked = {start}
    closed: set[str] = set()
    waiting: dict[str, set[str]] = defaultdict(set)
    stack: list[tuple[str, list[str]]] = [(start, next_of(start))]
    while stack and len(found) < limit:
        node, pending = stack[-1]
        if pending:
            following = pending.pop()
            if following == start:
                found.append(tuple(path))
                closed.update(path)
            elif following not in blocked:
                path.append(following)
                stack.append((following, next_of(following)))
                closed.discard(following)
                blocked.add(following)
                continue
        if not pending:
            if node in closed:
                _unblock(node, blocked, waiting)
            else:
                for successor in successors[node]:
                    if successor in component:
                        waiting[successor].add(node)
            stack.pop()
            path.pop()
    return found


def _unblock(node: str, blocked: set[str], waiting: dict[str, set[str]]) -> None:
    pending = {node}
    while pending:
        current = pending.pop()
        if current in blocked:
            blocked.discard(current)
            pending.update(waiting[current])
            waiting[current].clear()


def _components(successors: Mapping[str, frozenset[str]], nodes: set[str]) -> list[set[str]]:
    """Strongly connected components of the subgraph on ``nodes`` (iterative Tarjan), in sorted order."""

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[set[str]] = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(n for n in successors[root] if n in nodes)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(n for n in successors[child] if n in nodes))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                components.append(_pop_component(stack, on_stack, node))
    return sorted(components, key=min)


def _pop_component(stack: list[str], on_stack: set[str], root: str) -> set[str]:
    """Pop the members of ``root``'s component off ``stack`` (down to ``root``) and out of ``on_stack``."""

    component: set[str] = set()
    while True:
        member = stack.pop()
        on_stack.discard(member)
        component.add(member)
        if member == root:
            return component


def exclusive_conflicts(
    edges: Iterable[tuple[str, str, str]], label_of: Mapping[str, str | None], ontology: Ontology
) -> int:
    """Extra targets on ``exclusive`` pairs: per source, relation and target label, every target past the first
    ("Relations"; Conform, Verify and ``graph.build.metrics`` count them so).
    """

    exclusive = ontology.exclusive_pairs
    targets: Counter[tuple[str, str, str]] = Counter()
    for relation, source, target in edges:
        source_label, target_label = label_of.get(source), label_of.get(target)
        if source_label is None or target_label is None:
            continue
        if (relation, source_label, target_label) in exclusive:
            targets[(relation, source, target_label)] += 1
    return sum(count - 1 for count in targets.values() if count > 1)
