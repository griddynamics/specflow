"""The store-neutral test probe: how live graph tests seed, break, read and clean a graph store.

Live tests must not hold a query language. Everything they did with raw statements (seeding nodes the
library never writes, injecting faults the verifier must find, reading back what a build left, cleaning
up) goes through a ``GraphProbe``: one implementation per store (``neo4j`` today; later stores add
theirs). The probe talks to the graph in terms of nodes and edges:

- a *project* is a ``group_id`` value; every probe call is scoped to one project id (the library's own
  isolation rule), except ``outside_counts`` and ``delete_projects``;
- a node is its labels and flat properties (``ProbeNode``), an edge is its type, its two end nodes and
  flat properties (``ProbeEdge``); ids are opaque strings valid only for the store that returned them;
- a ``Match`` selects the nodes of one project by label and property equality; it is how an edge to
  create names its ends and how a mutation names its targets.

Only the abstract methods below are store-specific. Everything derived (the canonical snapshot, the
before/after state, the spine reachability count, single-node lookups) is written once here on top of
``nodes`` and ``edges`` so every store is compared by the same code.

Property contract (design D7), which seeded and read values follow: a property set to ``None`` is
removed, lists keep their order, values are scalars or lists of scalars (never maps), and
strings/ints/floats/bools keep their type.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from graph.core import Ontology
from graph.core.queries import Query
from graph.core.store import GraphStore
from graph.stores.config import StoreConfig

SPEC_NODE: Final = "SpecNode"
# Properties holding a vector: never part of a canonical comparison, left off the end nodes of an edge.
VECTOR_PROPERTIES: Final = ("embedding", "name_embedding")
# Properties that differ between two runs of the same scripted scenario: ids and clocks. Exact names, plus
# every name ending in a suffix of ``UNSTABLE_SUFFIXES`` (``created_build_id``, ``updated_at``, ``inferred_at``).
UNSTABLE_PROPERTIES: Final = ("group_id", "uuid", "build_id")
UNSTABLE_SUFFIXES: Final = ("_build_id", "_at")
_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProbeError(AssertionError):
    """A probe call that cannot do what the test asked (a match that found nothing or too much)."""


def identifier(name: str) -> str:
    """``name`` when it is a plain label, relationship type or property name; probes inline these."""

    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"not a plain graph identifier: {name!r}")
    return name


def check_flat(props: Mapping[str, Any]) -> None:
    """Property contract (design D7): a stored property is a scalar or a list of scalars, never a map."""

    for name, value in props.items():
        items = value if isinstance(value, list) else [value]
        if any(isinstance(item, (dict, list, tuple, set)) for item in items):
            raise ProbeError(f"property {name!r} is not a scalar or a list of scalars")


@dataclass(frozen=True)
class ProbeNode:
    """A node as the store holds it (``labels`` sorted; ``props`` all properties, vectors included)."""

    id: str
    labels: tuple[str, ...]
    props: dict[str, Any]

    @property
    def kinds(self) -> tuple[str, ...]:
        """The labels other than ``SpecNode``."""

        return tuple(label for label in self.labels if label != SPEC_NODE)

    @property
    def kind(self) -> str | None:
        """The first label other than ``SpecNode`` (None for a bare node)."""

        return self.kinds[0] if self.kinds else None

    @property
    def ref(self) -> Any:
        """The node's stable reference in comparisons: spec id, else fragment id, document id, uuid."""

        for name in ("spec_id", "fragment_id", "document_id", "uuid"):
            if self.props.get(name) is not None:
                return self.props[name]
        return None

    def get(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self.props[name]


@dataclass(frozen=True)
class ProbeEdge:
    """An edge with both end nodes (their vector properties are left out)."""

    id: str
    type: str
    source: ProbeNode
    target: ProbeNode
    props: dict[str, Any]

    def get(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self.props[name]


@dataclass(frozen=True)
class Match:
    """Nodes of one project with ``label`` (any when None) and every property in ``props`` equal."""

    label: str | None = None
    props: Mapping[str, Any] = field(default_factory=dict)


def select(label: str | None = None, **props: Any) -> Match:
    """``select("Module", uuid="u-mod")``: a ``Match`` written as a call."""

    return Match(label, dict(props))


@dataclass(frozen=True)
class NodeSpec:
    """A node to create: its labels and properties (``group_id`` is set to the call's project id)."""

    labels: tuple[str, ...]
    props: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeSpec:
    """An edge to create between the one node ``source`` selects and the one node ``target`` selects."""

    type: str
    source: Match
    target: Match
    props: Mapping[str, Any] = field(default_factory=dict)


SchemaKind = Literal["unique", "range", "fulltext", "vector"]


@dataclass(frozen=True)
class SchemaObject:
    """One schema object as a store reports it, in neutral terms.

    ``unique`` also covers a node key; ``label`` is the one label (node) or relationship type the object
    is on; ``online`` is true for an object that needs no build (a constraint) or has finished building;
    ``dimensions`` and ``similarity`` (lower case) are set for a vector index only.
    """

    name: str
    kind: str
    entity: Literal["node", "relationship"]
    label: str
    properties: tuple[str, ...]
    online: bool
    dimensions: int | None = None
    similarity: str | None = None


@dataclass(frozen=True)
class GraphState:
    """Every node and relationship touching a project, comparable: two states are equal when nothing
    changed (ids included, so a rewritten element differs)."""

    nodes: tuple[str, ...]
    relationships: tuple[str, ...]


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class GraphProbe(ABC):
    """Test-support access to one graph store (see the module docstring)."""

    #: The backend name (``neo4j``, ``arcadedb``, ``postgres``, ``tigergraph``).
    backend: str

    def __init__(self, config: StoreConfig) -> None:
        self.config = config
        self._store: GraphStore | None = None

    # -- lifecycle ---------------------------------------------------------------------------------

    @property
    def database(self) -> str | None:
        """The store's database/schema/graph name from the settings (None: the backend's default)."""

        return self.config.name

    @property
    def store(self) -> GraphStore:
        """The library's ``GraphStore`` for the same server (what the code under test is handed)."""

        if self._store is None:
            from graph.stores import build_store

            self._store = build_store(self.config)
        return self._store

    async def close(self) -> None:
        """Close the probe's own connection and the store's."""

        if self._store is not None:
            await self._store.close()
            self._store = None
        await self._close()

    @abstractmethod
    async def _close(self) -> None: ...

    # -- reads -------------------------------------------------------------------------------------

    @abstractmethod
    async def nodes(self, project_id: str, label: str | None = None, **where: Any) -> list[ProbeNode]:
        """Nodes of ``project_id`` with ``label`` (any when None) whose properties equal ``where``."""

    @abstractmethod
    async def edges(self, project_id: str, type: str | None = None, **where: Any) -> list[ProbeEdge]:
        """Edges of type ``type`` (any when None), with properties equal to ``where``, that touch the
        project: the edge's own ``group_id`` or either end's is ``project_id``."""

    @abstractmethod
    async def count_nodes(self, project_id: str) -> int:
        """How many nodes carry ``group_id`` ``project_id``."""

    # -- seeding and fault injection ---------------------------------------------------------------

    @abstractmethod
    async def create_nodes(self, project_id: str, specs: Sequence[NodeSpec]) -> int:
        """Create the nodes (``group_id`` set); return how many."""

    @abstractmethod
    async def create_edges(self, project_id: str, specs: Sequence[EdgeSpec]) -> int:
        """Create the edges between nodes of ``project_id``; each end must select exactly one node
        (else ``ProbeError``, nothing of that call kept in doubt: callers seed before they assert)."""

    @abstractmethod
    async def update_nodes(
        self,
        project_id: str,
        match: Match,
        props: Mapping[str, Any] | None = None,
        *,
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> int:
        """Set properties (a ``None`` value removes the property) and add or remove labels on every
        node ``match`` selects; return how many nodes matched."""

    @abstractmethod
    async def delete_nodes(self, project_id: str, match: Match) -> int:
        """Delete (with their edges) the nodes ``match`` selects; return how many."""

    @abstractmethod
    async def update_edges(self, edge_ids: Sequence[str], props: Mapping[str, Any]) -> int:
        """Set properties (``None`` removes) on edges returned by ``edges``; return how many matched."""

    @abstractmethod
    async def delete_edges(self, edge_ids: Sequence[str]) -> int:
        """Delete edges returned by ``edges``; return how many."""

    @abstractmethod
    def failing_write_query(self, name: str) -> Query:
        """A project-scoped write query the store fails at run time (a division by zero, say) after it
        accepted the statement, taking the ``rows`` parameter and returning nothing. Tests substitute it
        for one of the library's write queries to prove that a grouped write rolls back (fault injection)."""

    # -- schema ------------------------------------------------------------------------------------

    @abstractmethod
    async def schema_objects(self) -> list[SchemaObject]:
        """Every constraint and index of the store's graph."""

    @abstractmethod
    async def drop_edge_key_index(self, relation: str) -> str | None:
        """Drop the library's edge-key index of ``relation``; return its name, None when the store
        holds none under the library's own name."""

    @abstractmethod
    async def vector_index_dimension(self, name: str) -> int | None:
        """The dimension of the vector index ``name`` (None when it does not exist)."""

    @abstractmethod
    async def await_indexes(self, timeout_s: float = 60.0) -> None:
        """Return when every index is built and searches see everything written so far."""

    @abstractmethod
    async def server_edition(self) -> str:
        """The server's edition label (Neo4j: ``community``)."""

    # -- cleanup and isolation ---------------------------------------------------------------------

    @abstractmethod
    async def delete_projects(self, project_ids: Sequence[str]) -> int:
        """Delete everything of the project ids; return how many nodes are left of them (0 expected)."""

    @abstractmethod
    async def outside_counts(self, prefix: str) -> dict[str, int]:
        """What a test must never touch on a shared database, as counts: nodes without a project, spec
        nodes, documents and fragments of projects not starting with ``prefix``, and relationships not
        between two nodes of such projects."""

    # -- derived: written once, on nodes and edges --------------------------------------------------

    async def one_node(self, project_id: str, label: str | None = None, **where: Any) -> ProbeNode:
        """The one node selected; ``ProbeError`` for none or several."""

        found = await self.nodes(project_id, label, **where)
        if len(found) != 1:
            raise ProbeError(f"expected one {label or 'node'} {where} in {project_id}, found {len(found)}")
        return found[0]

    async def state(self, project_id: str) -> GraphState:
        """Nodes (ids, labels, properties) and relationships (ids of the ends, type, properties)."""

        nodes = await self.nodes(project_id)
        edges = await self.edges(project_id)
        return GraphState(
            nodes=tuple(sorted(dump({"id": n.id, "labels": list(n.labels), "props": n.props}) for n in nodes)),
            relationships=tuple(
                sorted(dump({"s": e.source.id, "type": e.type, "t": e.target.id, "props": e.props}) for e in edges)
            ),
        )

    async def canonical_snapshot(self, project_id: str) -> dict[str, Any]:
        """The graph of a project in a form that is equal for equal graphs on any store.

        Nodes: sorted labels, ``spec_id`` and the other properties without ``UNSTABLE_PROPERTIES``
        (project id, uuids, build ids, timestamps) and without vectors. Edges: type, ``edge_key``, the
        references (``ProbeNode.ref``) of both ends and the other properties without unstable ones.
        Both lists are in a total order (``dump`` of the entry), so the result is deterministic.
        """

        nodes = [canonical_node(n) for n in await self.nodes(project_id)]
        edges = [canonical_edge(e) for e in await self.edges(project_id)]
        return {"nodes": sorted(nodes, key=dump), "edges": sorted(edges, key=dump)}

    async def spine_unreachable_count(
        self, project_id: str, ontology: Ontology, bases: Sequence[str] | None = None
    ) -> int:
        """Spine sources of a project that reach no spine root by a spine path, counted from nodes and
        edges independently of ``graph.build.spine`` (shipped ontology: ``PART_OF`` 1-3 hops,
        ``GROUPED_BY`` then ``REALIZED_BY``, or one ``RELATES_TO`` in either direction, to a
        ``Module``). ``bases`` limits the edges walked (a missing ``basis`` reads as ``stated``); None
        walks every edge."""

        spine = ontology.spine
        assert spine.roots == {"Module"}, "the walk below is written for the shipped spine"
        allowed = None if bases is None else set(bases)

        def walked(edge: ProbeEdge) -> bool:
            return allowed is None or edge.get("basis", "stated") in allowed

        edges = [e for e in await self.edges(project_id) if walked(e)]
        nodes = {n.id: n for n in await self.nodes(project_id, SPEC_NODE)}
        outgoing: dict[str, list[ProbeEdge]] = {}
        for edge in edges:
            outgoing.setdefault(edge.source.id, []).append(edge)

        def is_module(n: ProbeNode) -> bool:
            return "Module" in n.labels

        def reaches(start: ProbeNode) -> bool:
            frontier = [start.id]
            for _ in range(3):  # PART_OF*1..3 (relationship-unique paths; a walk is enough for reachability)
                step: list[str] = []
                for current in frontier:
                    for edge in outgoing.get(current, ()):
                        if edge.type == "PART_OF":
                            if is_module(edge.target):
                                return True
                            step.append(edge.target.id)
                frontier = step
            for edge in outgoing.get(start.id, ()):
                if edge.type == "GROUPED_BY" and any(
                    second.type == "REALIZED_BY" and is_module(second.target)
                    for second in outgoing.get(edge.target.id, ())
                ):
                    return True
            return any(
                e.type == "RELATES_TO"
                and ((e.source.id == start.id and is_module(e.target)) or (e.target.id == start.id and is_module(e.source)))
                for e in edges
            )

        sources = set(spine.sources)
        return sum(1 for n in nodes.values() if sources.intersection(n.labels) and not reaches(n))


def _stable_value(name: str, value: Any) -> Any:
    """``statements`` is a JSON text whose entries carry the clock of the build (``at``): drop it."""

    if name == "statements" and isinstance(value, str):
        try:
            entries = json.loads(value)
        except ValueError:
            return value
        if isinstance(entries, list):
            return [{k: v for k, v in e.items() if k != "at"} if isinstance(e, dict) else e for e in entries]
    return value


def canonical_props(props: Mapping[str, Any], *, skip: Iterable[str] = ()) -> dict[str, Any]:
    """``props`` without vectors, unstable properties and ``skip``; values as JSON-stable data."""

    omitted = {*VECTOR_PROPERTIES, *UNSTABLE_PROPERTIES, *skip}
    kept = {
        k: _stable_value(k, v) for k, v in props.items() if k not in omitted and not k.endswith(UNSTABLE_SUFFIXES)
    }
    return json.loads(dump(kept))


def canonical_node(node_: ProbeNode) -> dict[str, Any]:
    return {
        "labels": sorted(node_.labels),
        "spec_id": node_.props.get("spec_id"),
        "props": canonical_props(node_.props, skip=("spec_id",)),
    }


def canonical_edge(edge: ProbeEdge) -> dict[str, Any]:
    return {
        "type": edge.type,
        "edge_key": edge.props.get("edge_key"),
        "source": edge.source.ref,
        "target": edge.target.ref,
        "props": canonical_props(edge.props, skip=("edge_key",)),
    }
