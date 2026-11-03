"""Change report: a build's changes classified per node and edge."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, get_args

from graph.build.diff import LINKS_CAUSE, MERGED_CAUSE, QUOTE_PROPERTY, STMT_PROPERTY, modified_causes
from graph.build.models import (
    Basis,
    ChangeClass,
    ChangedEdge,
    ChangedNode,
    ChangeReport,
    DiffArtifact,
    EdgeChangeKind,
    EdgeCreate,
    EdgeDelete,
    EdgeUpdate,
    InferenceReport,
    NodeCarryOver,
    NodeCreate,
    NodeDelete,
    NodeLinks,
    NodeMerge,
    NodeUpdate,
    VerificationReport,
    edge_ref,
)
from graph.core.identity import SpecIdRow
from graph.core.ontology import Ontology

CLASS_ORDER: Final = get_args(ChangeClass)
NAME_PROPERTY: Final = "name"
MEANING_PROPERTIES: Final = frozenset({NAME_PROPERTY})
"""Meaning properties besides the label's ontology attributes (``quote`` excluded)."""
IDENTITY_PROPERTIES: Final = frozenset({"identity_key"})
"""Causes that make no class of their own (``rekeyed`` and ``carried_over`` name them)."""
EDGE_EVIDENCE_CAUSES: Final = frozenset({"fact", "source_fragment_ids"})
"""``ChangedEdge.causes`` that are evidence only: the ends' ``changed_evidence``."""
LABEL_CAUSE: Final = "label"
"""Cause of a label set by a verification repair (``set_label``)."""
ACCEPTED_ITEMS: Final = frozenset({"accepted", "downgraded"})
REF_PARTS: Final = 3
"""An ``edge_ref`` splits into source ``spec_id``, relation and target ``spec_id``."""
INFERRED: Final = "inferred"
IMPLIED: Final = "implied"
STATED: Final = "stated"
ACTIVE: Final = "active"
EDGE_STAMPS: Final = frozenset({"edge_key", "updated_at", "last_build_id"})
INFERRED_EDGE_PROPERTIES: Final = frozenset({"inference_rule", "confidence", "evidence", "judge_model", "inferred_at"})


@dataclass(slots=True)
class _Node:
    label: str = ""
    classes: set[str] = field(default_factory=set)
    causes: list[str] = field(default_factory=list)
    previous: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Ids:
    """Node uuids and identity keys to ``spec_id``, and ``spec_id`` to label, for naming."""

    by_uuid: dict[str, str] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    survivor: dict[str, str] = field(default_factory=dict)
    """Merged-away ``spec_id`` to its survivor's."""


def classify(
    diff: DiffArtifact,
    inference: InferenceReport | None,
    verification: VerificationReport | None,
    ontology: Ontology,
    *,
    registry: Mapping[str, SpecIdRow] | None = None,
) -> ChangeReport:
    """The build's ``ChangeReport`` (module docstring); ``registry``: the project's ``spec_id`` rows
    (``SpecIdRegistry.taken``), naming the nodes whose evidence only changed and, for an artifact
    without ``edge_changes``, the edge ends."""

    ids = _ids(diff, registry or {})
    nodes: dict[str, _Node] = {}
    _classify_operations(diff, ontology, ids, nodes)
    edges = _edges(diff, inference, verification, ids)
    removed = set(verification.removed_spec_ids) if verification is not None else set()
    for spec_id in removed:
        entry = nodes.setdefault(spec_id, _Node(label=ids.labels.get(spec_id, "")))
        entry.classes = {"removed"}
    for repair in verification.repairs if verification is not None else ():
        if repair.action == "set_label" and repair.subject not in removed:
            entry = nodes.setdefault(repair.subject, _Node(label=ids.labels.get(repair.subject, "")))
            entry.label = repair.value or entry.label
            entry.classes.add("changed_meaning")
            entry.causes.append(LABEL_CAUSE)
    for change in edges:
        _classify_edge(change, ontology, ids, nodes)
    for spec_id, entry in nodes.items():
        if not entry.label:
            entry.label = _label_of(spec_id, ontology)
    listed = [
        ChangedNode(
            spec_id=spec_id,
            label=entry.label,
            classes=tuple(name for name in CLASS_ORDER if name in entry.classes),
            causes=tuple(dict.fromkeys(entry.causes)),
            previous_spec_ids=tuple(sorted(set(entry.previous))),
        )
        for spec_id, entry in sorted(nodes.items())
        if entry.classes
    ]
    counts = Counter(name for node in listed for name in node.classes)
    edge_counts = Counter(change.change for change in edges)
    return ChangeReport(
        nodes=tuple(listed),
        edges=tuple(edges),
        counts=dict(sorted(counts.items())),
        edge_counts=dict(sorted(edge_counts.items())),
    )


# Naming


def _ids(diff: DiffArtifact, registry: Mapping[str, SpecIdRow]) -> _Ids:
    """Names from the registry, then the diff's rows and operations (which win)."""

    ids = _Ids()
    for row in sorted(registry.values(), key=lambda row: (row.state != ACTIVE, row.spec_id)):
        if row.state == ACTIVE:
            ids.by_uuid.setdefault(row.uuid, row.spec_id)
        ids.by_key.setdefault(row.identity_key, row.spec_id)
    for row in diff.registry_rows:
        if row.state == ACTIVE:
            ids.by_uuid[row.uuid] = row.spec_id
            ids.by_key[row.identity_key] = row.spec_id
        else:
            ids.by_key.setdefault(row.identity_key, row.spec_id)
    _operation_ids(diff, ids)
    return ids


def _operation_ids(diff: DiffArtifact, ids: _Ids) -> None:
    """Names the diff's node operations give: uuids, labels, identity keys (and aliases, a carried-over
    node's earlier key) and each merged-away ``spec_id``'s survivor."""

    for operation in diff.operations:
        if isinstance(operation, NodeCreate | NodeUpdate | NodeCarryOver):
            ids.by_uuid[operation.uuid] = operation.spec_id
            ids.labels[operation.spec_id] = operation.label
            aliases = operation.properties.get("identity_aliases")
            keys = [operation.properties.get("identity_key"), *(aliases if isinstance(aliases, list) else ())]
            if isinstance(operation, NodeCarryOver):
                keys.append(operation.from_identity_key)
            for key in keys:
                if isinstance(key, str):
                    ids.by_key.setdefault(key, operation.spec_id)
        elif isinstance(operation, NodeDelete):
            ids.by_uuid.setdefault(operation.uuid, operation.spec_id)
        elif isinstance(operation, NodeMerge):
            ids.by_uuid[operation.survivor_uuid] = operation.survivor_spec_id
            for merged_uuid, merged in zip(operation.merged_uuids, operation.merged_spec_ids, strict=False):
                ids.by_uuid.setdefault(merged_uuid, merged)
                ids.survivor[merged] = operation.survivor_spec_id


def _label_of(spec_id: str, ontology: Ontology) -> str:
    """The label whose ``spec_id`` prefix ``spec_id`` has (prefixes never nest); empty when none."""

    for label in ontology.labels:
        prefix = ontology.node_type(label).id_prefix
        if prefix and spec_id.startswith(prefix):
            return label
    return ""


# Nodes


def _classify_operations(diff: DiffArtifact, ontology: Ontology, ids: _Ids, nodes: dict[str, _Node]) -> None:
    """Classes and causes of the nodes the diff's operations create, delete, merge, carry over or update."""

    rekeyed = set(diff.spec_changes.rekeyed) - set(ids.survivor)
    linked = {operation.uuid for operation in diff.operations if isinstance(operation, NodeLinks)}
    merges = {op.survivor_spec_id: op for op in diff.operations if isinstance(op, NodeMerge)}
    updated: set[str] = set()
    for operation in diff.operations:
        if isinstance(operation, NodeCreate):
            nodes[operation.spec_id] = _Node(label=operation.label, classes={"added"})
        elif isinstance(operation, NodeDelete):
            nodes[operation.spec_id] = _Node(label=ids.labels.get(operation.spec_id, ""), classes={"removed"})
        elif isinstance(operation, NodeUpdate | NodeCarryOver):
            updated.add(operation.uuid)
            merge = merges.get(operation.spec_id)
            entry = nodes.setdefault(operation.spec_id, _Node(label=operation.label))
            causes = modified_causes(
                operation.properties, operation.vectors, linked=operation.uuid in linked, merged=merge is not None
            )
            entry.causes.extend(causes)
            entry.classes |= _property_classes(causes, ontology, operation.label)
            if isinstance(operation, NodeCarryOver):
                entry.classes.add("carried_over")
            elif operation.spec_id in rekeyed:
                entry.classes.add("rekeyed")
            if merge is not None:
                entry.classes.add("merged")
                entry.previous.extend(merge.merged_spec_ids)
    _merge_survivors(merges, ids, nodes)
    for uuid in sorted(linked - updated):
        spec_id = ids.by_uuid.get(uuid)
        if spec_id is None or spec_id in nodes:
            continue  # an added node's links, or a node the registry does not name
        nodes[spec_id] = _Node(label=ids.labels.get(spec_id, ""), classes={"changed_evidence"}, causes=[LINKS_CAUSE])


def _merge_survivors(merges: Mapping[str, NodeMerge], ids: _Ids, nodes: dict[str, _Node]) -> None:
    """``merged`` for each merge survivor the diff does not update (its own properties did not change),
    with the merged-away ``spec_id``s; a survivor already classified by its update is left as it is."""

    for survivor, merge in sorted(merges.items()):
        entry = nodes.setdefault(survivor, _Node(label=ids.labels.get(survivor, ""), causes=[MERGED_CAUSE]))
        if "merged" in entry.classes or entry.classes & {"added", "removed"}:
            continue  # updated (classified with its causes), or not a continuing node
        entry.classes.add("merged")
        entry.previous.extend(merge.merged_spec_ids)


def _property_classes(causes: Iterable[str], ontology: Ontology, label: str) -> set[str]:
    """``changed_meaning`` for a meaning property, ``changed_evidence`` for any other cause but the
    identity key and a merge. ``stmt`` is a meaning property only when the ``quote`` or the ``name``
    changed too (else the same source text paraphrased by a re-extraction)."""

    attributes = set(ontology.attribute_specs(label)) if ontology.is_declared(label) else set()
    meaning = (MEANING_PROPERTIES | attributes) - {QUOTE_PROPERTY}
    found = set(causes)
    if not found & {QUOTE_PROPERTY, NAME_PROPERTY}:
        meaning -= {STMT_PROPERTY}
    classes: set[str] = set()
    for cause in found:
        if cause in meaning:
            classes.add("changed_meaning")
        elif cause not in IDENTITY_PROPERTIES and cause != MERGED_CAUSE:
            classes.add("changed_evidence")
    return classes


def _classify_edge(change: ChangedEdge, ontology: Ontology, ids: _Ids, nodes: dict[str, _Node]) -> None:
    """The classes an edge change gives its ends (module docstring)."""

    parts = change.ref.split("|")
    if len(parts) != REF_PARTS:
        return
    source, relation, target = (ids.survivor.get(parts[0], parts[0]), parts[1], ids.survivor.get(parts[2], parts[2]))
    evidence_only = change.change == "changed" and set(change.causes) <= EDGE_EVIDENCE_CAUSES and bool(change.causes)
    owner = False
    if not evidence_only and ontology.is_declared(relation):
        source_label = _end_label(source, ids, nodes, ontology)
        target_label = _end_label(target, ids, nodes, ontology)
        pair = ontology.pair_rule(relation, source_label, target_label) if source_label and target_label else None
        owner = pair is not None and pair.owner
    for spec_id, role in ((source, "source"), (target, "target")):
        if not spec_id:
            continue
        entry = nodes.setdefault(spec_id, _Node(label=_end_label(spec_id, ids, nodes, ontology)))
        if entry.classes & {"added", "removed"}:
            continue
        if evidence_only:
            entry.classes.add("changed_evidence")
        elif owner and role == "source":
            entry.classes.add("changed_meaning")
        else:
            entry.classes.add("changed_links")


def _end_label(spec_id: str, ids: _Ids, nodes: Mapping[str, _Node], ontology: Ontology) -> str:
    known = nodes.get(spec_id)
    return (known.label if known is not None else "") or ids.labels.get(spec_id) or _label_of(spec_id, ontology)


# Edges


def _edges(
    diff: DiffArtifact, inference: InferenceReport | None, verification: VerificationReport | None, ids: _Ids
) -> list[ChangedEdge]:
    """The build's edge changes: the diff's, then inference's and the verification repairs', one per
    ``(ref, change)``, ordered by ``ref``."""

    found: dict[tuple[str, str], ChangedEdge] = {}
    diffed = diff.edge_changes if diff.edge_changes is not None else _diff_edges(diff, ids)
    for change in diffed:
        found.setdefault((change.ref, change.change), change)
    rules = _rules(inference)
    added = set(inference.inferred_added) if inference is not None else set()
    for ref in sorted(added):
        found[(ref, "added")] = ChangedEdge(ref=ref, change="added", basis=INFERRED, rule=rules.get(ref))
    for ref in sorted(inference.inferred_removed if inference is not None else ()):
        found.setdefault((ref, "removed"), ChangedEdge(ref=ref, change="removed", basis=INFERRED))
    if verification is not None:
        bases = {change.ref: change.basis for change in found.values()}
        gone = set(verification.removed_inferred_edges)
        deleted = {repair.subject for repair in verification.repairs if repair.action == "delete_edge"}
        for ref in sorted(gone | deleted):
            if ref in added:
                del found[(ref, "added")]  # added and removed by the same build: no change
                continue
            basis = _basis(INFERRED if ref in gone else bases.get(ref, STATED))
            removed = ChangedEdge(ref=ref, change="removed", basis=basis, rule=rules.get(ref) if ref in gone else None)
            found.setdefault((ref, "removed"), removed)
    return [found[key] for key in sorted(found)]


def _rules(inference: InferenceReport | None) -> dict[str, str]:
    """The rule of each inferred edge an accepted judged item names, by ``edge_ref``."""

    rules: dict[str, str] = {}
    for item in (inference.judged_items or ()) if inference is not None else ():
        if item.outcome in ACCEPTED_ITEMS and item.source_spec_id and item.target_spec_id:
            rules.setdefault(edge_ref(item.source_spec_id, item.relation, item.target_spec_id), item.rule)
    return rules


def _diff_edges(diff: DiffArtifact, ids: _Ids) -> list[ChangedEdge]:
    """``DiffArtifact.edge_changes`` rebuilt from the edge operations of an artifact written before it (module
    docstring); an edge whose ends the names do not resolve is left out.
    """

    inferred_removed = set(diff.spec_changes.inferred_removed)
    deleted = {operation.uuid: operation for operation in diff.operations if isinstance(operation, EdgeDelete)}
    moved: set[str] = set()
    changes: list[ChangedEdge] = []
    for operation in diff.operations:
        if isinstance(operation, EdgeCreate):
            ref = _uuid_ref(operation.relation, operation.source_uuid, operation.target_uuid, ids)
            basis, rule = operation.properties.get("basis"), operation.properties.get("inference_rule")
            kind: EdgeChangeKind = "added"
            causes: tuple[str, ...] = ()
            if operation.uuid in deleted:
                moved.add(operation.uuid)
                kind, causes = "changed", ("ends",)
            changes.extend(_change(ref, kind, basis, rule, causes))
        elif isinstance(operation, EdgeUpdate):
            causes = tuple(
                sorted(name for name in operation.properties if name not in EDGE_STAMPS | INFERRED_EDGE_PROPERTIES)
            )
            if causes:
                key = operation.properties.get("edge_key")
                ref = _key_ref(operation.relation, key if isinstance(key, str) else operation.edge_key, ids)
                changes.extend(_change(ref, "changed", operation.properties.get("basis"), None, causes))
    for uuid, operation in sorted(deleted.items()):
        if uuid in moved:
            continue
        ref = _key_ref(operation.relation, operation.edge_key, ids)
        basis = INFERRED if ref is not None and ref in inferred_removed else STATED
        changes.extend(_change(ref, "removed", basis, None, ()))
    return changes


def _change(
    ref: str | None, kind: EdgeChangeKind, basis: object, rule: object, causes: tuple[str, ...]
) -> list[ChangedEdge]:
    if ref is None:
        return []
    known = _basis(basis)
    named = rule if isinstance(rule, str) and known == INFERRED else None
    return [ChangedEdge(ref=ref, change=kind, basis=known, rule=named, causes=causes)]


def _basis(value: object) -> Basis:
    """A stored basis; ``stated`` when unknown."""

    if value == INFERRED:
        return "inferred"
    if value == IMPLIED:
        return "implied"
    return "stated"


def _uuid_ref(relation: str, source: str, target: str, ids: _Ids) -> str | None:
    first, second = ids.by_uuid.get(source), ids.by_uuid.get(target)
    return edge_ref(first, relation, second) if first and second else None


def _key_ref(relation: str, edge_key: str, ids: _Ids) -> str | None:
    """The ``edge_ref`` of an edge key ``<source key>|<RELATION>|<target key>`` through ``ids.by_key``."""

    source, separator, target = edge_key.partition(f"|{relation}|")
    if not separator:
        return None
    first, second = ids.by_key.get(source), ids.by_key.get(target)
    return edge_ref(first, relation, second) if first and second else None
