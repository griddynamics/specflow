"""Step artifacts, reports and results of a build (docs/graph/build.md, analysis.md; tech-specs 5.2).

Every step reads its inputs from and writes its output to the ``BuildStore`` as one of these models
(``ARTIFACTS`` maps a step to its model), so steps can run and resume one by one. All models are
frozen, refuse unknown fields and round-trip through JSON (``model_dump_json``/``model_validate_json``).
Sequences are tuples; mappings keep the insertion order their producer gives them, and producers
insert in a deterministic order (document order, then ids).

Edge ``basis`` values (``stated``, ``implied``, ``inferred``; data-model.md "Edges") are library
vocabulary, not ontology content, so they are typed literally even though two of them also occur in
a vocabulary of the shipped ontology.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from graph.core.identity import SpecIdRow
from graph.core.llm.usage import UsageCounts

BuildMode = Literal["first", "update", "rebuild"]
BuildStatus = Literal["succeeded", "partially_succeeded", "failed"]
StepName = Literal["plan", "extract_nodes", "resolve", "extract_edges", "conform", "diff", "persist", "infer", "verify"]
STEPS: Final[tuple[StepName, ...]] = (
    "plan",
    "extract_nodes",
    "resolve",
    "extract_edges",
    "conform",
    "diff",
    "persist",
    "infer",
    "verify",
)
FragmentStatus = Literal["active", "skipped", "retired"]
FragmentChange = Literal["new", "changed", "unchanged", "retried"]
DocumentStatus = Literal["active", "removed"]
AttributeValue = str | int | float | bool | None
Window = tuple[int, int]


class ArtifactModel(BaseModel):
    """Base of every artifact: frozen, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# 1. Plan


class DocumentRec(ArtifactModel):
    """Plan (build.md step 1): one lineage of the snapshot or of the persisted graph, as it will be persisted."""

    document_id: str
    revision_id: str
    version: int
    path: str
    title: str
    status: DocumentStatus = "active"


class FragmentRec(ArtifactModel):
    """Plan (build.md step 1): one fragment of a current revision (data-model.md "Document and Fragment")."""

    # ``text``: source lines ``line_start..line_end`` (1-based, inclusive) joined by newlines, masked
    # lines blank, so line ``i`` of ``text`` is source line ``line_start + i``. ``previous_id``: the
    # persisted fragment a ``changed`` fragment replaces.
    fragment_id: str
    document_id: str
    heading_path: tuple[str, ...]
    ordinal: int
    line_start: int
    line_end: int
    text: str
    content_hash: str
    tokens: int
    status: FragmentStatus = "active"
    change: FragmentChange = "new"
    previous_id: str | None = None
    extracted_hash: str | None = None
    error_code: str | None = None


class Unit(ArtifactModel):
    """Plan (build.md step 1): consecutive fragments of one document, with its node-extraction windows."""

    # ``windows``: ``(start, end)`` indexes into ``fragment_ids``, end exclusive, one node-extraction
    # call each; empty when the unit has nothing to extract.
    unit_id: str
    document_id: str
    fragment_ids: tuple[str, ...]
    tokens: int
    content_hash: str
    windows: tuple[Window, ...] = ()


class ChangeSet(ArtifactModel):
    """Plan (build.md step 1.2): lineage ids by change against the persisted documents."""

    new: tuple[str, ...] = ()
    revised: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retried: tuple[str, ...] = ()


class PlanArtifact(ArtifactModel):
    """Plan (build.md step 1): change set, documents, every current fragment, retired and dirty ids, units."""

    mode: BuildMode
    change_set: ChangeSet
    documents: tuple[DocumentRec, ...]
    fragments: tuple[FragmentRec, ...]
    retired_ids: tuple[str, ...] = ()
    dirty_ids: tuple[str, ...] = ()
    units: tuple[Unit, ...] = ()
    # Lines masked as parser comments (build.md Plan 1), by lineage id; lineages without any are left out.
    masked_lines: dict[str, int] = {}


# 2. Extract nodes


Attribution = Literal["graphiti", "window_fallback"]
"""How a raw node got its fragments: Graphiti attributed them, or the window did."""


class RawNode(ArtifactModel):
    """Extract nodes (build.md step 2): one node of one extraction call, tied to its fragments."""

    raw_id: str
    name: str
    label: str
    unit_id: str
    call_id: str
    window: Window
    fragment_ids: tuple[str, ...]
    attribution: Attribution = "graphiti"
    reused: bool = False


class NodeCounters(ArtifactModel):
    """Extract nodes: call and attribution counts for the build metrics."""

    windows: int = 0
    calls: int = 0
    failed_calls: int = 0
    truncation_splits: int = 0
    window_fallbacks: int = 0
    default_attributions: int = 0
    fragments_reused: int = 0
    """Fragments whose nodes came from the ``ExtractionStore`` (``update`` builds)."""
    fragments_supported: int = 0
    """Skipped fragments among ``fragments_reused``, kept by their last extracted version (FR-BLD-12)."""
    known_terms: int = 0
    label_collapses: int = 0
    """Names a call returned with two or more labels, which Graphiti collapsed into one node."""


class NodesArtifact(ArtifactModel):
    """Extract nodes (build.md step 2): raw nodes, skipped fragments by error code, counters."""

    raw_nodes: tuple[RawNode, ...] = ()
    skipped: dict[str, str] = {}
    known_terms_source: Literal["hub_catalog", "persisted", "none"] = "none"
    counters: NodeCounters = NodeCounters()


# 3. Resolve


class CanonicalNode(ArtifactModel):
    """Resolve (build.md step 3): merged raw nodes under one identity key; ``cid`` is build-local."""

    # ``identity_aliases``: identity keys of the groups a cross-label or hub consolidation merge folded into this one.
    # ``name_embedding_text``: the text of the ``name_embedding``; the vector itself lives in the
    # vector store (``graph.build.vectors``), never in the artifact.
    cid: str
    identity_key: str
    identity_aliases: tuple[str, ...] = ()
    provisional: bool = False
    name: str
    label: str
    member_ids: tuple[str, ...]
    explicit_ids: tuple[str, ...] = ()
    fragment_ids: tuple[str, ...] = ()
    name_embedding_text: str | None = None
    label_disagreement: str | None = None
    """``update`` builds: the label extraction gave a node that keeps its persisted label (and key
    scope) instead; None when they agree."""


class ConsolidationCounters(ArtifactModel):
    """The hub consolidation pass (Resolve, ``graph.build.consolidate``): blocks, calls, proposed
    groups, dropped ids, vetoes, merges and decision memory use."""

    items: int = 0
    blocks: int = 0
    blocks_skipped: int = 0
    """Blocks decided from stored decisions alone (no model call)."""
    calls: int = 0
    failed_calls: int = 0
    groups_proposed: int = 0
    """Groups of the answers (and stored ``same`` pairs) with at least two known ids."""
    unknown_ids: int = 0
    """Ids of an answer that name no line of its block, or repeat one already grouped (dropped)."""
    vetoes: int = 0
    """Members of a proposed group refused by a veto (explicit ids, cluster size, stored decision)."""
    merges: int = 0
    cross_label_merges: int = 0
    """Merges that joined hubs of different labels (within ``merges``)."""
    decisions_read: int = 0
    decision_overrides: int = 0
    """Stored decisions that changed the answer: a stored ``same`` pair added, a stored ``different``
    pair refused inside a proposed group."""
    decisions_written: int = 0


class ResolveCounters(ArtifactModel):
    """Resolve: merge counts for the build metrics."""

    exact_merges: int = 0
    guard_rejections: int = 0
    """Cross-label hub merges refused by the explicit-id guard."""
    embeddings_reused: int = 0
    empty_names: int = 0
    """Raw nodes without an explicit id whose normalised name is empty; they get no key and are dropped."""
    cross_label_hub_merges: int = 0
    """Hub clusters joined into a cluster of another hub label because they share a normalised name."""
    labels_kept: int = 0
    """Canonical nodes that kept the label of the persisted node they continue (``update`` builds)."""
    consolidation: ConsolidationCounters = ConsolidationCounters()
    """The hub consolidation pass."""


class ResolveArtifact(ArtifactModel):
    """Resolve (build.md step 3): canonical nodes in ``cid`` order."""

    canonical: tuple[CanonicalNode, ...] = ()
    counters: ResolveCounters = ResolveCounters()


# 4. Extract edges


class RawEdge(ArtifactModel):
    """Extract edges (build.md step 4): one edge of one unit's call between canonical nodes."""

    relation: str
    source_cid: str
    target_cid: str
    fact: str | None = None
    fragment_ids: tuple[str, ...] = ()
    unit_id: str
    reused: bool = False


class EdgeCounters(ArtifactModel):
    """Extract edges: call counts for the build metrics."""

    units_extracted: int = 0
    units_reused: int = 0
    calls: int = 0
    failed_calls: int = 0
    attribute_batches: int = 0
    attribute_batches_failed: int = 0
    attribute_nodes_reused: int = 0
    """Nodes whose attribute values came from the ``ExtractionStore`` (``update`` builds)."""
    units_fallback: int = 0
    """Units whose edge call failed and that kept their last stored edges (same content, else same
    unit identity)."""
    catalogs_kept: int = 0
    """Clean units of an ``update`` build whose catalog took its nearness hubs from their last stored
    catalog instead of ranking them again."""
    truncation_splits: int = 0
    attribution_fallbacks: int = 0
    unknown_name_edges: int = 0
    """Facts the model returned between names outside the call's catalog (dropped)."""
    unknown_relation_edges: int = 0
    """Facts the model returned with a relation outside the ontology's edge types (dropped)."""


class EdgesArtifact(ArtifactModel):
    """Extract edges (build.md step 4): raw edges, attribute values by ``cid``, catalog ids per unit."""

    raw_edges: tuple[RawEdge, ...] = ()
    attributes: dict[str, dict[str, AttributeValue]] = {}
    attributes_failed: tuple[str, ...] = ()
    """Cids whose attributes batch failed (no values this build)."""
    catalogs: dict[str, tuple[str, ...]] = {}
    skipped: dict[str, str] = {}
    called_units: tuple[str, ...] = ()
    """Units whose edge call ran this build (answered or not), in plan order."""
    edge_failed_fragments: tuple[str, ...] = ()
    """Every fragment (clean ones too) of the units whose edge call failed without fallback edges,
    in plan order: their edges are not this build's (Diff keeps the current ones)."""
    recovered: tuple[str, ...] = ()
    """Fragments persisted ``skipped`` whose current content is now fully extracted: their nodes are
    stored for it and their unit's edges answered (or were reused) this build; persisted ``active``."""
    counters: EdgeCounters = EdgeCounters()


# 5. Conform


class EvidenceLines(ArtifactModel):
    """Conform (build.md step 5): source lines of one fragment holding a node's evidence (``REFERENCED_BY``)."""

    fragment_id: str
    line_start: int
    line_end: int


class StatementRec(ArtifactModel):
    """Conform: what one supporting fragment says about a node (data-model.md "Statements")."""

    fragment_id: str
    document_id: str
    revision_id: str
    version: int
    text: str
    at: str


class TargetNode(ArtifactModel):
    """Conform (build.md step 5): one grounded node of the target graph."""

    identity_key: str
    identity_aliases: tuple[str, ...] = ()
    label: str
    name: str
    attributes: dict[str, AttributeValue] = {}
    quote: str | None = None
    evidence: tuple[EvidenceLines, ...]
    statements: tuple[StatementRec, ...] = ()
    mentions: dict[str, int] = {}
    embedding_text: str | None = None
    """Text of the search ``embedding``; the vector lives in the vector store (``graph.build.vectors``)."""
    name_embedding_text: str | None = None
    """Text of the ``name_embedding``; the vector lives in the vector store."""
    explicit_ids: tuple[str, ...] = ()
    label_disagreement: str | None = None
    """The label extraction gave when the node kept its persisted label (``CanonicalNode``)."""
    attributes_missing: bool = False
    """The node's attributes batch failed: ``attributes`` (and so ``quote``) are not this build's
    values; Diff keeps the persisted ones."""


Basis = Literal["stated", "implied", "inferred"]
"""How a domain edge is known (data-model.md "Edges")."""


class TargetEdge(ArtifactModel):
    """Conform (build.md step 5): one legal domain edge between target nodes, by identity key."""

    edge_key: str
    relation: str
    source_key: str
    target_key: str
    basis: Basis
    fact: str | None = None
    fragment_ids: tuple[str, ...]


class TargetCounters(ArtifactModel):
    """Conform: dropped edges per relation and reason, ``exclusive`` conflicts, grounding fallbacks,
    span-key merges and splits, requirement dedup candidates, merges, adjudications and guard refusals."""

    dropped_edges: dict[str, dict[str, int]] = {}
    flipped_edges: int = 0
    implied_edges: int = 0
    exclusive_conflicts: int = 0
    quote_fallbacks: int = 0
    fact_fallbacks: int = 0
    span_merges: int = 0
    span_splits: int = 0
    dedup_candidates: int = 0
    dedup_merges: int = 0
    dedup_adjudications: int = 0
    dedup_guard_refusals: int = 0
    dedup_verdicts_reused: int = 0
    """Dedup verdicts taken from the ``ExtractionStore`` instead of a small-model call."""


class TargetGraph(ArtifactModel):
    """Conform (build.md step 5): the target graph, nodes by identity key, edges by edge key."""

    nodes: tuple[TargetNode, ...] = ()
    edges: tuple[TargetEdge, ...] = ()
    counters: TargetCounters = TargetCounters()


# 6. Diff and 7. Persist

PersistPhase = Literal[
    "remove_edges", "merge_nodes", "update_nodes", "delete_nodes", "create_nodes", "write_edges", "write_documents"
]
PERSIST_PHASES: Final[tuple[PersistPhase, ...]] = (
    "remove_edges",
    "merge_nodes",
    "update_nodes",
    "delete_nodes",
    "create_nodes",
    "write_edges",
    "write_documents",
)
Properties = dict[str, JsonValue]
VectorProperty = Literal["embedding", "name_embedding"]
NodeVectors = dict[VectorProperty, str]
"""Vector properties of a node operation: property name to the ``text_hash`` of the embedded text;
Persist writes the vectors from the vector store (``graph.build.vectors``)."""


class _Operation(ArtifactModel):
    phase: ClassVar[PersistPhase]


class EdgeDelete(_Operation):
    """Persist phase 1: delete a domain edge (removed, or replaced by a merge or split)."""

    phase: ClassVar[PersistPhase] = "remove_edges"
    op: Literal["edge_delete"] = "edge_delete"
    relation: str
    edge_key: str
    uuid: str


class NodeMerge(_Operation):
    """Persist phase 2: merged nodes' foreign relationships move to the survivor, then they are deleted."""

    phase: ClassVar[PersistPhase] = "merge_nodes"
    op: Literal["node_merge"] = "node_merge"
    survivor_uuid: str
    survivor_spec_id: str
    merged_uuids: tuple[str, ...]
    merged_spec_ids: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    """The survivor's ``spec_id_aliases`` after the merge (its own, the merged ids and their aliases)."""


class NodeUpdate(_Operation):
    """Persist phase 3: set build-owned properties of a node in place (``None`` removes one)."""

    phase: ClassVar[PersistPhase] = "update_nodes"
    op: Literal["node_update"] = "node_update"
    uuid: str
    spec_id: str
    label: str
    properties: Properties
    vectors: NodeVectors = {}


class NodeCarryOver(_Operation):
    """Persist phase 3: a new target takes a node's ``uuid``, ``spec_id`` and relationships."""

    phase: ClassVar[PersistPhase] = "update_nodes"
    op: Literal["node_carry_over"] = "node_carry_over"
    uuid: str
    spec_id: str
    label: str
    from_identity_key: str
    properties: Properties
    vectors: NodeVectors = {}


class NodeDelete(_Operation):
    """Persist phase 4: delete a node that lost all support, with its relationships (W4)."""

    phase: ClassVar[PersistPhase] = "delete_nodes"
    op: Literal["node_delete"] = "node_delete"
    uuid: str
    spec_id: str


class NodeCreate(_Operation):
    """Persist phase 5: create a spec node with a new ``spec_id``."""

    phase: ClassVar[PersistPhase] = "create_nodes"
    op: Literal["node_create"] = "node_create"
    uuid: str
    spec_id: str
    label: str
    properties: Properties
    vectors: NodeVectors = {}


class EdgeCreate(_Operation):
    """Persist phase 6: create a domain edge between existing nodes."""

    phase: ClassVar[PersistPhase] = "write_edges"
    op: Literal["edge_create"] = "edge_create"
    uuid: str
    relation: str
    edge_key: str
    source_uuid: str
    target_uuid: str
    properties: Properties


class EdgeUpdate(_Operation):
    """Persist phase 6: set properties of a domain edge in place (an inferred edge becoming stated too)."""

    phase: ClassVar[PersistPhase] = "write_edges"
    op: Literal["edge_update"] = "edge_update"
    uuid: str
    relation: str
    edge_key: str
    properties: Properties


class NodeLinks(_Operation):
    """Persist phase 6: replace a node's ``REFERENCED_BY`` (evidence lines) and ``MENTIONED_IN`` (counts)."""

    phase: ClassVar[PersistPhase] = "write_edges"
    op: Literal["node_links"] = "node_links"
    uuid: str
    evidence: tuple[EvidenceLines, ...]
    mentions: dict[str, int] = {}


class DocumentUpsert(_Operation):
    """Persist phase 7: create or update a ``Document`` node."""

    phase: ClassVar[PersistPhase] = "write_documents"
    op: Literal["document_upsert"] = "document_upsert"
    document: DocumentRec
    spec_id: str


class DocumentStatusChange(_Operation):
    """Persist phase 7: set a document's status (``removed``, or ``active`` when its lineage returns)."""

    phase: ClassVar[PersistPhase] = "write_documents"
    op: Literal["document_status"] = "document_status"
    document_id: str
    status: DocumentStatus


class FragmentUpsert(_Operation):
    """Persist phase 7: create or update a ``Fragment`` node and its ``CONTAINS`` from its document."""

    phase: ClassVar[PersistPhase] = "write_documents"
    op: Literal["fragment_upsert"] = "fragment_upsert"
    fragment: FragmentRec
    spec_id: str


class FragmentStatusChange(_Operation):
    """Persist phase 7: set a fragment's status and error code (``retired``, ``skipped``, ``active``)."""

    phase: ClassVar[PersistPhase] = "write_documents"
    op: Literal["fragment_status"] = "fragment_status"
    fragment_id: str
    status: FragmentStatus
    error_code: str | None = None


Operation = Annotated[
    EdgeDelete
    | NodeMerge
    | NodeUpdate
    | NodeCarryOver
    | NodeDelete
    | NodeCreate
    | EdgeCreate
    | EdgeUpdate
    | NodeLinks
    | DocumentUpsert
    | DocumentStatusChange
    | FragmentUpsert
    | FragmentStatusChange,
    Field(discriminator="op"),
]


def edge_ref(source_spec_id: str, relation: str, target_spec_id: str) -> str:
    """How reports name a domain edge: ``<source spec_id>|<RELATION>|<target spec_id>`` in its stored
    direction. Never the edge key, whose identity keys can hold document text (NFR-SEC-01)."""

    return f"{source_spec_id}|{relation}|{target_spec_id}"


class SpecChanges(ArtifactModel):
    """Diff (build.md step 6) and Infer: changed ``spec_id``s, and the inferred edges added and
    removed (``edge_ref`` form)."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    rekeyed: tuple[str, ...] = ()
    carried_over: tuple[str, ...] = ()
    inferred_added: tuple[str, ...] = ()
    inferred_removed: tuple[str, ...] = ()


class DiffArtifact(ArtifactModel):
    """Diff (build.md step 6): operations in persist phase order, spec changes and registry rows."""

    operations: tuple[Operation, ...] = ()
    spec_changes: SpecChanges = SpecChanges()
    registry_rows: tuple[SpecIdRow, ...] = ()
    texts: dict[str, str] = {}
    """Embedded texts named by node operations that are no target node's own, by ``text_hash``: a
    continuing node's texts under its persisted label (Persist embeds them when the store lacks them)."""
    nodes_by_label: dict[str, int] = {}
    """Spec nodes per label once Persist has applied the diff: created nodes under their label,
    continuing ones under the label they keep; deleted and merged-away nodes are not counted."""
    edges_by_relation: dict[str, int] = {}
    """Domain edges per relation once Persist has applied the diff: the target's edges and the current
    edges the diff keeps (inferred ones, and stated ones whose evidence was not extracted again)."""

    @model_validator(mode="after")
    def _phase_ordered(self) -> DiffArtifact:
        ranks = [PERSIST_PHASES.index(operation.phase) for operation in self.operations]
        if ranks != sorted(ranks):
            raise ValueError("operations must be in persist phase order")
        return self


class PersistCursor(ArtifactModel):
    """Persist (build.md step 7): ``batch_index`` is the last applied batch (-1: none yet)."""

    batch_index: int = -1
    batches: int = 0
    done: bool = False


# 8. Infer and 9. Verify


class RuleReport(ArtifactModel):
    """Infer: counts of one inference rule (analysis.md "Light inference").

    ``subjects`` have at least one candidate. ``reused`` of them had a stored judgement for their
    current inputs and were not judged again (judge memory); of the others, ``deferred`` were over the
    cap (or could not be read) and not judged. ``judged`` = ``accepted`` + ``declined`` (a valid answer
    naming no candidate) + ``rejected`` (below the floor or a quote not found) + ``invalid`` (an
    answer naming an id it was not given); ``failed`` judgements returned no verdict, and those
    subjects keep their inferred edges like deferred ones. ``truncated``: subjects whose payload was
    cut to the shown-node bound. ``edges_updated``: kept edges whose verdict was refreshed.
    ``error_code``: the rule failed and none of its inferred edges changed.
    """

    rule: str
    subjects: int = 0
    judged: int = 0
    accepted: int = 0
    rejected: int = 0
    invalid: int = 0
    declined: int = 0
    failed: int = 0
    deferred: int = 0
    reused: int = 0
    truncated: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    edges_updated: int = 0
    error_code: str | None = None


class InferenceReport(ArtifactModel):
    """Infer (build.md step 8): per-rule counts and the lists analysis.md names; ``error_code`` on failure.

    ``integration_outliers``: ``spec_id``s of ``endpoints`` subjects linked to fewer or more targets
    than the rule allows (but at least one); ``orphans_without_candidate``: ``spec_id``s of
    ``attach_orphan`` subjects without any candidate; ``spine_unreachable_before``: spine sources that
    reach no root over stated and implied edges. ``inferred_added`` and ``inferred_removed``: the
    inferred edges the swap created and deleted (``edge_ref`` form; merged into the build result's
    ``spec_changes``). ``error_code`` is the first reported rule failure (a non-fatal model error);
    the build still succeeds (FR-INF-05).
    """

    enabled: bool = True
    rules: tuple[RuleReport, ...] = ()
    integration_outliers: tuple[str, ...] = ()
    orphans_without_candidate: tuple[str, ...] = ()
    spine_unreachable_before: int | None = None
    inferred_added: tuple[str, ...] = ()
    inferred_removed: tuple[str, ...] = ()
    error_code: str | None = None


CheckName = Literal[
    "node_label", "spec_id", "illegal_edge", "ungrounded_node", "ungrounded_edge", "inferred_edge", "fragment_state"
]
RepairAction = Literal[
    "set_label", "delete_node", "assign_spec_id", "delete_edge", "restore_evidence", "request_follow_up"
]


class Problem(ArtifactModel):
    """Verify: one blocking problem. ``subject``: a node's ``spec_id`` (its ``uuid`` when it has none),
    an edge as ``<source spec_id>|<RELATION>|<target spec_id>`` (spec ids, never identity keys, so no
    document text), or a fragment id. ``code`` says what is wrong (``graph.build.verify``)."""

    check: CheckName
    subject: str
    code: str | None = None


class Repair(ArtifactModel):
    """Verify: one repair of a problem in a repair round; ``value`` is the label set or the
    ``spec_id`` assigned, or the lineage a follow-up build is requested for."""

    round: int
    check: CheckName
    action: RepairAction
    subject: str
    value: str | None = None


class VerificationMetrics(ArtifactModel):
    """Verify: reported metrics, never repaired (analysis.md "Verification")."""

    spine_unreachable_before: int | None = None
    spine_unreachable: int = 0
    exclusive_conflicts: int = 0
    label_disagreement: int = 0
    dropped_edges: dict[str, int] = {}
    fragments_without_nodes: int = 0


class VerificationReport(ArtifactModel):
    """Verify (build.md step 9, ``verify_project``): problems found and left, repairs, metrics, ``ok``.

    ``found``: problems per check at the first check; ``problems``: those left after the last one.
    ``rounds``: repair rounds applied; ``blocked_rounds``: rounds not applied because they would
    delete more than the valve allows, with their planned deletes in ``valve`` (``nodes``,
    ``edges``, and the project's ``spec_nodes`` and ``domain_edges``). ``removed_spec_ids`` and
    ``removed_inferred_edges`` (``<source spec_id>|<RELATION>|<target spec_id>``, never edge keys) are
    the nodes and inferred edges the repairs deleted.
    ``follow_up_lineages``: documents a follow-up build should extract again (passed as the next
    build's lineages to retry); any makes the build result not ``settled``. ``read_only``: a
    ``verify_project`` run, which never repairs and requests nothing.
    """

    ok: bool = True
    read_only: bool = False
    rounds: int = 0
    blocked_rounds: int = 0
    spec_nodes: int = 0
    domain_edges: int = 0
    found: dict[str, int] = {}
    problems: tuple[Problem, ...] = ()
    repairs: tuple[Repair, ...] = ()
    valve: dict[str, int] = {}
    removed_spec_ids: tuple[str, ...] = ()
    removed_inferred_edges: tuple[str, ...] = ()
    metrics: VerificationMetrics = VerificationMetrics()
    follow_up_lineages: tuple[str, ...] = ()


# Build result and estimate


class VerificationSummary(ArtifactModel):
    """Build result: ``ok``, problems found and left per check, repairs applied, repair rounds applied
    and blocked by the valve, follow-up lineages and the verification metrics."""

    ok: bool
    found: dict[str, int] = {}
    problems: dict[str, int] = {}
    repairs: int = 0
    rounds: int = 0
    blocked_rounds: int = 0
    follow_up_lineages: tuple[str, ...] = ()
    metrics: VerificationMetrics = VerificationMetrics()


class BuildMetrics(ArtifactModel):
    """Build result: build.md "Build result" metrics; reported, never used as gates."""

    fragments_by_status: dict[str, int] = {}
    units_extracted: int = 0
    units_reused: int = 0
    llm: dict[str, UsageCounts] = {}
    nodes_by_label: dict[str, int] = {}
    edges_by_relation: dict[str, int] = {}
    dropped_edges: dict[str, int] = {}
    exclusive_conflicts: int = 0
    spine_unreachable: int | None = None
    attribution_fallbacks: int = 0
    attribute_batches_failed: int = 0
    duration_s: dict[str, float] = {}


class BuildResult(ArtifactModel):
    """Build result (build.md "Build result"); ``verification`` None until verify ran."""

    project_id: str
    build_id: str
    mode: BuildMode
    status: BuildStatus
    settled: bool = False
    spec_changes: SpecChanges = SpecChanges()
    verification: VerificationSummary | None = None
    metrics: BuildMetrics = BuildMetrics()
    skipped_fragments: dict[str, str] = {}
    failed_step: StepName | None = None
    error_code: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime


EstimateStep = Literal[
    "hub_catalog",
    "node_extraction",
    "hub_consolidation",
    "edge_extraction",
    "attributes",
    "requirement_dedup",
    "embeddings",
]


class StepEstimate(ArtifactModel):
    """Cost estimate: expected calls and input tokens of one step."""

    calls: int = 0
    input_tokens: int = 0


class BuildEstimate(ArtifactModel):
    """Cost estimate (build.md "Cost estimate"): plan and reuse outcome, calls and input tokens per step."""

    project_id: str
    mode: BuildMode
    change_set: ChangeSet
    fragments_by_change: dict[str, int] = {}
    fragments_by_status: dict[str, int] = {}
    dirty_fragments: int = 0
    units_to_extract: int = 0
    units_reused: int = 0
    steps: dict[EstimateStep, StepEstimate] = {}


ARTIFACTS: Final[dict[StepName, type[ArtifactModel]]] = {
    "plan": PlanArtifact,
    "extract_nodes": NodesArtifact,
    "resolve": ResolveArtifact,
    "extract_edges": EdgesArtifact,
    "conform": TargetGraph,
    "diff": DiffArtifact,
    "persist": PersistCursor,
    "infer": InferenceReport,
    "verify": VerificationReport,
}
