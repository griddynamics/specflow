"""Diff step: the operations that turn the current graph into the target graph (build.md step 6).

``load_current_graph`` reads the project's persisted spec graph; ``diff`` matches target nodes and
edges to it and emits persist operations in persist phase order, the build's spec changes and the
``spec_id`` registry rows. A ``first`` build diffs against the empty graph, so everything is created.

Node matching (build.md "Diff" table). A target matches every current node that shares its identity
key or one of its identity aliases (either side's). Each matched current node continues as its best
target: the one with its identity key, then one naming it in an alias, then the most shared evidence
lines, then the lowest identity key. A target chosen by one current node is an update in place; chosen
by several, a merge (the oldest node survives: lowest ``created_at``, then ``spec_id``); a target that
no current node chose is new (so a split leaves the node with its best match and creates the others).
A current node without any match is carried over to a new target of the same identity scope with an
alike name and overlapping evidence (``CARRY_OVER_JACCARD``, ``_overlap``), or failing that of the same
label with mostly the same evidence lines whatever the name (``_carry_over``), else deleted.

Edges match by edge key. A current edge's key is read with its ends' identity keys after this diff (a
merged node's end moves to the survivor), so an edge whose end was rekeyed, carried over or merged is
still the same edge: it is updated in place (its ``edge_key`` too) or, when an end moved to another
node, deleted and created again with its ``uuid``. Inferred edges are outside the diff: kept, moved and
rekeyed with their ends, deleted only with a deleted end (or when two collapse into one); one that a
target edge now states becomes ``stated`` in place and keeps its ``uuid``. A stated or implied edge
that no target edge has is deleted unless all its evidence is in fragments no call of this build
answered for (skipped, or unchanged in a unit whose call failed): then it is kept like an inferred one
(``_match_edges``, ``_unextracted``).

Skipped fragments (FR-BLD-12): a fragment whose extraction failed keeps its last extracted version as
support. Only the persisted graph still holds that version (the ``Fragment`` text is the new one), so
diff, not Conform, restores it: a continuing node keeps its persisted evidence lines, statement (and,
when all its support is skipped, its quote and identity key) for each skipped fragment, re-pointed from
the id the fragment replaces to the skipped fragment that carries its lineage; edges likewise keep
their ``fact`` (``_keep_skipped_support``). The replaced id retires as the plan says.

Node properties written by build (data-model.md "Spec node"): ``spec_id``, ``name``, ``identity_key``,
``identity_aliases``, the stored ontology attributes, ``quote``, ``statements`` (JSON),
``statement_text``, ``label_disagreement``, ``embedding``, ``name_embedding``, ``embedding_model`` and
the write stamps. An update sets only the build-owned properties whose value changed (``None`` removes
one) and never the ``created_*`` stamps (FR-CORE-05). A node whose attributes batch failed
(``attributes_missing``) keeps its persisted attribute values and quote. A statement whose text the node
already stated in that document keeps its ``revision_id``, ``version`` and ``at``. The two vectors are not in
``properties``: a node
operation's ``vectors`` names the ``text_hash`` of each embedded text, and Persist writes the vectors
from the vector store (``graph.build.vectors``); an update names a vector only when its text changed.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from graph.build.models import (
    DiffArtifact,
    DocumentRec,
    DocumentStatusChange,
    DocumentUpsert,
    EdgeCreate,
    EdgeDelete,
    EdgeUpdate,
    EvidenceLines,
    FragmentRec,
    FragmentStatusChange,
    FragmentUpsert,
    NodeCarryOver,
    NodeCreate,
    NodeDelete,
    NodeLinks,
    NodeMerge,
    NodeUpdate,
    NodeVectors,
    Operation,
    PlanArtifact,
    Properties,
    SpecChanges,
    StatementRec,
    TargetEdge,
    TargetGraph,
    TargetNode,
    VectorProperty,
    edge_ref,
)
from graph.build.queries import (
    READ_CURRENT_FRAGMENTS,
    READ_CURRENT_LINKS,
    READ_CURRENT_NODES,
    READ_PERSISTED_DOCUMENTS,
    read_current_edges,
)
from graph.build.resolve import embedding_text, fold_plural, hub_surface, hub_words, tokens
from graph.build.vectors import text_hash
from graph.core.db import Executor
from graph.core.errors import ConformanceError
from graph.core.identity import SpecIdRow, allocate_spec_id, edge_uuid, node_uuid, normalise_surface, slug
from graph.core.ontology import Ontology
from graph.core.queries import SPEC_NODE

DOCUMENT_SPEC_PREFIX: Final = "DOC::"
FRAGMENT_SPEC_PREFIX: Final = "FRG::"
QUOTE_PROPERTY: Final = "quote"
STMT_PROPERTY: Final = "stmt"
BUILD_OWNER: Final = "build"
INFERRED: Final = "inferred"
CARRY_OVER_JACCARD: Final = 0.5
"""Least name-token Jaccard of a carried-over node and its new target (tech-specs 5.4 "Diff carry-over")."""
# Build-owned spec node properties that an ontology attribute must not shadow.
RESERVED_PROPERTIES: Final = frozenset(
    {
        "group_id",
        "uuid",
        "spec_id",
        "spec_id_aliases",
        "name",
        "identity_key",
        "identity_aliases",
        "statements",
        "statement_text",
        "label_disagreement",
        "embedding",
        "name_embedding",
        "embedding_model",
        "created_at",
        "updated_at",
        "created_build_id",
        "last_build_id",
    }
)
# What an update never sets: keys, creation stamps, vectors (``vectors``), aliases of merged nodes
# (set by the merge), and the write stamps (added when anything else changes).
_FIXED_PROPERTIES: Final = frozenset(
    {
        "group_id",
        "uuid",
        "spec_id",
        "spec_id_aliases",
        "embedding",
        "name_embedding",
        "created_at",
        "created_build_id",
        "updated_at",
        "last_build_id",
    }
)
# Properties of inferred edges only (data-model.md "Edges"); removed when the edge becomes stated.
INFERRED_EDGE_PROPERTIES: Final = ("inference_rule", "confidence", "evidence", "judge_model", "inferred_at")


# Current graph


@dataclass(frozen=True, slots=True)
class CurrentEvidence:
    """One ``REFERENCED_BY`` of a persisted node: its fragment, lines and the text of those lines."""

    fragment_id: str
    document_id: str
    line_start: int
    line_end: int
    lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CurrentNode:
    """A persisted spec node as diff sees it: keys, stamps, non-vector properties and links."""

    uuid: str
    spec_id: str
    label: str
    identity_key: str
    identity_aliases: tuple[str, ...] = ()
    name: str = ""
    spec_id_aliases: tuple[str, ...] = ()
    created_at: str = ""
    properties: Mapping[str, Any] = field(default_factory=dict)
    """Every property but the two vectors (foreign ones included; diff only compares its own)."""
    evidence: tuple[CurrentEvidence, ...] = ()
    mentions: Mapping[str, int] = field(default_factory=dict)

    @property
    def scope(self) -> str:
        return _scope(self.identity_key)

    def statements(self) -> list[dict[str, Any]]:
        raw = self.properties.get("statements")
        try:
            value = json.loads(raw) if isinstance(raw, str) else []
        except ValueError:
            return []
        return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


@dataclass(frozen=True, slots=True)
class CurrentEdge:
    """A persisted domain edge as diff sees it (inferred ones included)."""

    uuid: str
    relation: str
    edge_key: str
    basis: str
    source_uuid: str
    target_uuid: str
    source_key: str = ""
    target_key: str = ""
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CurrentFragment:
    """A persisted fragment: the properties a ``FragmentUpsert`` writes, but its text."""

    fragment_id: str
    document_id: str
    status: str | None = None
    content_hash: str | None = None
    extracted_hash: str | None = None
    error_code: str | None = None
    spec_id: str | None = None
    heading_path: tuple[str, ...] = ()
    ordinal: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class CurrentGraph:
    """The project's persisted graph; empty for a ``first`` build."""

    nodes: tuple[CurrentNode, ...] = field(default=())
    edges: tuple[CurrentEdge, ...] = field(default=())
    documents: tuple[DocumentRec, ...] = field(default=())
    fragments: tuple[CurrentFragment, ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not self.nodes and not self.edges


EMPTY_GRAPH: Final = CurrentGraph()


async def load_current_graph(
    executor: Executor, project_id: str, ontology: Ontology, *, edges: bool = True
) -> CurrentGraph:
    """Every spec node (with its evidence and mentions), every edge of each build-owned relation,
    every document and fragment of ``project_id``; nodes by uuid, edges by relation then key.
    ``edges`` False reads no edge and no document (``estimate_build`` needs nodes and fragments only)."""

    links: dict[str, tuple[list[CurrentEvidence], dict[str, int]]] = {}
    for row in await executor.read(READ_CURRENT_LINKS, project_id):
        evidence, mentions = links.setdefault(row["uuid"], ([], {}))
        if row["kind"] == "MENTIONED_IN":
            mentions[row["fragment_id"]] = int(row["count"] or 0)
        elif row["line_start"] is not None and row["line_end"] is not None:
            evidence.append(
                CurrentEvidence(
                    fragment_id=row["fragment_id"],
                    document_id=row["document_id"],
                    line_start=int(row["line_start"]),
                    line_end=int(row["line_end"]),
                    lines=tuple(row["lines"] or ()),
                )
            )
    nodes = []
    for row in await executor.read(READ_CURRENT_NODES, project_id):
        props = {name: value for name, value in row["props"].items() if value is not None}
        evidence, mentions = links.get(row["uuid"], ([], {}))
        nodes.append(
            CurrentNode(
                uuid=row["uuid"],
                spec_id=str(props.get("spec_id", "")),
                label=_domain_label(row["labels"], ontology),
                identity_key=str(props.get("identity_key", "")),
                identity_aliases=tuple(props.get("identity_aliases") or ()),
                name=str(props.get("name", "")),
                spec_id_aliases=tuple(props.get("spec_id_aliases") or ()),
                created_at=str(props.get("created_at", "")),
                properties=props,
                evidence=tuple(evidence),
                mentions=dict(sorted(mentions.items())),
            )
        )
    current_edges = []
    relations = sorted(name for name in ontology.relations if ontology.owner_of(name) == BUILD_OWNER)
    for relation in relations if edges else ():
        for row in await executor.read(read_current_edges(relation), project_id):
            props = dict(row["props"])
            current_edges.append(
                CurrentEdge(
                    uuid=str(props.get("uuid") or ""),
                    relation=relation,
                    edge_key=row["edge_key"],
                    basis=str(props.get("basis") or ""),
                    source_uuid=row["source_uuid"],
                    target_uuid=row["target_uuid"],
                    source_key=row["source_key"] or "",
                    target_key=row["target_key"] or "",
                    properties=props,
                )
            )
    documents = (
        tuple(DocumentRec.model_validate(row) for row in await executor.read(READ_PERSISTED_DOCUMENTS, project_id))
        if edges
        else ()
    )
    fragments = tuple(
        CurrentFragment(
            fragment_id=row["fragment_id"],
            document_id=row["document_id"] or "",
            status=row["status"],
            content_hash=row["content_hash"],
            extracted_hash=row["extracted_hash"],
            error_code=row["error_code"],
            spec_id=row["spec_id"],
            heading_path=tuple(row["heading_path"] or ()),
            ordinal=row["ordinal"],
            line_start=row["line_start"],
            line_end=row["line_end"],
        )
        for row in await executor.read(READ_CURRENT_FRAGMENTS, project_id)
    )
    return CurrentGraph(
        nodes=tuple(sorted(nodes, key=lambda node: node.uuid)),
        edges=tuple(current_edges),
        documents=documents,
        fragments=fragments,
    )


def _domain_label(labels: Sequence[str], ontology: Ontology) -> str:
    """The node's one domain label (D1); with several, the first build-owned one."""

    domain = sorted(label for label in labels if label != SPEC_NODE)
    owned = [label for label in domain if ontology.owner_of(label) == BUILD_OWNER]
    return (owned or domain or [""])[0]


# Diff


@dataclass(frozen=True, slots=True)
class DiffInput:
    """Everything ``diff`` reads besides the plan and the target graph."""

    project_id: str
    build_id: str
    embedding_model: str
    at: str
    taken: Mapping[str, SpecIdRow]
    skipped: Mapping[str, str]
    current: CurrentGraph = EMPTY_GRAPH
    edge_failed_fragments: tuple[str, ...] = ()
    """Fragments of the units whose edge call failed without a stored fallback, clean ones included."""


@dataclass(slots=True)
class _Placed:
    """A target node with its uuid and ``spec_id``; ``current``: the node it continues (None: created)."""

    node: TargetNode
    uuid: str = ""
    spec_id: str = ""
    current: CurrentNode | None = None
    merged: list[CurrentNode] = field(default_factory=list)
    carried: bool = False


@dataclass(slots=True)
class _NodeMatch:
    placed: list[_Placed]
    """Every target node, in identity key order."""
    deleted: list[CurrentNode]
    ends: dict[str, tuple[str, str]]
    """Current uuid to the ``(uuid, identity key)`` it continues as after the diff; deleted ones absent."""


@dataclass(slots=True)
class _Operations:
    """What ``diff`` collects while it walks the matched nodes: operations by phase, registry rows,
    spec changes by kind and the texts of changed vectors."""

    changes: dict[str, list[str]] = field(
        default_factory=lambda: {name: [] for name in ("added", "removed", "modified", "rekeyed", "carried_over")}
    )
    rows: list[SpecIdRow] = field(default_factory=list)
    merges: list[Operation] = field(default_factory=list)
    updates: list[Operation] = field(default_factory=list)
    creates: list[Operation] = field(default_factory=list)
    links: list[Operation] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)


def _add_created(out: _Operations, placed: _Placed, given: DiffInput, target_keys: Collection[str]) -> None:
    """The operations of a target node that continues no current node."""

    node = placed.node
    properties = _node_properties(node, placed.spec_id, given)
    # An alias naming another target's key would match that node too (a split-off target).
    properties["identity_aliases"] = [key for key in node.identity_aliases if key not in target_keys]
    if node.label_disagreement:
        # As an update would set it: a later build that continues the node is no change.
        properties["label_disagreement"] = node.label_disagreement
    out.creates.append(
        NodeCreate(
            uuid=placed.uuid,
            spec_id=placed.spec_id,
            label=node.label,
            properties=properties,
            vectors=node_vectors(node),
        )
    )
    out.links.append(NodeLinks(uuid=placed.uuid, evidence=node.evidence, mentions=dict(sorted(node.mentions.items()))))
    out.changes["added"].append(placed.spec_id)
    out.rows.append(SpecIdRow(spec_id=placed.spec_id, identity_key=node.identity_key, uuid=placed.uuid, state="active"))


def _add_continued(
    out: _Operations,
    placed: _Placed,
    current: CurrentNode,
    ontology: Ontology,
    given: DiffInput,
    *,
    target_keys: Collection[str],
) -> None:
    """The operations of a target node that continues ``current`` (merge, update or carry-over, links)."""

    node = placed.node
    aliases = _spec_id_aliases(placed, current)
    if placed.merged:
        out.merges.append(
            NodeMerge(
                survivor_uuid=placed.uuid,
                survivor_spec_id=placed.spec_id,
                merged_uuids=tuple(loser.uuid for loser in placed.merged),
                merged_spec_ids=tuple(loser.spec_id for loser in placed.merged),
                aliases=aliases,
            )
        )
    properties, vectors = _update(placed, current, ontology, given, target_keys=target_keys, texts=out.texts)
    if placed.carried:
        out.updates.append(
            NodeCarryOver(
                uuid=placed.uuid,
                spec_id=placed.spec_id,
                label=current.label,
                from_identity_key=current.identity_key,
                properties=properties,
                vectors=vectors,
            )
        )
    elif properties or vectors:
        out.updates.append(
            NodeUpdate(
                uuid=placed.uuid,
                spec_id=placed.spec_id,
                label=current.label,
                properties=properties,
                vectors=vectors,
            )
        )
    linked = _links_changed(current, node)
    if linked:
        out.links.append(
            NodeLinks(uuid=placed.uuid, evidence=node.evidence, mentions=dict(sorted(node.mentions.items())))
        )
    rekeyed = node.identity_key != current.identity_key
    if placed.carried:
        out.changes["carried_over"].append(placed.spec_id)
    elif rekeyed:
        out.changes["rekeyed"].append(placed.spec_id)
    elif properties or vectors or linked or placed.merged:
        out.changes["modified"].append(placed.spec_id)
    out.rows.append(SpecIdRow(spec_id=placed.spec_id, identity_key=node.identity_key, uuid=placed.uuid, state="active"))
    for loser in placed.merged:
        out.changes["rekeyed"].append(loser.spec_id)
        out.rows.append(
            SpecIdRow(spec_id=loser.spec_id, identity_key=loser.identity_key, uuid=placed.uuid, state="alias")
        )
    for alias in aliases:
        known = given.taken.get(alias)
        key = known.identity_key if known else node.identity_key
        out.rows.append(SpecIdRow(spec_id=alias, identity_key=key, uuid=placed.uuid, state="alias"))


def _add_deleted(out: _Operations, node: CurrentNode, given: DiffInput) -> Operation:
    """The registry rows and change of a current node no target continues; returns its delete operation."""

    out.changes["removed"].append(node.spec_id)
    out.rows.append(SpecIdRow(spec_id=node.spec_id, identity_key=node.identity_key, uuid=node.uuid, state="retired"))
    for alias in node.spec_id_aliases:
        known = given.taken.get(alias)
        key = known.identity_key if known else node.identity_key
        out.rows.append(SpecIdRow(spec_id=alias, identity_key=key, uuid=node.uuid, state="retired"))
    return NodeDelete(uuid=node.uuid, spec_id=node.spec_id)


def diff(plan: PlanArtifact, target: TargetGraph, ontology: Ontology, given: DiffInput) -> DiffArtifact:
    """The diff from ``given.current`` to ``target`` (build.md step 6).

    ``taken`` is the ``spec_id`` registry of the project; ``skipped`` maps fragments whose extraction
    failed to their error code; ``at`` stamps the written nodes and edges. Deterministic: operations
    are ordered by phase, then by identity key, edge key or document order.
    """

    fragments = {fragment.fragment_id: fragment for fragment in plan.fragments}
    match = _match_nodes(target.nodes, ontology, given, fragments)
    skipped = _skipped_lineage(plan, given)
    rename = _keep_skipped_support(match, skipped)
    target_keys = {placed.node.identity_key for placed in match.placed}
    out = _Operations()
    for placed in match.placed:
        if placed.current is None:
            _add_created(out, placed, given, target_keys)
        else:
            _add_continued(out, placed, placed.current, ontology, given, target_keys=target_keys)
    deletes = [_add_deleted(out, node, given) for node in match.deleted]

    uuids = {placed.node.identity_key: placed.uuid for placed in match.placed}
    edges = _renamed_edges(target.edges, rename, ontology)
    removed_edges, edge_writes, inferred_removed, edges_after = _match_edges(
        edges, uuids, match.ends, ontology, given, skipped=skipped, unextracted=_unextracted(plan, given)
    )
    nodes_after = Counter(
        placed.current.label if placed.current is not None else placed.node.label for placed in match.placed
    )

    operations: list[Operation] = [
        *removed_edges,
        *out.merges,
        *out.updates,
        *deletes,
        *out.creates,
        *edge_writes,
        *out.links,
        *_documents(plan, given),
    ]
    return DiffArtifact(
        operations=tuple(operations),
        spec_changes=SpecChanges(
            **{name: tuple(sorted(set(values))) for name, values in out.changes.items()},
            inferred_removed=tuple(sorted(set(inferred_removed))),
        ),
        registry_rows=tuple(row for row in _dedup_rows(out.rows) if given.taken.get(row.spec_id) != row),
        texts=dict(sorted(out.texts.items())),
        nodes_by_label=dict(sorted(nodes_after.items())),
        edges_by_relation=dict(sorted(edges_after.items())),
    )


def diff_summary(artifact: DiffArtifact) -> dict[str, int]:
    """Counts for the ``graph.build.diffed`` event: ``operations``, each operation kind (``node_create``,
    ``edge_delete``...; kinds absent are left out), the size of each ``spec_changes`` list and
    ``registry_rows``."""

    summary: dict[str, int] = {"operations": len(artifact.operations)}
    for operation in artifact.operations:
        summary[operation.op] = summary.get(operation.op, 0) + 1
    summary.update({name: len(values) for name, values in artifact.spec_changes.model_dump().items()})
    summary["registry_rows"] = len(artifact.registry_rows)
    return summary


def _dedup_rows(rows: Iterable[SpecIdRow]) -> list[SpecIdRow]:
    """One row per ``spec_id`` (the first one given), ordered by ``spec_id``."""

    first: dict[str, SpecIdRow] = {}
    for row in rows:
        first.setdefault(row.spec_id, row)
    return [first[spec_id] for spec_id in sorted(first)]


# Nodes


def _choose_survivors(
    current: Sequence[CurrentNode], targets: Sequence[TargetNode], fragments: Mapping[str, FragmentRec]
) -> tuple[dict[str, list[CurrentNode]], list[CurrentNode]]:
    """Each current node assigned to the target it best continues (``chosen``, by the target's identity
    key) or, when no target names its key or an alias, left an orphan."""

    by_key: dict[str, list[CurrentNode]] = {}
    for node in current:
        for key in dict.fromkeys((node.identity_key, *node.identity_aliases)):
            by_key.setdefault(key, []).append(node)
    candidates: dict[str, list[TargetNode]] = {}
    for target_node in targets:
        for key in dict.fromkeys((target_node.identity_key, *target_node.identity_aliases)):
            for match in by_key.get(key, ()):
                found = candidates.setdefault(match.uuid, [])
                if target_node not in found:
                    found.append(target_node)

    chosen: dict[str, list[CurrentNode]] = {}
    orphans: list[CurrentNode] = []
    for node in current:
        options = candidates.get(node.uuid)
        if not options:
            orphans.append(node)
            continue
        best = max(options, key=lambda option: _rank(node, option, fragments))
        chosen.setdefault(best.identity_key, []).append(node)
    return chosen, orphans


def _match_nodes(
    nodes: Sequence[TargetNode], ontology: Ontology, given: DiffInput, fragments: Mapping[str, FragmentRec]
) -> _NodeMatch:
    """Every target placed (continuing a current node, or created) and the current nodes deleted."""

    current = sorted(given.current.nodes, key=lambda node: (node.identity_key, node.uuid))
    targets = sorted(nodes, key=lambda node: node.identity_key)
    chosen, orphans = _choose_survivors(current, targets, fragments)

    placed: list[_Placed] = []
    fresh: list[_Placed] = []
    for node in targets:
        members = sorted(chosen.get(node.identity_key, ()), key=lambda member: (member.created_at, member.spec_id))
        if members:
            survivor = members[0]
            placed.append(
                _Placed(node=node, uuid=survivor.uuid, spec_id=survivor.spec_id, current=survivor, merged=members[1:])
            )
        else:
            entry = _Placed(node=node)
            placed.append(entry)
            fresh.append(entry)

    deleted = _carry_over(orphans, fresh, ontology, fragments)
    _allocate(placed, ontology, given)
    ends: dict[str, tuple[str, str]] = {}
    for entry in placed:
        if entry.current is None:
            continue
        for member in (entry.current, *entry.merged):
            ends[member.uuid] = (entry.uuid, entry.node.identity_key)
    return _NodeMatch(placed=placed, deleted=deleted, ends=ends)


def _rank(current: CurrentNode, node: TargetNode, fragments: Mapping[str, FragmentRec]) -> tuple[bool, bool, int]:
    """How well ``node`` continues ``current``: same identity key, an alias naming the other's key, shared
    evidence lines. Candidates come in identity key order and ``max`` keeps the first of equal ranks, so
    ties go to the lowest identity key."""

    named = node.identity_key in current.identity_aliases or current.identity_key in node.identity_aliases
    return (node.identity_key == current.identity_key, named, _overlap(current, node, fragments))


def _carry_over(
    orphans: Sequence[CurrentNode], fresh: Sequence[_Placed], ontology: Ontology, fragments: Mapping[str, FragmentRec]
) -> list[CurrentNode]:
    """Hand orphans to new targets of their identity scope, one to one; returns the orphans left (deleted).

    1. Alike name: name-token Jaccard (``_name_tokens``) at least ``CARRY_OVER_JACCARD`` and at least
       one shared evidence line in one document (``_overlap``); best pairs first (Jaccard, then shared
       lines, then identity keys).
    2. Same evidence, for the orphans and targets left: the same label (never across labels; hubs
       excluded, as a hub's fallback line is shared by every hub of a fragment) and an evidence-line
       Jaccard (``_evidence_texts``: normalised line text per document) at least ``CARRY_OVER_JACCARD``,
       whatever the name (a re-extracted node whose name and span key drifted); best pairs first
       (evidence Jaccard, then name Jaccard, then identity keys).

    Candidate pairs come from inverted indexes of the targets (name tokens per scope, evidence lines
    per scope and label), so only pairs sharing a token or a line are scored.
    """

    taken: set[str] = set()
    _assign(_name_pairs(orphans, fresh, ontology, fragments), taken)
    left = [orphan for orphan in orphans if orphan.uuid not in taken]
    _assign(_evidence_pairs(left, [entry for entry in fresh if entry.current is None], ontology, fragments), taken)
    return [orphan for orphan in orphans if orphan.uuid not in taken]


_Pair = tuple[tuple[float, float, str, str], CurrentNode, _Placed]


def _assign(pairs: list[_Pair], taken: set[str]) -> None:
    """Greedy one-to-one assignment of ``pairs`` by rank (lowest first)."""

    for _, orphan, entry in sorted(pairs, key=lambda pair: pair[0]):
        if orphan.uuid in taken or entry.current is not None:
            continue
        taken.add(orphan.uuid)
        entry.current, entry.uuid, entry.spec_id, entry.carried = orphan, orphan.uuid, orphan.spec_id, True


def _name_pairs(
    orphans: Sequence[CurrentNode], fresh: Sequence[_Placed], ontology: Ontology, fragments: Mapping[str, FragmentRec]
) -> list[_Pair]:
    indexes: dict[bool, dict[tuple[str, str], list[int]]] = {}
    words: dict[bool, list[frozenset[str]]] = {}
    pairs: list[_Pair] = []
    for orphan in orphans:
        hub = orphan.label in ontology.hub_labels
        if hub not in indexes:
            words[hub] = [_name_tokens(entry.node.name, hub=hub) for entry in fresh]
            indexes[hub] = _inverted(
                ((_scope(entry.node.identity_key), token) for token in words[hub][index])
                for index, entry in enumerate(fresh)
            )
        mine = _name_tokens(orphan.name, hub=hub)
        for index in _candidates(indexes[hub], ((orphan.scope, token) for token in mine)):
            score = _jaccard(mine, words[hub][index])
            if score < CARRY_OVER_JACCARD:
                continue
            entry = fresh[index]
            shared = _overlap(orphan, entry.node, fragments)
            if shared:
                pairs.append(((-score, -shared, orphan.identity_key, entry.node.identity_key), orphan, entry))
    return pairs


def _evidence_pairs(
    orphans: Sequence[CurrentNode], fresh: Sequence[_Placed], ontology: Ontology, fragments: Mapping[str, FragmentRec]
) -> list[_Pair]:
    eligible = [entry for entry in fresh if entry.node.label not in ontology.hub_labels]
    texts = [_evidence_texts(entry.node, fragments) for entry in eligible]
    index = _inverted(
        ((_scope(entry.node.identity_key), entry.node.label, line) for line in texts[position])
        for position, entry in enumerate(eligible)
    )
    pairs: list[_Pair] = []
    for orphan in orphans:
        if orphan.label in ontology.hub_labels:
            continue
        mine = frozenset(
            (e.document_id, text) for e in orphan.evidence for text in map(normalise_surface, e.lines) if text
        )
        for position in _candidates(index, ((orphan.scope, orphan.label, line) for line in mine)):
            score = _jaccard(mine, texts[position])
            if score < CARRY_OVER_JACCARD:
                continue
            entry = eligible[position]
            name = _jaccard(_name_tokens(orphan.name, hub=False), _name_tokens(entry.node.name, hub=False))
            pairs.append(((-score, -name, orphan.identity_key, entry.node.identity_key), orphan, entry))
    return pairs


def _inverted(keys_per_item: Iterable[Iterable[Any]]) -> dict[Any, list[int]]:
    """Item positions by each key the item has."""

    index: dict[Any, list[int]] = {}
    for position, keys in enumerate(keys_per_item):
        for key in dict.fromkeys(keys):
            index.setdefault(key, []).append(position)
    return index


def _candidates(index: Mapping[Any, list[int]], keys: Iterable[Any]) -> list[int]:
    """Positions of the items sharing at least one of ``keys``, ascending."""

    return sorted({position for key in keys for position in index.get(key, ())})


def _evidence_texts(node: TargetNode, fragments: Mapping[str, FragmentRec]) -> frozenset[tuple[str, str]]:
    """The node's evidence lines as ``(document_id, normalised line text)``, empty lines left out."""

    found: set[tuple[str, str]] = set()
    for evidence in node.evidence:
        fragment = fragments.get(evidence.fragment_id)
        if fragment is None:
            continue
        lines = fragment.text.split("\n")
        for line in range(evidence.line_start, evidence.line_end + 1):
            index = line - fragment.line_start
            text = normalise_surface(lines[index]) if 0 <= index < len(lines) else ""
            if text:
                found.add((fragment.document_id, text))
    return frozenset(found)


def _name_tokens(name: str, *, hub: bool) -> frozenset[str]:
    """Name tokens for carry-over: hub names folded as their identity value is (``hub_surface``), other
    names normalised with each token's plural folded (``fold_plural``), so a plural rename still matches."""

    if hub:
        return frozenset(hub_words(hub_surface(name)))
    return frozenset(fold_plural(token) for token in tokens(normalise_surface(name)))


def _jaccard[T](first: frozenset[T], second: frozenset[T]) -> float:
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def _scope(key: str) -> str:
    return key.split("|", 1)[0]


def _overlap(current: CurrentNode, node: TargetNode, fragments: Mapping[str, FragmentRec]) -> int:
    """Evidence lines of ``node`` that ``current`` also had in the same document: the same line number,
    or the same (normalised, non-empty) line text, so a line moved by an edit above it still counts."""

    numbers = {(e.document_id, line) for e in current.evidence for line in range(e.line_start, e.line_end + 1)}
    texts = {(e.document_id, text) for e in current.evidence for text in map(normalise_surface, e.lines) if text}
    if not numbers:
        return 0
    shared = 0
    for evidence in node.evidence:
        fragment = fragments.get(evidence.fragment_id)
        if fragment is None:
            continue
        lines = fragment.text.split("\n")
        for line in range(evidence.line_start, evidence.line_end + 1):
            index = line - fragment.line_start
            text = normalise_surface(lines[index]) if 0 <= index < len(lines) else ""
            if (fragment.document_id, line) in numbers or (text and (fragment.document_id, text) in texts):
                shared += 1
    return shared


def _allocate(placed: Sequence[_Placed], ontology: Ontology, given: DiffInput) -> None:
    """A uuid and ``spec_id`` for each created target, in identity key order.

    A ``spec_id`` already issued for the node's identity key, else for its uuid (a retried build whose
    earlier run recorded its rows) when that row's identity key is the node's key or one of its
    identity aliases, is reused even when the name or label now gives another slug, so a node's
    ``spec_id`` never changes (FR-CORE-06); only a node without one is allocated a new one. A uuid row of
    another key is never taken: a node created under an old key after its node was carried over to
    another key (the row now names the new key) and deleted gets a new ``spec_id``. A
    ``spec_id`` (or alias) of any current node is never given to a created one, and a uuid of a current
    node is never reused: a clash (a split-off target whose key a current node was created with) takes
    the uuid of ``<identity key>|2``, ``|3``...
    """

    taken = dict(given.taken)
    by_key, by_uuid = _issued(given.taken.values())
    used = {spec_id for node in given.current.nodes for spec_id in (node.spec_id, *node.spec_id_aliases)}
    uuids = {node.uuid for node in given.current.nodes}
    for entry in placed:
        if entry.current is not None:
            continue
        node = entry.node
        uuid = node_uuid(given.project_id, node.identity_key)
        number = 1
        while uuid in uuids:
            number += 1
            uuid = node_uuid(given.project_id, f"{node.identity_key}|{number}")
        explicit = node.explicit_ids[0] if node.explicit_ids else None
        keys = {node.identity_key, *node.identity_aliases}
        same_uuid = next((row.spec_id for row in by_uuid.get(uuid, ()) if row.identity_key in keys), None)
        candidates = (by_key.get(node.identity_key), same_uuid)
        spec_id = next((issued for issued in candidates if issued and issued not in used), None)
        if spec_id is None:
            spec_id = allocate_spec_id(
                ontology.id_prefix(node.label), slug(node.name, explicit), node.identity_key, taken
            )
            while spec_id in used:  # an id held by a current node under a stale registry row
                taken[spec_id] = SpecIdRow(spec_id=spec_id, identity_key="", uuid="", state="alias")
                spec_id = allocate_spec_id(
                    ontology.id_prefix(node.label), slug(node.name, explicit), node.identity_key, taken
                )
        used.add(spec_id)
        uuids.add(uuid)
        taken[spec_id] = SpecIdRow(spec_id=spec_id, identity_key=node.identity_key, uuid=uuid, state="active")
        entry.uuid, entry.spec_id = uuid, spec_id


def _issued(rows: Iterable[SpecIdRow]) -> tuple[dict[str, str], dict[str, list[SpecIdRow]]]:
    """The ``spec_id`` issued to each identity key, and the rows issued to each uuid (aliases left
    out): an ``active`` row before a ``retired`` one, then the lowest ``spec_id``."""

    by_key: dict[str, str] = {}
    by_uuid: dict[str, list[SpecIdRow]] = {}
    issued = (row for row in rows if row.state != "alias")
    ranked = sorted(issued, key=lambda row: (row.state != "active", row.spec_id))
    for row in ranked:
        by_key.setdefault(row.identity_key, row.spec_id)
        by_uuid.setdefault(row.uuid, []).append(row)
    return by_key, by_uuid


def _spec_id_aliases(placed: _Placed, current: CurrentNode) -> tuple[str, ...]:
    """The continuing node's ``spec_id_aliases``: its own, plus each merged node's ``spec_id`` and aliases."""

    ids = [*current.spec_id_aliases]
    for loser in placed.merged:
        ids.extend((loser.spec_id, *loser.spec_id_aliases))
    return tuple(sorted(set(ids) - {placed.spec_id}))


def _desired_properties(
    placed: _Placed, current: CurrentNode, ontology: Ontology, given: DiffInput, target_keys: Collection[str]
) -> tuple[Properties, set[str]]:
    """The properties a continuing node should have (build-owned, fixed ones left out) and the attribute
    names of its persisted and extracted labels."""

    node = placed.node
    desired = _node_properties(node, placed.spec_id, given)
    for name in _FIXED_PROPERTIES:
        desired.pop(name, None)
    attribute_names = {*_attribute_names(ontology, current.label), *_attribute_names(ontology, node.label)}
    if node.attributes_missing:
        # The attributes batch failed: this build has no values, so the persisted ones stay.
        for name in sorted(attribute_names - set(node.attributes) - RESERVED_PROPERTIES):
            if name in current.properties:
                desired[name] = _stored(current.properties[name])
        if current.properties.get(QUOTE_PROPERTY) is not None:
            desired[QUOTE_PROPERTY] = current.properties[QUOTE_PROPERTY]
    earlier = {current.identity_key, *current.identity_aliases}
    for loser in placed.merged:
        earlier.update((loser.identity_key, *loser.identity_aliases))
    aliases = (set(node.identity_aliases) | earlier) - set(target_keys) - {node.identity_key}
    desired["identity_aliases"] = [*sorted(aliases)]
    desired["label_disagreement"] = node.label_disagreement or (node.label if node.label != current.label else None)
    desired["statements"] = _statements_json(node, current)
    return desired, attribute_names


def _changed_vectors(
    node: TargetNode,
    current: CurrentNode,
    desired: Mapping[str, Any],
    given: DiffInput,
    texts: dict[str, str] | None,
) -> NodeVectors:
    """The vectors whose text changed (name to text hash); a text that is not the target's own is added to
    ``texts`` (by hash) for Persist."""

    vectors: NodeVectors = {}
    same_model = current.properties.get("embedding_model") == given.embedding_model
    own = {node.embedding_text, node.name_embedding_text}
    for name, text in continuing_texts(node, current.label).items():
        digest = text_hash(text)
        if same_model and digest == _current_vector_hash(current, name):
            continue
        if same_model and name == "embedding" and node.attributes_missing and _kept_embedding(node, current, desired):
            continue  # its text is the kept ``stmt``'s, which the target's text lacks
        vectors[name] = digest
        if texts is not None and text not in own:
            texts[digest] = text
    return vectors


def _update(
    placed: _Placed,
    current: CurrentNode,
    ontology: Ontology,
    given: DiffInput,
    *,
    target_keys: Collection[str],
    texts: dict[str, str] | None = None,
) -> tuple[Properties, NodeVectors]:
    """The build-owned properties of a continuing node whose value changes, and the vectors whose text
    changes; empty when nothing does. A property the target no longer has is removed (``None``).

    The node keeps its persisted label, so its vectors are those of its texts under that label
    (``continuing_texts``), the texts the next build's Conform gives it; such a text that is not the
    target's own is added to ``texts`` (by hash) for Persist."""

    desired, attribute_names = _desired_properties(placed, current, ontology, given, target_keys)
    owned = (RESERVED_PROPERTIES - _FIXED_PROPERTIES) | {QUOTE_PROPERTY} | attribute_names
    properties: Properties = {
        name: value for name, value in sorted(desired.items()) if _stored(current.properties.get(name)) != value
    }
    properties.update({name: None for name in sorted(owned) if name in current.properties and name not in desired})
    vectors = _changed_vectors(placed.node, current, desired, given, texts)
    if properties or vectors or placed.merged:
        properties["updated_at"] = given.at
        properties["last_build_id"] = given.build_id
    if not properties and not vectors:
        return {}, {}
    return properties, vectors


def _kept_embedding(node: TargetNode, current: CurrentNode, desired: Mapping[str, Any]) -> bool:
    """Whether the current search vector still fits a node that keeps its persisted ``stmt`` (its
    attributes batch failed): the text built from the kept ``stmt`` is the one it was computed from."""

    stmt = desired.get(STMT_PROPERTY)
    if not stmt:
        return False
    return text_hash(f"{embedding_text(current.label, node.name)}. {stmt}") == _current_vector_hash(
        current, "embedding"
    )


def _attribute_names(ontology: Ontology, label: str) -> tuple[str, ...]:
    if not ontology.is_declared(label):
        return ()
    return tuple(ontology.attribute_specs(label))


def _stored(value: Any) -> Any:
    """A property value as diff compares it (Neo4j returns lists for arrays)."""

    return list(value) if isinstance(value, tuple) else value


_STATEMENT_KEPT: Final = (("revision_id", str), ("version", int), ("at", str))
"""Statement fields an unchanged statement keeps from its persisted entry."""


def _statements_json(node: TargetNode, current: CurrentNode) -> str:
    """The node's statements; one whose text the node already stated in that document keeps the
    ``revision_id``, ``version`` and ``at`` it was stated at, so a statement a revision leaves as it was
    is no change (an unchanged node of a revised document is not ``modified``)."""

    stated: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in current.statements():
        stated.setdefault((entry.get("document_id"), entry.get("text")), entry)
    entries = []
    for statement in node.statements:
        entry = statement.model_dump(mode="json")
        earlier = stated.get((statement.document_id, statement.text), {})
        for name, kind in _STATEMENT_KEPT:
            value = earlier.get(name)
            if isinstance(value, kind) and not isinstance(value, bool):
                entry[name] = value
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False)


def _current_vector_hash(current: CurrentNode, name: str) -> str | None:
    """The ``text_hash`` of the text a current vector was computed from, rebuilt from the node's
    properties as Conform builds it: Resolve and Conform keep a continuing node's persisted label, so the
    text uses that label, never ``label_disagreement``."""

    text = embedding_text(current.label, current.name)
    if name == "embedding":
        stmt = current.properties.get(STMT_PROPERTY)
        if stmt:
            text = f"{text}. {stmt}"
    return text_hash(text)


def _links_changed(current: CurrentNode, node: TargetNode) -> bool:
    before = {(e.fragment_id, e.line_start, e.line_end) for e in current.evidence}
    after = {(e.fragment_id, e.line_start, e.line_end) for e in node.evidence}
    return before != after or dict(current.mentions) != dict(node.mentions)


def _node_properties(node: TargetNode, spec_id: str, given: DiffInput) -> Properties:
    properties: Properties = {}
    for name, value in node.attributes.items():
        if name in RESERVED_PROPERTIES:
            raise ConformanceError("attribute_shadows_property", attribute=name)
        properties[name] = value
    properties.update(
        {
            "spec_id": spec_id,
            "name": node.name,
            "identity_key": node.identity_key,
            "identity_aliases": list(node.identity_aliases),
            QUOTE_PROPERTY: node.quote,
            "statements": json.dumps(
                [statement.model_dump(mode="json") for statement in node.statements], ensure_ascii=False
            ),
            "statement_text": "\n".join(statement.text for statement in node.statements),
            "embedding_model": given.embedding_model,
            "created_at": given.at,
            "updated_at": given.at,
            "created_build_id": given.build_id,
            "last_build_id": given.build_id,
        }
    )
    return properties


def continuing_texts(node: TargetNode, label: str) -> dict[VectorProperty, str]:
    """The embedded texts of a target node, by vector property, as a node of ``label`` (the persisted
    label of the node it continues): Conform's texts start with ``embedding_text(node.label, name)``,
    which becomes ``embedding_text(label, name)``. A node that continues another label (carried over,
    merged into, or matched by an alias) keeps that label, and the next build's Conform keeps it too
    (``conform._keep_persisted_labels``), so its vectors are computed from those texts now."""

    texts: dict[VectorProperty, str] = {}
    if node.embedding_text is not None:
        texts["embedding"] = node.embedding_text
    if node.name_embedding_text is not None:
        texts["name_embedding"] = node.name_embedding_text
    if label == node.label:
        return texts
    old, new = embedding_text(node.label, node.name), embedding_text(label, node.name)
    return {name: new + text[len(old) :] if text.startswith(old) else text for name, text in texts.items()}


def node_vectors(node: TargetNode) -> NodeVectors:
    """The ``text_hash`` of each embedded text of a target node, by vector property."""

    vectors: NodeVectors = {}
    if node.embedding_text is not None:
        vectors["embedding"] = text_hash(node.embedding_text)
    if node.name_embedding_text is not None:
        vectors["name_embedding"] = text_hash(node.name_embedding_text)
    return vectors


# Skipped fragments (FR-BLD-12)


def _skipped_lineage(plan: PlanArtifact, given: DiffInput) -> dict[str, tuple[str, ...]]:
    """Each fragment persisted as ``skipped`` by this build, with the ids its last extracted version
    may be stored under: its own and, for a changed fragment, the id it replaces (``previous_id``)."""

    dirty = set(plan.dirty_ids)
    lineage: dict[str, tuple[str, ...]] = {}
    for fragment in plan.fragments:
        if _outcome(fragment, dirty=fragment.fragment_id in dirty, skipped=given.skipped).status == "skipped":
            ids = (fragment.fragment_id, *((fragment.previous_id,) if fragment.previous_id else ()))
            lineage[fragment.fragment_id] = ids
    return lineage


def _unextracted(plan: PlanArtifact, given: DiffInput) -> dict[str, str]:
    """Persisted fragment ids whose edges this build did not extract again, each mapped to the current
    fragment that carries it (its own id, or the fragment naming it ``previous_id``).

    Those are the fragments persisted as ``skipped`` (``_skipped_lineage``), the fragments of
    ``given.edge_failed_fragments`` (units whose edge call failed without a stored fallback, clean ones
    included), and the unchanged, not dirty fragments of a unit with a skipped fragment (a failed edge
    call skips the unit's dirty fragments and leaves its unchanged ones without an answer).
    """

    carried = {fid: current for current, lineage in _skipped_lineage(plan, given).items() for fid in lineage}
    failed = {
        fid
        for unit in plan.units
        if any(fid in given.skipped for fid in unit.fragment_ids)
        for fid in unit.fragment_ids
    }
    dirty = set(plan.dirty_ids)
    edge_failed = set(given.edge_failed_fragments)
    for fragment in plan.fragments:
        fid = fragment.fragment_id
        if fid in edge_failed or (fid in failed and fid not in dirty and fragment.change == "unchanged"):
            for previous in (fid, *((fragment.previous_id,) if fragment.previous_id else ())):
                carried.setdefault(previous, fid)
    return carried


def _kept_evidence(
    node: TargetNode, current: CurrentNode, skipped: Mapping[str, tuple[str, ...]]
) -> list[EvidenceLines] | None:
    """The target evidence with the current node's lines kept for each skipped fragment; None when no
    skipped fragment had earlier lines to keep."""

    evidence: list[EvidenceLines] = []
    kept = False
    for lines in node.evidence:
        lineage = skipped.get(lines.fragment_id, ())
        earlier = [e for e in current.evidence if e.fragment_id in lineage]
        if not earlier:
            evidence.append(lines)
            continue
        kept = True
        evidence.extend(
            EvidenceLines(fragment_id=lines.fragment_id, line_start=e.line_start, line_end=e.line_end) for e in earlier
        )
    return evidence if kept else None


def _kept_statements(
    node: TargetNode, current: CurrentNode, skipped: Mapping[str, tuple[str, ...]]
) -> list[StatementRec]:
    """The target statements with the current node's statement kept for each skipped fragment."""

    statements = []
    stated = current.statements()
    for statement in node.statements:
        lineage = skipped.get(statement.fragment_id, ())
        entry = next((entry for entry in stated if entry.get("fragment_id") in lineage), None)
        if entry is None:
            statements.append(statement)
            continue
        statements.append(
            statement.model_copy(
                update={
                    name: entry[name]
                    for name in ("document_id", "revision_id", "version", "text", "at")
                    if name in entry
                }
            )
        )
    return statements


def _kept_identity(placed: _Placed, current: CurrentNode, keys: set[str], rename: dict[str, str]) -> dict[str, Any]:
    """The node update that keeps the current identity key when the target's drifted (and no other target
    holds it); records the replacement in ``rename`` and ``keys``."""

    node = placed.node
    key = current.identity_key
    if not key or key == node.identity_key or key in keys:
        return {}
    rename[node.identity_key] = key
    keys.discard(node.identity_key)
    keys.add(key)
    placed.carried = False
    return {
        "identity_key": key,
        "identity_aliases": tuple(sorted({*node.identity_aliases, node.identity_key} - {key})),
    }


def _keep_skipped_support(match: _NodeMatch, skipped: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    """Keep the last extracted support of continuing nodes in ``skipped`` fragments (FR-BLD-12).

    Conform grounds the nodes reused for a skipped fragment in its new text, which they were not
    extracted from: their evidence falls back to the whole fragment and quote, statement and a span
    identity key drift. The persisted node still holds its support from the last extracted version, so
    for each target evidence fragment that is skipped, the current node's evidence lines and statement
    there (under the fragment's own id or the id it replaces) are kept, re-pointed to the skipped
    fragment, which carries the lineage while the old id retires. A node supported only by skipped
    fragments also keeps its quote and its identity key (the drifted key becomes an alias, unless the
    current key is another target's). Returns the replaced target keys (drifted to kept).
    """

    if not skipped:
        return {}
    keys = {placed.node.identity_key for placed in match.placed}
    rename: dict[str, str] = {}
    for placed in match.placed:
        current = placed.current
        if current is None or not any(e.fragment_id in skipped for e in placed.node.evidence):
            continue
        node = placed.node
        evidence = _kept_evidence(node, current, skipped)
        if evidence is None:
            continue
        update: dict[str, Any] = {
            "evidence": tuple(dict.fromkeys(evidence)),
            "statements": tuple(_kept_statements(node, current, skipped)),
        }
        if all(lines.fragment_id in skipped for lines in node.evidence):
            update["quote"] = current.properties.get(QUOTE_PROPERTY)
            update.update(_kept_identity(placed, current, keys, rename))
        placed.node = node.model_copy(update=update)
    if rename:
        for placed in match.placed:
            if placed.current is not None:
                for member in (placed.current, *placed.merged):
                    match.ends[member.uuid] = (placed.uuid, placed.node.identity_key)
    return rename


def _renamed_edges(edges: Sequence[TargetEdge], rename: Mapping[str, str], ontology: Ontology) -> list[TargetEdge]:
    """Target edges with the identity keys ``_keep_skipped_support`` replaced (symmetric ends reordered)."""

    if not rename:
        return list(edges)
    result = []
    for edge in edges:
        source, target = rename.get(edge.source_key, edge.source_key), rename.get(edge.target_key, edge.target_key)
        if (source, target) == (edge.source_key, edge.target_key):
            result.append(edge)
            continue
        if ontology.is_declared(edge.relation) and ontology.is_symmetric(edge.relation) and target < source:
            source, target = target, source
        result.append(
            edge.model_copy(
                update={"source_key": source, "target_key": target, "edge_key": f"{source}|{edge.relation}|{target}"}
            )
        )
    return result


# Edges


@dataclass(frozen=True, slots=True)
class _Placement:
    """Where a current edge lands after the node diff: its key and its ends' uuids."""

    edge: CurrentEdge
    key: str
    source: str
    target: str

    @property
    def in_place(self) -> bool:
        return (self.edge.source_uuid, self.edge.target_uuid) == (self.source, self.target)

    @property
    def inferred(self) -> bool:
        return self.edge.basis == INFERRED


@dataclass(slots=True)
class _EdgeWork:
    """What ``_match_edges`` collects: edge operations, the edge uuids in use and a ``ref`` namer."""

    given: DiffInput
    spec_ids: dict[str, str]
    edge_uuids: set[str]
    deletes: list[CurrentEdge] = field(default_factory=list)
    updates: list[EdgeUpdate] = field(default_factory=list)
    creates: list[EdgeCreate] = field(default_factory=list)
    inferred_removed: list[str] = field(default_factory=list)

    def ref(self, edge: CurrentEdge) -> str:
        return edge_ref(self.spec_ids.get(edge.source_uuid, ""), edge.relation, self.spec_ids.get(edge.target_uuid, ""))

    def update(self, edge: CurrentEdge, changed: Properties) -> None:
        """Update ``edge`` in place with ``changed`` and the build stamps."""

        stamped = {**changed, "updated_at": self.given.at, "last_build_id": self.given.build_id}
        self.updates.append(_edge_update(edge, stamped))


def _match_target_edge(
    work: _EdgeWork,
    edge: TargetEdge,
    ends: tuple[str, str],
    found: Sequence[_Placement],
    *,
    successor: Mapping[str, str],
    skipped: Mapping[str, tuple[str, ...]],
) -> None:
    """Settle one target edge with the current edges that landed on its key (``found``, best first)."""

    source, target = ends
    given = work.given
    desired: Properties = {
        "edge_key": edge.edge_key,
        "basis": edge.basis,
        "source_fragment_ids": list(edge.fragment_ids),
        "fact": edge.fact,
    }
    if not found:
        uuid = _edge_uuid(given.project_id, edge.edge_key, work.edge_uuids)
        work.creates.append(
            _create(
                edge.relation,
                edge.edge_key,
                uuid,
                source=source,
                target=target,
                properties={**desired, "created_at": given.at},
                given=given,
            )
        )
        return
    chosen, rest = found[0], found[1:]
    if edge.fragment_ids and all(fid in skipped for fid in edge.fragment_ids):
        kept_ids = [
            successor[fid]
            for fid in chosen.edge.properties.get("source_fragment_ids") or ()
            if isinstance(fid, str) and fid in successor
        ]
        if kept_ids:
            desired["source_fragment_ids"] = list(dict.fromkeys(kept_ids))
            desired["fact"] = chosen.edge.properties.get("fact")
    for other in rest:
        work.deletes.append(other.edge)
        if other.inferred:
            work.inferred_removed.append(work.ref(other.edge))
    if chosen.in_place:
        changed = {name: value for name, value in desired.items() if _stored(chosen.edge.properties.get(name)) != value}
        if chosen.inferred:
            changed.update({name: None for name in INFERRED_EDGE_PROPERTIES if name in chosen.edge.properties})
        if changed:
            work.update(chosen.edge, changed)
    else:
        work.deletes.append(chosen.edge)
        created_at = chosen.edge.properties.get("created_at") or given.at
        uuid = chosen.edge.uuid or _edge_uuid(given.project_id, edge.edge_key, work.edge_uuids)
        work.creates.append(
            _create(
                edge.relation,
                edge.edge_key,
                uuid,
                source=source,
                target=target,
                properties={**desired, "created_at": created_at},
                given=given,
            )
        )


def _keep_leftover(work: _EdgeWork, placement: _Placement, fragment_ids: list[str] | None, key: str) -> None:
    """Keep one leftover current edge on ``key``: in place (rekeyed or re-pointed as needed) or moved."""

    edge, given = placement.edge, work.given
    changed: Properties = {"edge_key": key} if edge.edge_key != key else {}
    if fragment_ids is not None and _stored(edge.properties.get("source_fragment_ids")) != fragment_ids:
        changed["source_fragment_ids"] = [*fragment_ids]
    if placement.in_place:
        if changed:
            work.update(edge, changed)
        return
    work.deletes.append(edge)
    properties = {name: value for name, value in edge.properties.items() if name not in ("uuid", "group_id")}
    properties.update({**changed, "edge_key": key, "updated_at": given.at, "last_build_id": given.build_id})
    uuid = edge.uuid or _edge_uuid(given.project_id, key, work.edge_uuids)
    work.creates.append(
        EdgeCreate(
            uuid=uuid,
            relation=edge.relation,
            edge_key=key,
            source_uuid=placement.source,
            target_uuid=placement.target,
            properties={**properties, "uuid": uuid},
        )
    )


def _settle_leftovers(
    work: _EdgeWork, landed: Mapping[str, list[_Placement]], unextracted: Mapping[str, str], after: Counter[str]
) -> None:
    """Current edges no target edge took: keep one per key when inferred or unextracted, delete the rest."""

    for key in sorted(landed):
        ranked: list[tuple[_Placement, list[str] | None]] = []
        for placement in landed[key]:
            fragment_ids = None if placement.inferred else _kept_fragment_ids(placement.edge, unextracted)
            if placement.inferred or fragment_ids is not None:
                ranked.append((placement, fragment_ids))
            else:
                work.deletes.append(placement.edge)
        ranked.sort(key=lambda item: (item[0].inferred, not item[0].in_place, item[0].edge.uuid))
        kept_one = False
        for placement, fragment_ids in ranked:
            edge = placement.edge
            loop = placement.source == placement.target and edge.source_uuid != edge.target_uuid
            if kept_one or loop:
                work.deletes.append(edge)
                if placement.inferred:
                    work.inferred_removed.append(work.ref(edge))
                continue
            kept_one = True
            after[edge.relation] += 1
            _keep_leftover(work, placement, fragment_ids, key)


def _match_edges(
    edges: Sequence[TargetEdge],
    uuids: Mapping[str, str],
    ends: Mapping[str, tuple[str, str]],
    ontology: Ontology,
    given: DiffInput,
    *,
    skipped: Mapping[str, tuple[str, ...]] | None = None,
    unextracted: Mapping[str, str] | None = None,
) -> tuple[list[EdgeDelete], list[Operation], list[str], Counter[str]]:
    """``(edge deletes, edge updates then creates, inferred edges removed, edges after)``; the removed
    inferred edges are named in ``edge_ref`` form, by their ends' current ``spec_id``s; ``edges
    after``: the edges per relation the graph holds once these are applied (every target edge and
    every kept leftover).

    Each target edge takes the current edge that lands on its key (one already between its ends
    first, then a stated or implied one, then the lowest uuid): updated in place when its ends are
    unchanged, else deleted and created again with its ``uuid`` and ``created_at``; without one it is
    created. A target edge whose evidence is only in ``skipped`` fragments keeps the matched edge's
    ``fact`` and evidence (re-pointed to the fragments carrying their lineage), as its nodes keep theirs
    (FR-BLD-12).

    A current edge left over (no target edge has its key) whose ends both survive is kept, rekeyed or
    moved with its ends, when it is inferred (outside the diff), or when it is stated or implied and
    every one of its ``source_fragment_ids`` is ``unextracted`` (``_unextracted``: skipped, or unchanged
    in a unit whose call failed): no call this build answered for its evidence, so its absence from the
    target is no evidence of removal. Such a stated edge keeps its ``fact`` and its fragment ids,
    re-pointed to the fragments that carry them. Per key one edge is kept (a stated one before an
    inferred one, then the one in place, then the lowest uuid), never as a new self-loop; every other
    leftover edge is deleted.
    """

    skipped = skipped or {}
    unextracted = unextracted or {}
    successor = {previous: fid for fid, lineage in skipped.items() for previous in lineage}

    landed: dict[str, list[_Placement]] = {}
    work = _EdgeWork(
        given=given,
        spec_ids={node.uuid: node.spec_id for node in given.current.nodes},
        edge_uuids={edge.uuid for edge in given.current.edges if edge.uuid},
    )
    for edge in given.current.edges:
        placement = _land(edge, ends, ontology)
        if placement is None:
            work.deletes.append(edge)
            if edge.basis == INFERRED:
                work.inferred_removed.append(work.ref(edge))
        else:
            landed.setdefault(placement.key, []).append(placement)

    for target_edge in sorted(edges, key=lambda item: (item.relation, item.edge_key)):
        source, target = uuids.get(target_edge.source_key), uuids.get(target_edge.target_key)
        if source is None or target is None:
            raise ConformanceError("diff_edge_endpoint_unknown", relation=target_edge.relation)
        found = sorted(
            (p for p in landed.pop(target_edge.edge_key, ()) if p.edge.relation == target_edge.relation),
            key=lambda p: (not p.in_place, p.inferred, p.edge.uuid),
        )
        _match_target_edge(work, target_edge, (source, target), found, successor=successor, skipped=skipped)

    after: Counter[str] = Counter(edge.relation for edge in edges)
    _settle_leftovers(work, landed, unextracted, after)

    removed = [
        EdgeDelete(relation=relation, edge_key=key, uuid=uuid)
        for relation, key, uuid in sorted({(edge.relation, edge.edge_key, edge.uuid) for edge in work.deletes})
    ]
    writes: list[Operation] = [
        *sorted(work.updates, key=lambda op: (op.relation, op.edge_key, op.uuid)),
        *sorted(work.creates, key=lambda op: (op.relation, op.edge_key)),
    ]
    return removed, writes, work.inferred_removed, after


def _kept_fragment_ids(edge: CurrentEdge, unextracted: Mapping[str, str]) -> list[str] | None:
    """A leftover stated or implied edge's fragment ids re-pointed through ``unextracted``, when every
    one of them is there; else None (the edge is deleted)."""

    raw = edge.properties.get("source_fragment_ids") or ()
    ids = [fid for fid in raw if isinstance(fid, str)]
    if not ids or len(ids) != len(raw) or not all(fid in unextracted for fid in ids):
        return None
    return list(dict.fromkeys(unextracted[fid] for fid in ids))


def _land(edge: CurrentEdge, ends: Mapping[str, tuple[str, str]], ontology: Ontology) -> _Placement | None:
    """The key and ends of a current edge after the node diff; None when an end is deleted."""

    source, target = ends.get(edge.source_uuid), ends.get(edge.target_uuid)
    if source is None or target is None:
        return None
    if ontology.is_declared(edge.relation) and ontology.is_symmetric(edge.relation) and target[1] < source[1]:
        source, target = target, source
    return _Placement(edge=edge, key=f"{source[1]}|{edge.relation}|{target[1]}", source=source[0], target=target[0])


def _edge_uuid(project_id: str, key: str, used: set[str]) -> str:
    """``edge_uuid`` of ``key``; one a current edge holds is not reused (``<key>|2``, ``|3``...)."""

    uuid, number = edge_uuid(project_id, key), 1
    while uuid in used:
        number += 1
        uuid = edge_uuid(project_id, f"{key}|{number}")
    used.add(uuid)
    return uuid


def _create(
    relation: str, key: str, uuid: str, *, source: str, target: str, properties: Properties, given: DiffInput
) -> EdgeCreate:
    return EdgeCreate(
        uuid=uuid,
        relation=relation,
        edge_key=key,
        source_uuid=source,
        target_uuid=target,
        properties={
            "uuid": uuid,
            "basis": properties["basis"],
            "source_fragment_ids": properties["source_fragment_ids"],
            "fact": properties["fact"],
            "created_at": properties["created_at"],
            "updated_at": given.at,
            "last_build_id": given.build_id,
        },
    )


def _edge_update(edge: CurrentEdge, properties: Properties) -> EdgeUpdate:
    return EdgeUpdate(uuid=edge.uuid, relation=edge.relation, edge_key=edge.edge_key, properties=properties)


# Documents and fragments


def _documents(plan: PlanArtifact, given: DiffInput) -> list[Operation]:
    """Documents, then fragments with their extraction outcome, then retired fragments; a document or
    fragment persisted exactly so already is left out."""

    persisted = {document.document_id: document for document in given.current.documents}
    stored = {fragment.fragment_id: fragment for fragment in given.current.fragments}
    operations: list[Operation] = []
    for document in plan.documents:
        before = persisted.get(document.document_id)
        if document.status == "active":
            if before != document:
                operations.append(
                    DocumentUpsert(document=document, spec_id=f"{DOCUMENT_SPEC_PREFIX}{document.document_id}")
                )
        elif before is None or before.status != document.status:
            operations.append(DocumentStatusChange(document_id=document.document_id, status=document.status))
    dirty = set(plan.dirty_ids)
    for fragment in plan.fragments:
        outcome = _outcome(fragment, dirty=fragment.fragment_id in dirty, skipped=given.skipped)
        spec_id = f"{FRAGMENT_SPEC_PREFIX}{fragment.fragment_id}"
        if not _fragment_persisted(outcome, spec_id, stored.get(fragment.fragment_id)):
            operations.append(FragmentUpsert(fragment=outcome, spec_id=spec_id))
    for fid in plan.retired_ids:
        before_fragment = stored.get(fid)
        if before_fragment is None or before_fragment.status != "retired":
            operations.append(FragmentStatusChange(fragment_id=fid, status="retired"))
    return operations


def _fragment_persisted(fragment: FragmentRec, spec_id: str, stored: CurrentFragment | None) -> bool:
    return stored is not None and stored == CurrentFragment(
        fragment_id=fragment.fragment_id,
        document_id=fragment.document_id,
        status=fragment.status,
        content_hash=fragment.content_hash,
        extracted_hash=fragment.extracted_hash,
        error_code=fragment.error_code,
        spec_id=spec_id,
        heading_path=tuple(fragment.heading_path),
        ordinal=fragment.ordinal,
        line_start=fragment.line_start,
        line_end=fragment.line_end,
    )


def _outcome(fragment: FragmentRec, *, dirty: bool, skipped: Mapping[str, str]) -> FragmentRec:
    """A fragment as persisted: ``skipped`` with its error code, or extracted at its content hash."""

    code = skipped.get(fragment.fragment_id)
    if code is not None:
        return fragment.model_copy(update={"status": "skipped", "error_code": code})
    if dirty:
        return fragment.model_copy(
            update={"status": "active", "error_code": None, "extracted_hash": fragment.content_hash}
        )
    return fragment
