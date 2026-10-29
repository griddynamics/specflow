"""Verification: checks, metrics and repairs of the persisted graph (analysis.md "Verification").

``verify_build`` is the Verify step (build.md step 9): check, repair, check again, at most
``MAX_REPAIR_ROUNDS`` repair rounds; ``verify_project`` (api.md "Build") runs the checks read-only.
Both return a ``VerificationReport``; the run is ``ok`` when no blocking problem is left.

What is read (FR-VER-03). Only what ``graph.build`` owns: ``SpecNode`` nodes of the project with
their build-owned properties and build-owned labels, relationships of build-owned relations touching
them (domain edges), ``REFERENCED_BY`` and fragments. Foreign nodes, labels, relationship types and
properties are never read, so never reported or changed. Labels, relations, the spine, exclusive
pairs and id prefixes all come from the ontology.

Checks (all blocking). ``node_label``: a spec node without exactly one build-owned label (codes
``no_label``, ``several_labels``). ``spec_id``: missing, or not ``<id prefix of its label><SLUG>``
(``missing``, ``malformed``). ``illegal_edge``: a domain edge on a pair the ontology does not allow,
or with an end that is no spec node of the project (``illegal_pair``, ``endpoint_not_spec_node``); an
end with a label problem is judged by the label its ``spec_id`` prefix names (none: not judged, the
end is deleted with its edges).
``ungrounded_node``: no ``REFERENCED_BY`` to an ``active`` or ``skipped`` fragment.
``ungrounded_edge``: a domain edge that is not ``inferred`` (a missing ``basis`` counts as stated)
whose ``source_fragment_ids`` name no ``active`` or ``skipped`` fragment. ``inferred_edge``: an
``inferred`` edge without ``inference_rule``, a numeric ``confidence`` or a non-empty ``evidence``
list (``missing_rule``, ``missing_confidence``, ``missing_evidence``). ``fragment_state``: a status
outside ``active``/``skipped``/``retired``, or an ``active`` fragment whose ``extracted_hash`` is not
its ``content_hash`` (``unknown_status``, ``not_extracted``).

Repairs (one transaction per round). ``node_label``: keep the label the ``spec_id`` prefix names,
removing the other build labels, and set ``label_disagreement`` as Diff would for that label
(``Evidence.label_disagreement``); delete the node when the prefix names none. ``spec_id``: the
``spec_id`` the registry issued to the node (same uuid and identity key, well-formed, unused), else a
new one allocated like Diff does; a malformed old value no other node or registry row holds becomes
an alias. ``ungrounded_node`` / ``ungrounded_edge``: restore the evidence from the build's target graph
(``Evidence``: the ``REFERENCED_BY`` lines and edge fragments this build's diff wrote, else the conform
target graph's, by identity key or edge key), keeping only ``active`` and ``skipped`` fragments; with
none left, delete. ``illegal_edge`` and ``inferred_edge``: delete (the next inference re-adds a valid
inferred edge). ``fragment_state``: request a follow-up build for the fragment's document
(``follow_up_lineages``); the problem stays until that build, so the run is not ``ok``.

Valve (FR-VER-02). A round that deletes more than ``max(VALVE_MIN, VALVE_FRACTION * total)`` spec
nodes, or more than that of domain edges (``total``: the project's spec nodes, or its domain edges),
is not applied and ends the loop; so a single bad item can always be repaired. A deleted node's domain edges count as
deleted
edges (they go with it). The fractions are of the counts at the round's check.

Element ids address what was read: verification assumes no other writer changes the project while it
runs (as a build does).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, get_args

from graph.build.conform import GROUNDING_STATUSES
from graph.build.models import (
    CheckName,
    DiffArtifact,
    EdgeCreate,
    EdgeUpdate,
    EvidenceLines,
    FragmentStatus,
    NodeLinks,
    Problem,
    Repair,
    RepairAction,
    TargetGraph,
    VerificationMetrics,
    VerificationReport,
)
from graph.build.options import BuildContext
from graph.build.queries import (
    REPAIR_DELETE_EDGES,
    REPAIR_DELETE_NODES,
    REPAIR_EDGE_EVIDENCE,
    REPAIR_NODE_EVIDENCE,
    REPAIR_SET_LABELS,
    REPAIR_SET_SPEC_IDS,
    VERIFY_DOMAIN_EDGES,
    VERIFY_FRAGMENTS,
    VERIFY_SPEC_NODES,
)
from graph.build.spine import SpineEdge, SpineGraph, SpineNode
from graph.core.db import Executor
from graph.core.errors import ConformanceError
from graph.core.identity import SpecIdRegistry, SpecIdRow, allocate_spec_id, slug
from graph.core.logfields import count_fields
from graph.core.ontology import Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.verify")

MAX_REPAIR_ROUNDS: Final = 2
VALVE_FRACTION: Final = 0.05
"""Largest share of spec nodes, or of domain edges, one repair round may delete (FR-VER-02)."""
VALVE_MIN: Final = 1
"""Deletes of each kind a round may always make, whatever the share."""
BUILD_OWNER: Final = "build"
INFERRED: Final = "inferred"
READ_BASES: Final = ("stated", "implied")
"""Edge bases of the graph before inference (``spine_unreachable_before``)."""
ACTIVE: Final = "active"
FRAGMENT_STATUSES: Final = frozenset(get_args(FragmentStatus))
CHECKS: Final[tuple[CheckName, ...]] = get_args(CheckName)
EXPLICIT_ID_KIND: Final = "id"
KEY_SEPARATORS: Final = 2
"""Separators of an identity key ``<project>|<kind>|<value>``."""
"""Identity key kind whose value is the node's explicit id (data-model.md "Identity")."""
_SLUG: Final = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")


# The project as verification reads it


@dataclass(frozen=True, slots=True)
class VNode:
    """A spec node: build-owned properties as stored (a fault may give any type) and build labels."""

    id: str
    uuid: Any = None
    spec_id: Any = None
    name: Any = None
    identity_key: Any = None
    identity_aliases: tuple[str, ...] = ()
    spec_id_aliases: tuple[str, ...] = ()
    label_disagreement: Any = None
    labels: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    """Fragment ids of its ``REFERENCED_BY`` (any status)."""

    @property
    def label(self) -> str | None:
        return self.labels[0] if len(self.labels) == 1 else None

    @property
    def subject(self) -> str:
        for value in (self.spec_id, self.uuid):
            if isinstance(value, str) and value:
                return value
        return self.id


@dataclass(frozen=True, slots=True)
class VEdge:
    """A domain edge: its relation, build-owned properties as stored and its ends' element ids."""

    id: str
    relation: str
    source: str
    target: str
    uuid: Any = None
    edge_key: Any = None
    basis: Any = None
    fragment_ids: Any = None
    inference_rule: Any = None
    confidence: Any = None
    evidence: Any = None


@dataclass(frozen=True, slots=True)
class VFragment:
    fragment_id: Any
    document_id: Any = None
    status: Any = None
    content_hash: Any = None
    extracted_hash: Any = None
    grounds: bool = False
    """Some spec node of the project has a ``REFERENCED_BY`` to it."""


@dataclass(frozen=True)
class GraphState:
    """What verification read of one project: spec nodes by element id, domain edges, fragments."""

    nodes: Mapping[str, VNode]
    edges: tuple[VEdge, ...]
    fragments: tuple[VFragment, ...]
    grounding: frozenset[str] = field(init=False)
    """Fragment ids that can ground a node or edge (``active`` or ``skipped``)."""

    def __post_init__(self) -> None:
        grounding = frozenset(
            fragment.fragment_id
            for fragment in self.fragments
            if isinstance(fragment.fragment_id, str) and fragment.status in GROUNDING_STATUSES
        )
        object.__setattr__(self, "grounding", grounding)


@dataclass(frozen=True)
class OwnedNames:
    """The ontology names verification works with: build-owned labels and relations, id prefixes."""

    labels: tuple[str, ...]
    relations: tuple[str, ...]
    prefixes: Mapping[str, str]
    """``spec_id`` prefix to its label."""

    @classmethod
    def of(cls, ontology: Ontology) -> OwnedNames:
        labels = tuple(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER)
        relations = tuple(sorted(name for name in ontology.relations if ontology.owner_of(name) == BUILD_OWNER))
        prefixes = {prefix: label for label in labels if (prefix := ontology.node_type(label).id_prefix)}
        return cls(labels, relations, prefixes)

    def prefix_label(self, spec_id: Any) -> str | None:
        """The label whose id prefix starts ``spec_id`` and whose slug part is well-formed."""

        if not isinstance(spec_id, str):
            return None
        for prefix, label in self.prefixes.items():
            if spec_id.startswith(prefix) and _SLUG.fullmatch(spec_id[len(prefix) :]):
                return label
        return None

    def effective_label(self, node: VNode) -> str | None:
        """The node's label, or for a node with a label problem the label its ``spec_id`` prefix names
        (the ``node_label`` repair sets it; None: the node would be deleted)."""

        return node.label if node.label is not None else self.prefix_label(node.spec_id)

    def prefix_of(self, label: str) -> str | None:
        return next((prefix for prefix, owner in self.prefixes.items() if owner == label), None)


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str))
    return ()


async def load_state(executor: Executor, project_id: str, names: OwnedNames) -> GraphState:
    """Read the project's spec nodes, domain edges and fragments (build-owned data only)."""

    nodes = {
        row["id"]: VNode(
            id=row["id"],
            uuid=row["uuid"],
            spec_id=row["spec_id"],
            name=row["name"],
            identity_key=row["identity_key"],
            identity_aliases=_strings(row["identity_aliases"]),
            spec_id_aliases=_strings(row["spec_id_aliases"]),
            label_disagreement=row["label_disagreement"],
            labels=tuple(sorted(_strings(row["labels"]))),
            evidence=tuple(sorted(set(_strings(row["evidence"])))),
        )
        for row in await executor.read(VERIFY_SPEC_NODES, project_id, labels=list(names.labels))
    }
    edges = tuple(
        VEdge(
            id=row["id"],
            relation=row["relation"],
            source=row["source"],
            target=row["target"],
            uuid=row["uuid"],
            edge_key=row["edge_key"],
            basis=row["basis"],
            fragment_ids=row["fragment_ids"],
            inference_rule=row["inference_rule"],
            confidence=row["confidence"],
            evidence=row["evidence"],
        )
        for row in await executor.read(VERIFY_DOMAIN_EDGES, project_id, relations=list(names.relations))
    )
    fragments = tuple(
        VFragment(
            fragment_id=row["fragment_id"],
            document_id=row["document_id"],
            status=row["status"],
            content_hash=row["content_hash"],
            extracted_hash=row["extracted_hash"],
            grounds=bool(row["grounds"]),
        )
        for row in await executor.read(VERIFY_FRAGMENTS, project_id)
    )
    return GraphState(nodes=nodes, edges=edges, fragments=fragments)


# Checks


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem and what it is about (a node or edge element id, or a fragment)."""

    problem: Problem
    node: str | None = None
    edge: str | None = None
    fragment: VFragment | None = None


def edge_subject(edge: VEdge, state: GraphState) -> str:
    """``<source spec_id>|<RELATION>|<target spec_id>`` (an end that is no spec node: its element id)."""

    ends = [state.nodes[end].subject if end in state.nodes else end for end in (edge.source, edge.target)]
    return f"{ends[0]}|{edge.relation}|{ends[1]}"


def spec_id_code(node: VNode, names: OwnedNames) -> str | None:
    """``missing`` or ``malformed`` (see the module docstring), else None."""

    if not isinstance(node.spec_id, str) or not node.spec_id:
        return "missing"
    label = node.label
    if label is None:
        return None if names.prefix_label(node.spec_id) else "malformed"
    prefix = names.prefix_of(label)
    if prefix is None:
        return None
    rest = node.spec_id[len(prefix) :] if node.spec_id.startswith(prefix) else None
    return None if rest is not None and _SLUG.fullmatch(rest) else "malformed"


def edge_grounded(edge: VEdge, grounding: frozenset[str]) -> bool:
    return any(fid in grounding for fid in _strings(edge.fragment_ids))


def inferred_code(edge: VEdge) -> str | None:
    """What an inferred edge lacks (``missing_rule``, ``missing_confidence``, ``missing_evidence``)."""

    if not isinstance(edge.inference_rule, str) or not edge.inference_rule:
        return "missing_rule"
    if isinstance(edge.confidence, bool) or not isinstance(edge.confidence, int | float):
        return "missing_confidence"
    evidence = edge.evidence
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            return "missing_evidence"
    if not isinstance(evidence, list) or not evidence:
        return "missing_evidence"
    return None


def _node_findings(state: GraphState, names: OwnedNames) -> list[Finding]:
    findings: list[Finding] = []
    for node in sorted(state.nodes.values(), key=lambda item: (item.subject, item.id)):
        if node.label is None:
            code = "no_label" if not node.labels else "several_labels"
            findings.append(Finding(Problem(check="node_label", subject=node.subject, code=code), node=node.id))
        id_code = spec_id_code(node, names)
        if id_code is not None:
            findings.append(Finding(Problem(check="spec_id", subject=node.subject, code=id_code), node=node.id))
        if not any(fid in state.grounding for fid in node.evidence):
            findings.append(Finding(Problem(check="ungrounded_node", subject=node.subject), node=node.id))
    return findings


def _edge_findings(state: GraphState, ontology: Ontology, names: OwnedNames) -> list[Finding]:
    findings: list[Finding] = []
    for edge in sorted(state.edges, key=lambda item: (item.relation, edge_subject(item, state), item.id)):
        subject = edge_subject(edge, state)
        source, target = state.nodes.get(edge.source), state.nodes.get(edge.target)
        if source is None or target is None:
            findings.append(
                Finding(Problem(check="illegal_edge", subject=subject, code="endpoint_not_spec_node"), edge=edge.id)
            )
        else:
            source_label, target_label = names.effective_label(source), names.effective_label(target)
            if (
                source_label is not None
                and target_label is not None
                and not ontology.is_legal(edge.relation, source_label, target_label)
            ):
                findings.append(
                    Finding(Problem(check="illegal_edge", subject=subject, code="illegal_pair"), edge=edge.id)
                )
        if edge.basis == INFERRED:
            code = inferred_code(edge)
            if code is not None:
                findings.append(Finding(Problem(check="inferred_edge", subject=subject, code=code), edge=edge.id))
        elif not edge_grounded(edge, state.grounding):
            findings.append(Finding(Problem(check="ungrounded_edge", subject=subject), edge=edge.id))
    return findings


def _fragment_findings(state: GraphState) -> list[Finding]:
    findings: list[Finding] = []
    for fragment in state.fragments:
        if fragment.status not in FRAGMENT_STATUSES:
            code = "unknown_status"
        elif fragment.status == ACTIVE and (
            fragment.extracted_hash is None or fragment.extracted_hash != fragment.content_hash
        ):
            code = "not_extracted"
        else:
            continue
        findings.append(
            Finding(Problem(check="fragment_state", subject=str(fragment.fragment_id), code=code), fragment=fragment)
        )
    return findings


def find_problems(state: GraphState, ontology: Ontology, names: OwnedNames) -> list[Finding]:
    """Every blocking problem of ``state``: nodes, then edges, then fragments."""

    return [*_node_findings(state, names), *_edge_findings(state, ontology, names), *_fragment_findings(state)]


# Metrics


def spine_graph(state: GraphState) -> SpineGraph:
    """``state`` as the shared spine walk reads it (``graph.build.spine``), keyed by element id."""

    return SpineGraph(
        nodes=tuple(
            SpineNode(key=node.id, spec_id=node.subject, label=node.label)
            for node in state.nodes.values()
            if node.label is not None
        ),
        edges=tuple(
            SpineEdge(relation=edge.relation, source=edge.source, target=edge.target, basis=edge.basis)
            for edge in state.edges
        ),
    )


def exclusive_conflicts(state: GraphState, ontology: Ontology) -> int:
    """Extra targets on ``exclusive`` pairs: per source, relation and target label, every target past
    the first (ontology.md "Relations"; counted as Conform counts them)."""

    exclusive = ontology.exclusive_pairs
    targets: Counter[tuple[str, str, str]] = Counter()
    for edge in state.edges:
        source, target = state.nodes.get(edge.source), state.nodes.get(edge.target)
        if source is None or target is None or source.label is None or target.label is None:
            continue
        if (edge.relation, source.label, target.label) in exclusive:
            targets[(edge.relation, edge.source, target.label)] += 1
    return sum(count - 1 for count in targets.values() if count > 1)


def dropped_by_relation(target: TargetGraph | None) -> dict[str, int]:
    """Edges Conform dropped by legalisation, per relation (from the build's conform artifact)."""

    if target is None:
        return {}
    return {
        relation: sum(reasons.values())
        for relation, reasons in sorted(target.counters.dropped_edges.items())
        if sum(reasons.values())
    }


def measure(state: GraphState, ontology: Ontology, dropped: Mapping[str, int]) -> VerificationMetrics:
    """The metrics of analysis.md "Verification" on ``state``; ``spine_unreachable_before`` reads the
    ``stated`` and ``implied`` edges only (the graph as it was before inference)."""

    spine, sources = spine_graph(state), ontology.spine.sources
    return VerificationMetrics(
        spine_unreachable_before=len(spine.unreached(ontology, sources, READ_BASES)),
        spine_unreachable=len(spine.unreached(ontology, sources)),
        exclusive_conflicts=exclusive_conflicts(state, ontology),
        label_disagreement=sum(1 for node in state.nodes.values() if node.label_disagreement is not None),
        dropped_edges=dict(dropped),
        fragments_without_nodes=sum(
            1 for fragment in state.fragments if fragment.status == ACTIVE and not fragment.grounds
        ),
    )


# Evidence of the build's target graph


@dataclass(frozen=True)
class Evidence:
    """The evidence this build meant to persist: what its diff wrote (``REFERENCED_BY`` lines by node
    uuid, edge fragments by edge uuid), else the conform target graph's (by identity key and its
    aliases, by edge key). Empty for ``verify_project``."""

    node_links: Mapping[str, tuple[EvidenceLines, ...]] = field(default_factory=dict)
    node_keys: Mapping[str, tuple[EvidenceLines, ...]] = field(default_factory=dict)
    edge_writes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    edge_keys: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    node_labels: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)
    """Target node label and ``label_disagreement`` by identity key and its aliases."""

    @classmethod
    def of_build(cls, target: TargetGraph | None, diff: DiffArtifact | None) -> Evidence:
        node_links: dict[str, tuple[EvidenceLines, ...]] = {}
        edge_writes: dict[str, tuple[str, ...]] = {}
        for operation in diff.operations if diff else ():
            if isinstance(operation, NodeLinks):
                node_links[operation.uuid] = operation.evidence
            elif isinstance(operation, EdgeCreate | EdgeUpdate):
                ids = _strings(operation.properties.get("source_fragment_ids"))
                if ids:
                    edge_writes[operation.uuid] = ids
        node_keys: dict[str, tuple[EvidenceLines, ...]] = {}
        edge_keys: dict[str, tuple[str, ...]] = {}
        node_labels: dict[str, tuple[str, str | None]] = {}
        if target is not None:
            for node in target.nodes:
                node_keys[node.identity_key] = node.evidence
                node_labels[node.identity_key] = (node.label, node.label_disagreement)
            for node in target.nodes:
                for alias in node.identity_aliases:
                    node_keys.setdefault(alias, node.evidence)
                    node_labels.setdefault(alias, (node.label, node.label_disagreement))
            edge_keys = {edge.edge_key: edge.fragment_ids for edge in target.edges}
        return cls(node_links, node_keys, edge_writes, edge_keys, node_labels)

    def label_disagreement(self, node: VNode, label: str) -> str | None:
        """``label_disagreement`` of ``node`` once its label is ``label``, as Diff computes it for an
        update: the target node's own, else the target's label when it is not ``label``. Without a
        target node, the stored value unless it names ``label``."""

        for key in (node.identity_key, *node.identity_aliases):
            found = self.node_labels.get(key) if isinstance(key, str) else None
            if found is not None:
                target_label, disagreement = found
                return disagreement or (target_label if target_label != label else None)
        stored = node.label_disagreement
        return stored if isinstance(stored, str) and stored and stored != label else None

    def for_node(self, node: VNode, grounding: frozenset[str]) -> tuple[EvidenceLines, ...]:
        """The first source's evidence of ``node`` that keeps a grounding fragment, restricted to those."""

        sources: list[tuple[EvidenceLines, ...] | None] = []
        if isinstance(node.uuid, str):
            sources.append(self.node_links.get(node.uuid))
        sources.extend(
            self.node_keys.get(key) for key in (node.identity_key, *node.identity_aliases) if isinstance(key, str)
        )
        for found in sources:
            kept = tuple(lines for lines in found or () if lines.fragment_id in grounding)
            if kept:
                return kept
        return ()

    def for_edge(self, edge: VEdge, grounding: frozenset[str]) -> tuple[str, ...]:
        """Like ``for_node``: evidence fragment ids of ``edge``."""

        sources = [
            self.edge_writes.get(edge.uuid) if isinstance(edge.uuid, str) else None,
            self.edge_keys.get(edge.edge_key) if isinstance(edge.edge_key, str) else None,
        ]
        for found in sources:
            kept = tuple(dict.fromkeys(fid for fid in found or () if fid in grounding))
            if kept:
                return kept
        return ()


NO_EVIDENCE: Final = Evidence()


# Repair planning


@dataclass
class RoundPlan:
    """The repairs of one round, as query rows by element id."""

    delete_nodes: dict[str, VNode] = field(default_factory=dict)
    delete_edges: dict[str, VEdge] = field(default_factory=dict)
    labels: list[dict[str, Any]] = field(default_factory=list)
    spec_ids: list[dict[str, Any]] = field(default_factory=list)
    node_evidence: list[dict[str, Any]] = field(default_factory=list)
    edge_evidence: list[dict[str, Any]] = field(default_factory=list)
    registry_rows: list[SpecIdRow] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    follow_ups: dict[str, str] = field(default_factory=dict)
    """Fragment id to the lineage a follow-up build is requested for."""

    @property
    def writes(self) -> bool:
        return bool(
            self.delete_nodes
            or self.delete_edges
            or self.labels
            or self.spec_ids
            or self.node_evidence
            or self.edge_evidence
        )

    def add(self, round_: int, check: CheckName, action: RepairAction, subject: str, value: str | None = None) -> None:
        self.repairs.append(Repair(round=round_, check=check, action=action, subject=subject, value=value))


def deleted_edges(plan: RoundPlan, state: GraphState) -> dict[str, VEdge]:
    """Every domain edge the round deletes: explicitly, or with a deleted end."""

    gone = dict(plan.delete_edges)
    for edge in state.edges:
        if edge.source in plan.delete_nodes or edge.target in plan.delete_nodes:
            gone.setdefault(edge.id, edge)
    return gone


def valve(plan: RoundPlan, state: GraphState) -> dict[str, int] | None:
    """The round's planned deletes and the project's totals when they exceed ``max(VALVE_MIN,
    VALVE_FRACTION * total)`` of the spec nodes or of the domain edges (the round must not be
    applied); else None."""

    nodes, edges = len(plan.delete_nodes), len(deleted_edges(plan, state))
    total_nodes, total_edges = len(state.nodes), len(state.edges)
    if nodes > max(VALVE_MIN, VALVE_FRACTION * total_nodes) or edges > max(VALVE_MIN, VALVE_FRACTION * total_edges):
        return {"nodes": nodes, "edges": edges, "spec_nodes": total_nodes, "domain_edges": total_edges}
    return None


@dataclass
class SpecIds:
    """``spec_id`` allocation for repairs: the registry's rows and every id the graph holds."""

    taken: dict[str, SpecIdRow]
    used: dict[str, str]
    """Every ``spec_id`` and alias a node holds (or a repair gave), to the element id of its node."""
    by_uuid: dict[str, list[SpecIdRow]]

    @classmethod
    def of(cls, taken: Mapping[str, SpecIdRow], state: GraphState) -> SpecIds:
        used = {
            value: node.id
            for node in state.nodes.values()
            for value in (node.spec_id, *node.spec_id_aliases)
            if isinstance(value, str) and value
        }
        by_uuid: dict[str, list[SpecIdRow]] = {}
        for row in sorted(taken.values(), key=lambda row: (row.state != "active", row.spec_id)):
            if row.state != "alias":
                by_uuid.setdefault(row.uuid, []).append(row)
        return cls(dict(taken), used, by_uuid)

    def assign(self, node: VNode, label: str, ontology: Ontology, names: OwnedNames) -> str:
        """The node's issued ``spec_id`` when the registry has a well-formed unused one for its uuid and
        identity key, else a new one (``<prefix of label><slug of its name>``, never one in use)."""

        key = node.identity_key if isinstance(node.identity_key, str) else ""
        keys = {key, *node.identity_aliases}
        prefix = ontology.id_prefix(label)
        for row in self.by_uuid.get(node.uuid, []) if isinstance(node.uuid, str) else []:
            if row.identity_key in keys and row.spec_id not in self.used and names.prefix_label(row.spec_id) == label:
                return self._take(row.spec_id, row, node)
        explicit = (
            key.split("|", 2)[2]
            if key.count("|") >= KEY_SEPARATORS and key.split("|", 2)[1] == EXPLICIT_ID_KIND
            else None
        )
        name = node.name if isinstance(node.name, str) else ""
        spec_id = allocate_spec_id(prefix, slug(name, explicit), key, self.taken)
        while spec_id in self.used:
            self.taken[spec_id] = SpecIdRow(spec_id=spec_id, identity_key="", uuid="", state="alias")
            spec_id = allocate_spec_id(prefix, slug(name, explicit), key, self.taken)
        uuid = node.uuid if isinstance(node.uuid, str) else ""
        return self._take(spec_id, SpecIdRow(spec_id=spec_id, identity_key=key, uuid=uuid, state="active"), node)

    def _take(self, spec_id: str, row: SpecIdRow, node: VNode) -> str:
        self.used[spec_id] = node.id
        self.taken[spec_id] = row
        return spec_id

    def free(self, value: Any, node: VNode) -> bool:
        """Whether ``value`` may become an alias of ``node``: a string no other node holds that the
        registry never issued, or issued as an alias of this node (a round resumed after its registry
        rows were recorded)."""

        if not isinstance(value, str) or not value or self.used.get(value, node.id) != node.id:
            return False
        row = self.taken.get(value)
        return row is None or (row.state == "alias" and row.uuid == (node.uuid if isinstance(node.uuid, str) else ""))


def _plan_labels(
    plan: RoundPlan,
    by_check: Mapping[str, Sequence[Finding]],
    state: GraphState,
    *,
    evidence: Evidence,
    names: OwnedNames,
    round_: int,
) -> dict[str, str]:
    """Set the label a node's ``spec_id`` prefix names, or delete the node when it names none; returns
    the labels set (by node id)."""

    labels: dict[str, str] = {}
    for finding in by_check["node_label"]:
        node = state.nodes[finding.node or ""]
        label = names.prefix_label(node.spec_id)
        if label is None:
            plan.delete_nodes[node.id] = node
            plan.add(round_, "node_label", "delete_node", node.subject)
            continue
        labels[node.id] = label
        plan.labels.append(
            {
                "id": node.id,
                "label": label,
                "remove": [other for other in node.labels if other != label],
                "label_disagreement": evidence.label_disagreement(node, label),
            }
        )
        plan.add(round_, "node_label", "set_label", node.subject, label)
    return labels


def _plan_spec_ids(
    plan: RoundPlan,
    findings: Sequence[Finding],
    state: GraphState,
    labels: Mapping[str, str],
    *,
    spec_ids: SpecIds | None,
    ontology: Ontology,
    names: OwnedNames,
    round_: int,
) -> None:
    """Assign a ``spec_id`` to each node that lacks a valid one; the old one becomes an alias when free."""

    for finding in findings:
        node = state.nodes[finding.node or ""]
        label = labels.get(node.id) or node.label
        if node.id in plan.delete_nodes or label is None or spec_ids is None:
            continue
        new = spec_ids.assign(node, label, ontology, names)
        aliases = [*node.spec_id_aliases]
        if spec_ids.free(node.spec_id, node) and node.spec_id != new:
            aliases.append(node.spec_id)
            uuid = node.uuid if isinstance(node.uuid, str) else ""
            key = node.identity_key if isinstance(node.identity_key, str) else ""
            plan.registry_rows.append(SpecIdRow(spec_id=node.spec_id, identity_key=key, uuid=uuid, state="alias"))
            spec_ids.taken[node.spec_id] = plan.registry_rows[-1]
        plan.registry_rows.append(spec_ids.taken[new])
        # No aliases: null, so a node without the property keeps it absent.
        plan.spec_ids.append({"id": node.id, "spec_id": new, "aliases": sorted(set(aliases) - {new}) or None})
        plan.add(round_, "spec_id", "assign_spec_id", node.subject, new)


def _plan_node_grounding(
    plan: RoundPlan, findings: Sequence[Finding], state: GraphState, *, evidence: Evidence, round_: int
) -> None:
    """Restore the evidence of each ungrounded node, or delete the node when none can be restored."""

    for finding in findings:
        node = state.nodes[finding.node or ""]
        if node.id in plan.delete_nodes:
            continue
        restored = evidence.for_node(node, state.grounding)
        if restored:
            plan.node_evidence.append(
                {"id": node.id, "evidence": [lines.model_dump(mode="json") for lines in restored]}
            )
            plan.add(round_, "ungrounded_node", "restore_evidence", node.subject)
        else:
            plan.delete_nodes[node.id] = node
            plan.add(round_, "ungrounded_node", "delete_node", node.subject)


def _plan_edges(
    plan: RoundPlan,
    by_check: Mapping[str, Sequence[Finding]],
    state: GraphState,
    *,
    evidence: Evidence,
    round_: int,
) -> None:
    """Restore the evidence of an ungrounded edge, or delete the edge; an edge with a deleted end goes along."""

    edges = {edge.id: edge for edge in state.edges}
    for check in ("illegal_edge", "inferred_edge", "ungrounded_edge"):
        for finding in by_check[check]:
            edge = edges[finding.edge or ""]
            if edge.id in plan.delete_edges or edge.source in plan.delete_nodes or edge.target in plan.delete_nodes:
                continue
            if check == "ungrounded_edge":
                restored_ids = evidence.for_edge(edge, state.grounding)
                if restored_ids:
                    plan.edge_evidence.append({"id": edge.id, "fragment_ids": list(restored_ids)})
                    plan.add(round_, check, "restore_evidence", finding.problem.subject)
                    continue
            plan.delete_edges[edge.id] = edge
            plan.add(round_, check, "delete_edge", finding.problem.subject)


def plan_round(
    findings: Sequence[Finding],
    state: GraphState,
    ontology: Ontology,
    names: OwnedNames,
    evidence: Evidence,
    *,
    spec_ids: SpecIds | None,
    round_: int,
) -> RoundPlan:
    """The repairs of ``findings`` (analysis.md "Verification" table), in check order: labels first
    (a node whose label cannot be set is deleted, and nothing else is done to it), then ``spec_id``,
    grounding, then edges (an edge with a deleted end goes with it), then fragments."""

    plan = RoundPlan()
    by_check: dict[str, list[Finding]] = {check: [] for check in CHECKS}
    for finding in findings:
        by_check[finding.problem.check].append(finding)
    labels = _plan_labels(plan, by_check, state, evidence=evidence, names=names, round_=round_)
    _plan_spec_ids(
        plan, by_check["spec_id"], state, labels, spec_ids=spec_ids, ontology=ontology, names=names, round_=round_
    )
    _plan_node_grounding(plan, by_check["ungrounded_node"], state, evidence=evidence, round_=round_)
    _plan_edges(plan, by_check, state, evidence=evidence, round_=round_)
    for finding in by_check["fragment_state"]:
        fragment = finding.fragment
        if fragment is not None and isinstance(fragment.document_id, str) and fragment.document_id:
            plan.follow_ups[finding.problem.subject] = fragment.document_id
    return plan


async def apply_round(plan: RoundPlan, executor: Executor, project_id: str, names: OwnedNames) -> None:
    """Apply one round's repairs in one transaction: edge deletes, node deletes, labels, ``spec_id``s,
    restored evidence."""

    relations = list(names.relations)
    async with executor.tx(project_id) as transaction:
        if plan.delete_edges:
            await transaction.run(REPAIR_DELETE_EDGES, ids=list(plan.delete_edges), relations=relations)
        if plan.delete_nodes:
            await transaction.run(REPAIR_DELETE_NODES, ids=list(plan.delete_nodes))
        if plan.labels:
            await transaction.run(REPAIR_SET_LABELS, rows=plan.labels)
        if plan.spec_ids:
            await transaction.run(REPAIR_SET_SPEC_IDS, rows=plan.spec_ids)
        if plan.node_evidence:
            await transaction.run(REPAIR_NODE_EVIDENCE, rows=plan.node_evidence)
        if plan.edge_evidence:
            await transaction.run(REPAIR_EDGE_EVIDENCE, rows=plan.edge_evidence, relations=relations)


# The verification run


@dataclass(frozen=True)
class RepairScope:
    """What a repairing run needs besides the graph: the build, its target graph evidence and the
    ``spec_id`` registry."""

    build_id: str
    evidence: Evidence
    registry: SpecIdRegistry


def _counts(findings: Iterable[Finding]) -> dict[str, int]:
    counted = Counter(finding.problem.check for finding in findings)
    return {check: counted[check] for check in CHECKS if counted[check]}


async def verify_graph(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    *,
    dropped: Mapping[str, int],
    repair: RepairScope | None,
) -> VerificationReport:
    """Check the project's graph; with ``repair``, repair and check again, at most
    ``MAX_REPAIR_ROUNDS`` rounds (a round over the valve ends the loop unapplied). Without it, the
    run is read-only: no write, no follow-up request."""

    names = OwnedNames.of(ontology)
    state = await load_state(executor, project_id, names)
    findings = find_problems(state, ontology, names)
    found = _counts(findings)
    totals = (len(state.nodes), len(state.edges))
    log_event(
        logger,
        INFO,
        "graph.verify.checked",
        project_id=project_id,
        round=0,
        spec_nodes=totals[0],
        domain_edges=totals[1],
        **count_fields(found),
    )
    repairs: list[Repair] = []
    follow_ups: dict[str, str] = {}
    removed_ids: list[str] = []
    removed_inferred: list[str] = []
    rounds = blocked = 0
    blocked_at: dict[str, int] = {}
    while repair is not None and findings and rounds < MAX_REPAIR_ROUNDS:
        round_ = rounds + 1
        taken = None
        if any(finding.problem.check == "spec_id" for finding in findings):
            taken = SpecIds.of(await repair.registry.taken(project_id), state)
        plan = plan_round(findings, state, ontology, names, repair.evidence, spec_ids=taken, round_=round_)
        for fid, lineage in plan.follow_ups.items():
            if fid not in follow_ups:
                follow_ups[fid] = lineage
                repairs.append(
                    Repair(round=round_, check="fragment_state", action="request_follow_up", subject=fid, value=lineage)
                )
        if not plan.writes:
            break
        over = valve(plan, state)
        if over is not None:
            blocked += 1
            blocked_at = over
            log_event(
                logger, WARNING, "graph.verify.round_blocked", project_id=project_id, round=round_, **count_fields(over)
            )
            break
        gone = deleted_edges(plan, state)
        if plan.registry_rows:
            await repair.registry.record(project_id, repair.build_id, plan.registry_rows)
        await apply_round(plan, executor, project_id, names)
        rounds = round_
        repairs.extend(plan.repairs)
        removed_ids.extend(node.subject for node in plan.delete_nodes.values() if isinstance(node.spec_id, str))
        removed_inferred.extend(edge_subject(edge, state) for edge in gone.values() if edge.basis == INFERRED)
        log_event(
            logger,
            INFO,
            "graph.verify.repaired",
            project_id=project_id,
            round=round_,
            **count_fields(Counter(item.action for item in plan.repairs)),
        )
        state = await load_state(executor, project_id, names)
        findings = find_problems(state, ontology, names)
        log_event(
            logger, INFO, "graph.verify.checked", project_id=project_id, round=round_, **count_fields(_counts(findings))
        )
    report = VerificationReport(
        ok=not findings,
        read_only=repair is None,
        rounds=rounds,
        blocked_rounds=blocked,
        spec_nodes=totals[0],
        domain_edges=totals[1],
        found=found,
        problems=tuple(finding.problem for finding in findings),
        repairs=tuple(repairs),
        valve=blocked_at,
        removed_spec_ids=tuple(sorted(set(removed_ids))),
        removed_inferred_edges=tuple(sorted(set(removed_inferred))),
        metrics=measure(state, ontology, dropped),
        follow_up_lineages=tuple(sorted(set(follow_ups.values()))),
    )
    log_event(
        logger,
        INFO if report.ok else WARNING,
        "graph.verify.done",
        project_id=project_id,
        ok=report.ok,
        read_only=report.read_only,
        rounds=rounds,
        blocked_rounds=blocked,
        problems=len(report.problems),
        repairs=len(report.repairs),
        follow_ups=len(report.follow_up_lineages),
    )
    return report


async def verify_build(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    *,
    build_id: str,
    registry: SpecIdRegistry,
    target: TargetGraph | None,
    diff: DiffArtifact | None,
) -> VerificationReport:
    """The Verify step's run (build.md step 9): checks and repairs, evidence restored from this build's
    diff and target graph, metrics with this build's dropped edges."""

    return await verify_graph(
        executor,
        project_id,
        ontology,
        dropped=dropped_by_relation(target),
        repair=RepairScope(build_id=build_id, evidence=Evidence.of_build(target, diff), registry=registry),
    )


async def verify_project(context: BuildContext) -> VerificationReport:
    """Run the checks read-only (api.md "Build", FR-VER-04): no write, no repair, no follow-up request.

    ``dropped_edges`` comes from the conform artifact of ``context.build_id``, else of the project's
    latest finished build (empty when there is none)."""

    build_id = context.build_id
    if build_id is None:
        latest = await context.builds.latest_build(context.project_id, finished_only=True)
        build_id = latest.build_id if latest is not None else None
    target = None
    if build_id is not None:
        payload = await context.builds.get_artifact(context.project_id, build_id, "conform")
        target = TargetGraph.model_validate(payload) if payload is not None else None
    if context.executor is None:  # BuildContext always sets one
        raise ConformanceError("build_context_without_executor")
    return await verify_graph(
        context.executor, context.project_id, context.ontology, dropped=dropped_by_relation(target), repair=None
    )
