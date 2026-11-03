"""Step artifacts, reports and results of a build."""

from __future__ import annotations

from typing import Annotated, ClassVar, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from graph.core.identity import SpecIdRow
from graph.core.llm.usage import UsageCounts
from graph.core.ontology import QuarantineReason

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
Confidence = Literal["high", "medium", "low"]
"""A judge's confidence in a hub group or a dedup verdict; ``low`` means unsure (no merge, not stored)."""
DedupGuard = Literal["numbers", "negation", "modality", "ids", "same_sentence"]
"""The pairwise dedup guards of Conform (numbers, negation, modal strength, referenced ids, one source
sentence)."""


class ArtifactModel(BaseModel):
    """Base of every artifact: frozen, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# 1. Plan


class DocumentRec(ArtifactModel):
    """Plan: one lineage of the snapshot or of the persisted graph, as it will be persisted."""

    document_id: str
    revision_id: str
    version: int
    path: str
    title: str
    status: DocumentStatus = "active"


class FragmentRec(ArtifactModel):
    """Plan: one fragment of a current revision."""

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
    """Plan: consecutive fragments of one document, with its node-extraction windows."""

    # ``windows``: ``(start, end)`` indexes into ``fragment_ids``, end exclusive, one node-extraction
    # call each; empty when the unit has nothing to extract.
    unit_id: str
    document_id: str
    fragment_ids: tuple[str, ...]
    tokens: int
    content_hash: str
    windows: tuple[Window, ...] = ()


class ChangeSet(ArtifactModel):
    """Plan: lineage ids by change against the persisted documents."""

    new: tuple[str, ...] = ()
    revised: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retried: tuple[str, ...] = ()


class PlanArtifact(ArtifactModel):
    """Plan: change set, documents, every current fragment, retired and dirty ids, units."""

    mode: BuildMode
    change_set: ChangeSet
    documents: tuple[DocumentRec, ...]
    fragments: tuple[FragmentRec, ...]
    retired_ids: tuple[str, ...] = ()
    dirty_ids: tuple[str, ...] = ()
    units: tuple[Unit, ...] = ()
    # Lines masked as parser comments, by lineage id; lineages without any are left out.
    masked_lines: dict[str, int] = {}


# 2. Extract nodes


KnownTermsSource = Literal["hub_catalog", "hub_pass", "persisted", "persisted_and_catalog", "none"]
"""Where the known terms of a build's statement pass came from: the catalog of its hub pass
(``hub_pass``), the persisted hub nodes, both (``persisted_and_catalog``), or none. ``hub_catalog``
is the value of artifacts written by the small-model outline catalog of earlier releases."""
Attribution = Literal["model", "quote", "window_fallback", "graphiti"]
"""How a raw node got its fragments: the model attributed them, the window fragments its quote matches (``quote``: the
model gave no valid fragment number), or the whole window.
"""


class RawNode(ArtifactModel):
    """Extract nodes: one node of one extraction call, tied to its fragments."""

    raw_id: str
    name: str
    label: str
    unit_id: str
    call_id: str
    window: Window
    fragment_ids: tuple[str, ...]
    attribution: Attribution = "model"
    reused: bool = False
    quote: str | None = None
    """The source text the node call gave for the node (verbatim; None when it gave none)."""
    aliases: tuple[str, ...] = ()
    """Other names the hub pass gave for the node, as written (empty for a statement)."""


class NodeCounters(ArtifactModel):
    """Extract nodes: call and attribution counts for the build metrics."""

    windows: int = 0
    calls: int = 0
    failed_calls: int = 0
    truncation_splits: int = 0
    window_fallbacks: int = 0
    """Raw nodes without a valid fragment number whose quote matched no fragment of the window: tied
    to the whole window."""
    quote_attributions: int = 0
    """Raw nodes without a valid fragment number tied to the fragments their quote matches."""
    quote_outside_window: int | None = None
    """Among ``quote_attributions``: those whose quote matched no window fragment but another fragment of the unit (tied
    to that fragment); None when not measured.
    """
    hub_calls: int = 0
    """Hub pass calls (included in ``calls``; truncation halves included)."""
    known_terms_capped: int = 0
    """Windows (per pass) whose mentioned known terms hit the safety cap (``terms.MENTIONED_TERMS_MAX``)."""
    default_attributions: int = 0
    """Deprecated, always 0 (Graphiti's default attribution is gone); kept so older artifacts load."""
    fragments_reused: int = 0
    """Fragments whose nodes came from the ``ExtractionStore`` (``update`` builds)."""
    fragments_supported: int = 0
    """Skipped fragments among ``fragments_reused``, kept by their last extracted version."""
    known_terms: int = 0
    label_collapses: int = 0
    """Names a call returned with two or more labels (each label is kept as its own raw node)."""
    stray_pass_items: int | None = None
    """Items a node call put in the answer list of the other pass (left out); None when not measured."""
    catalog_batches: int | None = None
    """Small-model hub catalog batches ; None (n/a) since the hub pass is the catalog."""
    catalog_calls: int | None = None
    """Small-model hub catalog calls; None (n/a) since the hub pass is the catalog (its calls: ``hub_calls``)."""
    catalog_failed: int | None = None
    """Small-model hub catalog batches that failed; None (n/a) since the hub pass is the catalog."""
    catalog_reused: int | None = None
    """Small-model hub catalog batches reused; None (n/a) since the hub pass is the catalog."""
    catalog_truncation_splits: int | None = None
    """Small-model hub catalog batches halved; None (n/a) since the hub pass is the catalog."""
    catalog_hubs: int | None = None
    """Hubs of the merged hub catalog (``terms.merge_catalog`` over the hub pass items)."""
    catalog_aliases: int | None = None
    """Aliases of the merged hub catalog, summed over its hubs."""
    catalog_links_refused: int | None = None
    """Alias links of the catalog merge refused because the joined group would grow too large."""
    catalog_aliases_dropped: int | None = None
    """Catalog aliases dropped by the merge (another hub's name, or claimed by two or more hubs)."""
    catalog_ambiguous: int | None = None
    """Catalog hubs of an ``update`` build matching two or more persisted hubs (left out of the known terms)."""


class HubName(ArtifactModel):
    """A hub name and its label, from the hub catalog or a persisted hub node; ``aliases``: other names of the same hub
    (the catalog's aliases, or the values of a persisted hub's ``surface`` identity aliases: names folded in by
    consolidation or a rename), which find the hub by mention too; ``document_ids``: the documents whose hub pass items
    named it (empty for a persisted hub).
    """

    name: str
    label: str
    aliases: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()


class KnownTermsShown(ArtifactModel):
    """The known terms one unit's calls of one pass were shown: those its fragments mention (by
    name or alias) and those shown by nearness, each by the name shown."""

    mentioned: tuple[str, ...] = ()
    nearby: tuple[str, ...] = ()


class NodesArtifact(ArtifactModel):
    """Extract nodes: raw nodes, skipped fragments by error code, counters."""

    raw_nodes: tuple[RawNode, ...] = ()
    skipped: dict[str, str] = {}
    known_terms_source: KnownTermsSource = "none"
    counters: NodeCounters = NodeCounters()
    hub_catalog: tuple[HubName, ...] = ()
    known_terms: dict[str, KnownTermsShown] | None = None
    hub_known_terms: dict[str, KnownTermsShown] | None = None
    persisted_hubs: tuple[HubName, ...] | None = None


# 3. Resolve


class SourceQuote(ArtifactModel):
    """Resolve: one quote the node calls gave a canonical node's members, with the fragments those
    members were attributed to (in plan order); Conform grounds each one in its own fragments."""

    quote: str
    fragment_ids: tuple[str, ...]


class CanonicalNode(ArtifactModel):
    """Resolve: merged raw nodes under one identity key; ``cid`` is build-local."""

    # ``identity_aliases``: identity keys of the groups a hub consolidation merge folded into this one.
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
    quotes: tuple[SourceQuote, ...] = ()
    """Every distinct quote its member raw nodes gave, each with the union of the fragments of the
    members that gave it, ordered by their first fragment in plan order, then by text."""
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
    label_splits: int = 0
    """Proposed groups split into label parts because they mixed a label whose type has
    ``merge_across_labels: false`` with another label (each part is applied on its own)."""
    merges: int = 0
    cross_label_merges: int = 0
    """Merges that joined hubs of different labels (within ``merges``)."""
    decisions_read: int = 0
    decision_overrides: int = 0
    """Stored decisions that changed the answer: a stored ``same`` pair added, a stored ``different``
    pair refused inside a proposed group."""
    decisions_written: int = 0
    pair_candidates_vector: int = 0
    """Pair-pass candidates found by the candidate (rich) vectors."""
    pair_candidates_alias: int = 0
    """Pair-pass candidates found by catalog alias names (a pair found both ways counts in both)."""
    pair_batches: int = 0
    pair_calls: int = 0
    pair_over_cap: int = 0
    """Pair-pass candidates left past the per-build call cap (not judged, not stored; next build)."""
    pair_merges: int = 0
    """Merges of the pair pass (within ``merges``)."""
    alias_merges: int = 0
    """Pair-pass merges of pairs found by a catalog alias (within ``pair_merges``)."""
    unsure_groups: int = 0
    """Groups the judge marked ``low`` confidence: not merged, not stored, not taken as different."""
    decisions_expired: int = 0
    """Stored decisions whose evidence hash no longer matches (skipped; judged again)."""
    kept_apart: int = 0
    """Pairs of two settled items (valid node entries, not unsure) that an answer grouped although no build judged or
    stored the pair, and pair-pass candidates of two items settled before the build without a stored decision (an
    update): kept apart, so unchanged hubs keep their grouping.
    """


class ResolveCounters(ArtifactModel):
    """Resolve: merge counts for the build metrics."""

    exact_merges: int = 0
    guard_rejections: int = 0
    """Deprecated, always 0 (the cross-label name merge is gone); kept so older artifacts load."""
    embeddings_reused: int = 0
    empty_names: int = 0
    """Raw nodes without an explicit id whose normalised name is empty; they get no key and are dropped."""
    cross_label_hub_merges: int = 0
    """Deprecated, always 0 (the cross-label name merge is gone); kept so older artifacts load."""
    labels_kept: int = 0
    """Canonical nodes that kept the label of the persisted node they continue (``update`` builds)."""
    consolidation: ConsolidationCounters = ConsolidationCounters()
    """The hub consolidation pass."""


HubMergeSource = Literal["block", "pair", "stored"]
PairSource = Literal["vector", "alias", "both"]
PairOutcome = Literal["same", "different", "unsure", "vetoed", "over_cap", "block", "kept_apart"]


class HubMerge(ArtifactModel):
    """Resolve: one member a hub consolidation merge folded into an anchor, by identity key: where the
    merge came from (a block call, the pair pass, a stored decision) and the judge's confidence (None
    when the decision carries none)."""

    anchor_key: str
    member_key: str
    source: HubMergeSource
    confidence: Confidence | None = None


class PairCandidate(ArtifactModel):
    """Resolve: a candidate pair of the pair pass (by identity key) in one label part, its name and candidate-vector
    cosines, how it was found and what came of it (``block``: already judged in a block; ``kept_apart``: two items
    settled before an update, no stored decision; ``vetoed``: grouped and refused by a veto, or never joinable, a
    section and its subsection).
    """

    key_a: str
    key_b: str
    part: str
    name_cos: float
    rich_cos: float | None = None
    source: PairSource
    outcome: PairOutcome


class ResolveArtifact(ArtifactModel):
    """Resolve: canonical nodes in ``cid`` order; ``merges`` and ``pair_candidates`` record the hub consolidation
    (identity keys stay in the artifact).
    """

    canonical: tuple[CanonicalNode, ...] = ()
    counters: ResolveCounters = ResolveCounters()
    merges: tuple[HubMerge, ...] = ()
    pair_candidates: tuple[PairCandidate, ...] = ()


# 4. Extract edges


class RawEdge(ArtifactModel):
    """Extract edges: one edge of one unit's call between canonical nodes."""

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
    """Units with an edge window whose call failed and that kept its last stored edges (same content,
    else same window identity; ``windows_fallback``)."""
    catalogs_kept: int = 0
    """Clean units of an ``update`` build whose catalog took its nearness hubs from their last stored
    catalog instead of ranking them again."""
    truncation_splits: int = 0
    attribution_fallbacks: int = 0
    outside_window_edges: int | None = None
    """Edges of ``attribution_fallbacks`` whose fact named fragments, all outside its edge window: kept on the window's
    fragments and reused by that window's content; None when not measured.
    """
    unknown_name_edges: int = 0
    """Facts the model returned between names outside the call's catalog (dropped)."""
    unknown_relation_edges: int = 0
    """Facts the model returned with a relation outside the ontology's edge types (dropped)."""
    self_loops: int = 0
    """Facts the model returned between a name and itself (dropped)."""
    windows_called: int | None = None
    """Edge windows whose edge call ran this build, answered or not; None when not measured."""
    windows_reused: int | None = None
    """Edge windows whose edges came from the ``ExtractionStore`` (clean, same window catalog); None as above."""
    windows_fallback: int | None = None
    """Edge windows whose call failed and that kept their last stored edges (same content, else same
    identity); None as above."""
    warm_ups: int | None = None
    """Per-unit edge warm-up calls (units with two or more windows called); None as above."""


EdgeWindowCause = Literal["first", "dirty", "catalog_changed", "not_stored"]
"""Why an edge window was called: every window of a ``first`` build or of a unit without a stored answer (``first``), a
window holding a dirty fragment (``dirty``), a clean window whose window catalog changed (``catalog_changed``), a clean
window with no stored answer to reuse (``not_stored``, e.g.
"""


class EdgeWindowCall(ArtifactModel):
    """Extract edges: one edge window whose call ran, by unit and ``(start, end)`` indexes into the
    unit's ``fragment_ids`` (end exclusive), and why it was called (None: not recorded)."""

    unit_id: str
    window: Window
    cause: EdgeWindowCause | None = None


class CatalogParts(ArtifactModel):
    """Extract edges: the foreign part of one unit's edge catalog by entry path, cids each: hubs its fragments mention
    (``mentioned``), hubs named only in a fragment's heading path (``heading``), hubs taken by nearness (``near``) and
    requirements its text references in another unit (``referenced``).
    """

    mentioned: tuple[str, ...] = ()
    heading: tuple[str, ...] = ()
    near: tuple[str, ...] = ()
    referenced: tuple[str, ...] = ()


class EdgesArtifact(ArtifactModel):
    """Extract edges: raw edges, attribute values by ``cid``, catalog ids per unit."""

    raw_edges: tuple[RawEdge, ...] = ()
    attributes: dict[str, dict[str, AttributeValue]] = {}
    attributes_failed: tuple[str, ...] = ()
    """Cids whose attributes batch failed (no values this build)."""
    catalogs: dict[str, tuple[str, ...]] = {}
    skipped: dict[str, str] = {}
    called_units: tuple[str, ...] = ()
    """Units with an edge call that ran this build (answered or not), in plan order."""
    edge_failed_fragments: tuple[str, ...] = ()
    """Every fragment (clean ones too) of the edge windows whose call failed without fallback edges
    (of the units, before edge windows), and the fragments a window's identity fallback did not hold,
    in plan order: their edges are not this build's (Diff keeps the current ones)."""
    recovered: tuple[str, ...] = ()
    """Fragments persisted ``skipped`` whose current content is now fully extracted: their nodes are
    stored for it and their unit's edges answered (or were reused) this build; persisted ``active``."""
    counters: EdgeCounters = EdgeCounters()
    called_windows: tuple[EdgeWindowCall, ...] | None = None
    """Edge windows whose call ran this build (answered or not), in plan order; None in an artifact written before edge
    windows (one call per unit: ``called_units``).
    """
    catalog_parts: dict[str, CatalogParts] | None = None
    """Per unit id, the foreign part of its edge catalog by entry path (``CatalogParts``); None in an artifact written
    before entry paths were recorded.
    """


# 5. Conform


class EvidenceLines(ArtifactModel):
    """Conform: source lines of one fragment holding a node's evidence (``REFERENCED_BY``)."""

    fragment_id: str
    line_start: int
    line_end: int


class StatementRec(ArtifactModel):
    """Conform: what one supporting fragment says about a node."""

    fragment_id: str
    document_id: str
    revision_id: str
    version: int
    text: str
    at: str


class TargetNode(ArtifactModel):
    """Conform: one grounded node of the target graph."""

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
    """The node's attributes batch failed: ``attributes`` are not this build's values; Diff keeps the
    persisted ones (and the persisted ``quote`` only when this node has none)."""


Basis = Literal["stated", "implied", "inferred"]
"""How a domain edge is known."""


class TargetEdge(ArtifactModel):
    """Conform: one legal domain edge between target nodes, by identity key."""

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
    derive_fallback_matches: int = 0
    """``derive`` references resolved by the whole-word fallback (Conform step 3), not by an exact name."""
    derive_fallback_refused: int = 0
    """``derive`` references the whole-word fallback found a hub for but refused (the hub is named in none
    of the source's evidence fragments)."""
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
    dedup_candidates_rich_only: int = 0
    """Dedup candidates found by the candidate (rich) vectors only, not by the name vectors."""
    dedup_unsure: int = 0
    """Dedup verdicts of ``low`` confidence: no merge, not stored."""
    dedup_verdicts_expired: int = 0
    """Stored dedup verdicts whose evidence hash no longer matches (skipped; judged again)."""
    dedup_union_refused: int = 0
    """Confirmed dedup pairs not applied because a pairwise guard refuses two members of the clusters
    they would join (a "5 s" and a "10 s" statement never meet through a third one)."""
    dedup_refused_collapsed: int = 0
    """Guard-refused dedup pairs whose ends still ended in one node (listed with ``collapsed``)."""
    owner_conflicts: int = 0
    """Normalisation (stage 1): owner and exclusive groups with more than one target."""
    owner_conflicts_judged: int = 0
    """Judge groups (owner conflicts and cycle breaks) sent to the judge in this build: fresh calls,
    failed and below the floor included; stored verdicts are in ``conform_judge_reused``."""
    redundant_edges: int = 0
    """Edges normalisation dropped as redundant (also in ``dropped_edges`` as ``redundant``)."""
    cycles_broken: int = 0
    """Edges taken out of a cycle of an ``acyclic`` relation (fallback relation or quarantine)."""
    unsupported_nodes: int = 0
    """Nodes dropped by ``supported_by`` (their edges in ``dropped_edges`` as ``unsupported``)."""
    derived_edges: int = 0
    """New ``implied`` edges of normalisation stage 8 (``derive_via``, ``id_references``)."""
    conform_judge_calls: int = 0
    """Fresh conform judge calls, failed and below the floor included (what ``conform.cap`` counts)."""
    conform_judge_reused: int = 0
    """Conform judge verdicts taken from the ``ExtractionStore`` instead of a call (below the floor
    included)."""
    conform_judge_failed: int = 0
    """Judge groups without a verdict (failed call, or no judge): the fallback relation."""
    conform_judge_below_floor: int = 0
    """Verdicts below ``conform.floor``, fresh or stored: the fallback relation."""
    conform_judge_over_cap: int = 0
    """Judge groups past ``conform.cap``: the fallback relation."""
    conform_judge_invalid_quotes: int = 0
    """``relates`` answers whose quotes were not found: the fallback relation."""
    in_scope_disagreements: int = 0
    """Merged nodes whose members gave different values for an attribute of the spine ``exclude``
    condition (the first value is kept unless ``in_scope_dirty_wins`` replaces it)."""
    in_scope_dirty_wins: int = 0
    """Of those, the merged nodes that took the value of a member grounded in a new or revised
    document of this build instead of the first value (T12; never in a ``first`` build, where every
    document is new)."""
    unsnapped_quote_nodes: dict[str, int] = {}
    """Grounding (``conform.GroundingStats``): canonical nodes per label without a snapped quote."""
    unsnapped_source_quotes: int = 0
    """Source quotes of canonical nodes that snapped nowhere."""
    provisional_keys_left: int = 0
    """Canonical nodes that kept a provisional identity key."""
    attributed_fragments_dropped: int = 0
    """Attributed fragments, summed over canonical nodes, holding none of the node's evidence."""
    nodes_missing_source_documents: int = 0
    """Canonical nodes attributed to a document that holds none of their evidence."""
    derive_suppressed_by_edge: int | None = None
    """``derive`` drafts dropped because their node already has a kept stated edge of that relation on an owner or
    ``exclusive`` pair; None when not measured.
    """
    foreign_edges: dict[str, dict[str, int]] | None = None
    """Raw edges with an end from outside their unit, per catalog entry path of that end (``mentioned``, ``heading``,
    ``near``, ``referenced``; ``CatalogParts``) and outcome (``FOREIGN_EDGE_OUTCOMES``: ``kept``, ``owner_conflict``,
    ``quarantined:<reason>``, ``dropped``); None when not measured.
    """
    oq_answer_judged: int | None = None
    """Open questions the answered-elsewhere check judged this build, fresh or stored ;
    None when the check did not run (no flag) or the artifact predates it."""
    oq_partly_kept: int | None = None
    """Of those, the ones answered ``partly``: kept; None as above."""
    oq_answered_dropped: int | None = None
    """Of those, the ones answered elsewhere (at or above the floor, quote found): dropped with their
    edges and listed in ``TargetGraph.answered_elsewhere``; None as above."""


FOREIGN_EDGE_OUTCOMES: Final = ("kept", "owner_conflict", "quarantined", "dropped")
"""Outcomes of a foreign-end raw edge in ``TargetCounters.foreign_edges``: kept as a target edge, lost
an owner conflict, quarantined (counted as ``quarantined:<reason>``), dropped otherwise."""


class QuarantineEntry(ArtifactModel):
    """An edge Conform or inference cannot keep: why, the relation it was extracted or proposed with, its end labels in
    that direction, its basis and evidence fragments.
    """

    reason: QuarantineReason
    relation: str
    source_label: str
    target_label: str
    basis: Basis
    fragment_ids: tuple[str, ...] = ()


class QuarantinedTargetEdge(QuarantineEntry):
    """Conform: a quarantined edge as the conform artifact stores it, its ends by identity key."""

    source_key: str
    target_key: str


class QuarantinedEdge(QuarantineEntry):
    """Build result: a quarantined edge, its ends by ``spec_id`` (never by identity key)."""

    source_spec_id: str | None = None
    target_spec_id: str | None = None


class RefusedDedupPair(ArtifactModel):
    """Conform: a requirement pair a pairwise dedup guard kept apart, its ends by identity key (surviving
    target nodes); the cosines of their name and candidate (rich) vectors (None: not computed)."""

    first_key: str
    second_key: str
    label: str
    guard: DedupGuard
    name_cosine: float
    rich_cosine: float | None = None
    collapsed: bool = False
    """Both ends ended in one node anyway: the ends keep their own keys (the node's key and aliases)."""


class DedupRefusal(ArtifactModel):
    """Build result: a guard-refused dedup pair (``RefusedDedupPair``), its ends by ``spec_id`` (never
    by identity key); None as for ``QuarantinedEdge``."""

    first_spec_id: str | None = None
    second_spec_id: str | None = None
    label: str
    guard: DedupGuard
    name_cosine: float
    rich_cosine: float | None = None
    collapsed: bool = False
    """Both ends ended in one node (``RefusedDedupPair.collapsed``): both ``spec_id``s are that node's."""


class AnsweredElsewhere(ArtifactModel):
    """Conform : an open question dropped because another document answers it, by the
    question's identity key, the fragment holding the answer and the answer's quote."""

    key: str
    fragment: str
    quote: str


class AnsweredElsewhereRef(ArtifactModel):
    """Build result: a node Conform dropped as answered in another document (``AnsweredElsewhere``), by ``spec_id``
    (never by identity key; None for a node never persisted), the fragment holding the answer and the answer's quote.
    """

    spec_id: str | None = None
    fragment_id: str
    quote: str


class TargetGraph(ArtifactModel):
    """Conform: the target graph, nodes by identity key, edges by edge key, and the quarantined edges ordered by reason,
    then source key, relation and target key.
    """

    nodes: tuple[TargetNode, ...] = ()
    edges: tuple[TargetEdge, ...] = ()
    counters: TargetCounters = TargetCounters()
    quarantine: tuple[QuarantinedTargetEdge, ...] = ()
    dedup_refused: tuple[RefusedDedupPair, ...] = ()
    """Requirement pairs a pairwise dedup guard refused (cluster-level refusals are only counted)."""
    derive_fallback_edges: tuple[str, ...] = ()
    """Edge keys of the target edges only the derive whole-word fallback gave (Conform step 3), for sampling; an edge
    normalisation re-keyed is not listed.
    """
    answered_elsewhere: tuple[AnsweredElsewhere, ...] = ()
    """Open questions the answered-elsewhere check dropped , by key; empty when none were
    dropped, the check did not run or the artifact predates it (``TargetCounters.oq_answer_judged``
    None tells the last two)."""


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
    """Persist phase 4: delete a node that lost all support, with its relationships."""

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
    """How reports name a domain edge: ``<source spec_id>|<RELATION>|<target spec_id>`` in its stored direction."""

    return f"{source_spec_id}|{relation}|{target_spec_id}"


class SpecChanges(ArtifactModel):
    """Diff and Infer: changed ``spec_id``s, and the inferred edges added and removed (``edge_ref`` form)."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    rekeyed: tuple[str, ...] = ()
    carried_over: tuple[str, ...] = ()
    inferred_added: tuple[str, ...] = ()
    inferred_removed: tuple[str, ...] = ()


EdgeChangeKind = Literal["added", "removed", "changed"]
"""How a domain edge changed (``ChangedEdge``): written new, deleted, or kept with changed evidence or
verdict."""


class ChangedEdge(ArtifactModel):
    """Diff and change report: one domain edge the build changed, by ``edge_ref``, with its basis and, for an inferred
    edge, its rule id.
    """

    ref: str
    change: EdgeChangeKind
    basis: Basis
    rule: str | None = None
    causes: tuple[str, ...] = ()


class DiffArtifact(ArtifactModel):
    """Diff: operations in persist phase order, spec changes and registry rows."""

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
    modified_causes: dict[str, int] | None = None
    """Why the nodes of ``spec_changes.modified`` are modified, nodes per cause, sorted: each build-owned property whose
    value changes (by name; ``updated_at`` and ``last_build_id``, set with any change, left out), ``vectors`` (a
    vector's text changed), ``links`` (evidence lines or mention counts changed), ``merged`` (other nodes merged into
    it).
    """
    edge_changes: tuple[ChangedEdge, ...] | None = None
    """The domain edges the diff adds, deletes or updates (``ChangedEdge``: created ones named by their ends'
    ``spec_id``s after the diff, deleted ones by those before; the inferred edges it deletes or turns stated, never one
    Infer writes), ordered by ``ref``; an update that only rewrites the edge key (an end rekeyed or carried over) is no
    change.
    """

    @model_validator(mode="after")
    def _phase_ordered(self) -> DiffArtifact:
        ranks = [PERSIST_PHASES.index(operation.phase) for operation in self.operations]
        if ranks != sorted(ranks):
            raise ValueError("operations must be in persist phase order")
        return self


class PersistCursor(ArtifactModel):
    """Persist: ``batch_index`` is the last applied batch (-1: none yet)."""

    batch_index: int = -1
    batches: int = 0
    done: bool = False


# 8. Infer and 9. Verify


RuleLimit = Literal["cap", "first_cap", "budget"]
"""The bound that stopped a rule's fresh judgements (``RuleReport.limit``)."""


class RuleReport(ArtifactModel):
    """Infer: counts of one inference rule."""

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
    items_by_outcome: dict[str, int] | None = None
    """Rules judging several items per subject (template ``cross_document``): judged items per outcome
    (``JudgedItem.outcome``), sorted; None for other rules and reports written before it.
    """
    duplicate_pairs: int | None = None
    """Template ``cross_document``: open member pairs left out of a subject because an earlier subject (most members
    first, then ``spec_id``) chose them, so no pair is judged under two hubs in one run; None for other rules and
    reports written before it.
    """
    limit: RuleLimit | None = None
    """The bound that stopped the rule's fresh judgements this build: ``cap``, ``first_cap`` (``first``/``rebuild``) or
    ``budget`` (the build's inference token budget); None when no bound was reached or in a report written before bounds
    were recorded.
    """
    budget_tokens: int | None = None
    """Estimated input tokens of the rule's fresh judge calls, counted before each call from the rendered messages (what
    the build budget is charged; reused judgements cost nothing); None in a report written before it.
    """
    backlog: int | None = None
    """Subjects left in the rule's queue after this build (``InferenceReport.backlog``); None in a report written before
    the queue existed.
    """
    read_edges: int | None = None
    """Rules with ``reads``: the inferred edges of the read rules (planned this build) that the rule's candidates were
    found over; None for other rules and reports written before it.
    """
    changed_queued: int | None = None
    """A build whose diff changed nothing: subjects judged before whose inputs changed (a read rule's edges moved as its
    queue drained), queued without a call for the next build; None in other builds and reports written before it.
    """


JudgedItemKind = Literal["link", "owner"]
JUDGED_ITEM_OUTCOMES: Final = (
    "accepted",
    "downgraded",
    "declined",
    "below_floor",
    "quote_missing",
    "invalid",
)
"""``JudgedItem.outcome`` values (``downgraded``: ``cross_document`` only; ``declined``: a verdict that
proposed no edge, one item without a target)."""


class BacklogQueue(ArtifactModel):
    """Infer: one rule's queue of subjects left unjudged by a bound, by ``spec_id``, in judging order (a failed
    judgement first); later builds drain it under the rule's ``cap``.
    """

    spec_ids: tuple[str, ...] = ()
    since_build: str | None = None
    age_builds: int = 0


class JudgedItem(ArtifactModel):
    """Infer: one item a judgement proposed, ends by ``spec_id`` (never by identity key) and no document text."""

    rule: str
    kind: JudgedItemKind
    subject: str | None = None
    source_spec_id: str | None = None
    relation: str
    target_spec_id: str | None = None
    outcome: str
    confidence: float | None = None
    fragment_ids: tuple[str, ...] = ()
    evidence: tuple[tuple[str, str], ...] | None = None
    read_edges: tuple[str, ...] | None = None


class InferenceReport(ArtifactModel):
    """Infer: per-rule counts and the lists names; ``error_code`` on failure."""

    enabled: bool = True
    rules: tuple[RuleReport, ...] = ()
    integration_outliers: tuple[str, ...] = ()
    orphans_without_candidate: tuple[str, ...] = ()
    spine_unreachable_before: int | None = None
    inferred_added: tuple[str, ...] = ()
    inferred_removed: tuple[str, ...] = ()
    error_code: str | None = None
    quarantine: tuple[QuarantinedEdge, ...] = ()
    judged_items: tuple[JudgedItem, ...] | None = None
    """The judged items (``JudgedItem``) of every rule (before of the rules judging several items per subject only),
    ordered by rule, subject, source, relation and target; None in a report written before it.
    """
    backlog: dict[str, BacklogQueue] | None = None
    """Per rule id with subjects left by a bound, its queue (``BacklogQueue``), in rule order; rules with an empty queue
    left out.
    """
    spine_unreachable_after: int | None = None
    """Spine sources that reach no root over the loaded graph plus the inferred edges this build plans (a dry run's
    projection too); Verify measures the persisted value (``VerificationMetrics.spine_unreachable``).
    """

    def backlog_counts(self) -> dict[str, int]:
        """Queued subjects per rule id with a queue (``backlog``), in rule order; empty without one
        (``Settlement.inference_backlog``)."""

        return {rule: len(queue.spec_ids) for rule, queue in (self.backlog or {}).items() if queue.spec_ids}


CheckName = Literal[
    "node_label",
    "spec_id",
    "illegal_edge",
    "ungrounded_node",
    "ungrounded_edge",
    "inferred_edge",
    "v2_rule",
    "fragment_state",
]
RepairAction = Literal[
    "set_label", "delete_node", "assign_spec_id", "delete_edge", "restore_evidence", "request_follow_up"
]


class Problem(ArtifactModel):
    """Verify: one blocking problem."""

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


class MetricFields(ArtifactModel):
    """Metric fields shared by the verify artifact and the consolidated metric set."""

    spine_unreachable_before: int | None = None
    exclusive_conflicts: int = 0
    label_disagreement: int = 0
    spine_unreachable_by_label: dict[str, int] | None = None
    quarantine_by_reason: dict[str, int] | None = None
    quarantine_by_pair: dict[str, int] | None = None
    """By ``<RELATION>|<source label>|<target label>``."""
    owner_conflicts_judged: int | None = None
    """Conform judge groups (owner conflicts and cycle breaks) decided by a verdict, fresh or stored."""
    cycles_broken: int | None = None
    interfaces_without_provider: int | None = None
    features_named_like_statements: int | None = None
    unsupported_entities_dropped: int | None = None
    entity_relates_fan_out: dict[int, int] | None = None
    relates_fan_out: dict[int, int] | None = None
    deployed_to_fan_out: dict[int, int] | None = None
    ownerless_requirements: int | None = None
    in_scope_disagreements: int | None = None
    in_scope_dirty_wins: int | None = None
    unsnapped_quote_nodes: dict[str, int] | None = None
    unsnapped_source_quotes: int | None = None
    provisional_keys_left: int | None = None
    attributed_fragments_dropped: int | None = None
    nodes_missing_source_documents: int | None = None
    label_collapses: int | None = None
    derive_fallback_matches: int | None = None
    derive_fallback_refused: int | None = None
    hub_surfaces_per_hub: dict[str, dict[str, float]] | None = None
    """Hub-name and known-term metrics (``graph.build.metrics.naming_metrics``), from this build's
        extract-nodes and resolve artifacts and plan."""
    hub_raw_names_per_hub: dict[str, dict[str, float]] | None = None
    hub_consolidation_merges: int | None = None
    catalog_batches: int | None = None
    catalog_calls: int | None = None
    catalog_failed: int | None = None
    catalog_reused: int | None = None
    catalog_truncation_splits: int | None = None
    catalog_hubs: int | None = None
    catalog_aliases: int | None = None
    catalog_links_refused: int | None = None
    catalog_aliases_dropped: int | None = None
    catalog_ambiguous: int | None = None
    known_terms_mentioned: dict[str, float] | None = None
    """``units``, ``mean`` and ``max`` per unit."""
    known_terms_nearby: dict[str, float] | None = None
    nearby_term_uses: int | None = None
    nearby_term_uses_unsupported: int | None = None
    nearby_term_uses_hubs: int | None = None
    nearby_term_uses_hubs_unsupported: int | None = None
    nearby_term_uses_statements: int | None = None
    nearby_term_uses_statements_unsupported: int | None = None
    alias_name_uses: int | None = None
    hub_merges_by_source: dict[str, int] | None = None
    """Identity metrics (``graph.build.metrics.IDENTITY_METRICS``): Resolve's hub merges per
    source (``block``, ``pair``, ``stored``), its pair-pass and decision counters and Conform's dedup
    counters; None for an artifact written without them."""
    pair_candidates_vector: int | None = None
    pair_candidates_alias: int | None = None
    pair_batches: int | None = None
    pair_calls: int | None = None
    pair_over_cap: int | None = None
    pair_merges: int | None = None
    alias_merges: int | None = None
    unsure_groups: int | None = None
    decisions_expired: int | None = None
    dedup_candidates_rich_only: int | None = None
    dedup_unsure: int | None = None
    dedup_verdicts_expired: int | None = None
    dedup_refused_by_guard: dict[str, int] | None = None
    owners_by_source: dict[str, int] | None = None
    """Edge metrics (``graph.build.metrics.edge_metrics``) on the persisted graph: owned requirements by the basis of
    their owner edge (``stated``, ``implied``, ``inferred``, ``none``) and edges whose ends share no document per
    relation and basis; None when not measured.
    """
    cross_document_edges: dict[str, dict[str, int]] | None = None
    """Relation to basis to count."""
    oq_answer_judged: int | None = None
    """Answered-elsewhere counts of the build's conform artifact ; None without them."""
    oq_partly_kept: int | None = None
    oq_answered_dropped: int | None = None


class VerificationMetrics(MetricFields):
    """None: no data for it (an older artifact, or an ontology without the flags the metric reads)."""

    spine_unreachable: int = 0
    dropped_edges: dict[str, int] = {}
    fragments_without_nodes: int = 0
    nodes_by_label: dict[str, int] = {}
    edges_by_relation_basis: dict[str, dict[str, int]] = {}
    spine_unreachable_before_by_label: dict[str, int] = {}
    redundant_after_inference: int | None = None


class VerificationReport(ArtifactModel):
    """Verify (step 9, ``verify_project``): problems found and left, repairs, metrics, ``ok``."""

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
    """Build result: "Build result" metrics; reported, never used as gates."""

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
    quarantine_by_reason: dict[str, int] | None = None
    """Quarantined edges per reason; None when the build's conform artifact records no quarantine."""
    quarantine_by_pair: dict[str, int] | None = None
    """Quarantined edges per ``<RELATION>|<source label>|<target label>``; None as above."""
    conform_judge_calls: int = 0
    """Fresh conform judge calls of the build (``TargetCounters.conform_judge_calls``)."""
    dedup_refused_by_guard: dict[str, int] | None = None
    """Guard-refused dedup pairs (``BuildResult.dedup_refused``) per guard; None when the build's conform artifact
    records none.
    """
    judge_calls: dict[str, int] | None = None
    """Judge calls of this ``run_build`` call per judging step (``hub_consolidation``, ``requirement_dedup``,
    ``conform_judge``, ``answered_check``, ``inference``; steps without one left out); None for a result written before
    they were counted.
    """
    judge_failed_calls: dict[str, int] | None = None
    """The failed ones among ``judge_calls`` (retries exhausted, malformed or refused answers), per step;
    a failed judge call decides nothing and the build goes on (reported, never a gate)."""


SettlementState = Literal["settled", "not_settled"]
SettlementReason = Literal["documents_incomplete", "inference_backlog", "inference_rule_failed"]


class Settlement(ArtifactModel):
    """Build result: whether the project is settled after this build and its follow-ups."""

    state: SettlementState = "settled"
    reasons: tuple[SettlementReason, ...] = ()
    documents: tuple[str, ...] = ()
    document_codes: dict[str, tuple[str, ...]] = {}
    inference_backlog: dict[str, int] = {}
    follow_up_builds: tuple[str, ...] = ()


ChangeClass = Literal[
    "added",
    "removed",
    "merged",
    "carried_over",
    "rekeyed",
    "changed_meaning",
    "changed_links",
    "changed_evidence",
]
"""Change-report node classes: ``changed_meaning``, its ``name`` or an ontology attribute other than ``quote`` changed,
or it gained, lost or changed an edge of an owner pair (any basis); ``changed_links``, it gained, lost or changed
another domain edge (any basis); ``changed_evidence``, its ``quote``, statements, evidence lines or mention counts
changed (lines that only move make ``changed_evidence``, never ``changed_meaning``).
"""


class ChangedNode(ArtifactModel):
    """Change report: one spec node the build changed, by ``spec_id``, its label, classes (``ChangeClass``, in that
    order), causes (changed property names, ``vectors``, ``links``, ``merged``; as ``DiffArtifact.modified_causes``)
    and, for a merge or rekey, the spec ids it replaces.
    """

    spec_id: str
    label: str
    classes: tuple[ChangeClass, ...]
    causes: tuple[str, ...] = ()
    previous_spec_ids: tuple[str, ...] = ()


class ChangeReport(ArtifactModel):
    """Build result: what the build changed, classified from its diff, inference and verification."""

    nodes: tuple[ChangedNode, ...] = ()
    edges: tuple[ChangedEdge, ...] = ()
    counts: dict[str, int] = {}
    edge_counts: dict[str, int] = {}


class BuildResult(ArtifactModel):
    """Build result; ``verification`` None until verify ran."""

    project_id: str
    build_id: str
    mode: BuildMode
    status: BuildStatus
    settled: bool = False
    spec_changes: SpecChanges = SpecChanges()
    quarantine: tuple[QuarantinedEdge, ...] = ()
    """Edges Conform could not keep, ends by ``spec_id``; stored in the ``BuildStore`` only."""
    dedup_refused: tuple[DedupRefusal, ...] = ()
    """Requirement pairs a dedup guard kept apart, ends by ``spec_id``; stored in the ``BuildStore`` only."""
    answered_elsewhere: tuple[AnsweredElsewhereRef, ...] = ()
    """Nodes Conform dropped as answered in another document, by ``spec_id``; stored in the ``BuildStore`` only; empty
    in a result written before it.
    """
    verification: VerificationSummary | None = None
    metrics: BuildMetrics = BuildMetrics()
    skipped_fragments: dict[str, str] = {}
    failed_step: StepName | None = None
    error_code: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    settlement: Settlement | None = None
    """Settled or not after this build and its follow-ups (``Settlement``); stored in the ``BuildStore`` only (the API
    keeps exposing ``settled``); None in a result written before it.
    """
    change_report: ChangeReport | None = None
    """What this build changed (``ChangeReport``), by ``spec_id``; stored in the ``BuildStore`` only; None in a result
    written before it.
    """


EstimateStep = Literal[
    "hub_catalog",
    "node_extraction",
    "hub_consolidation",
    "edge_extraction",
    "attributes",
    "requirement_dedup",
    "conform_judge",
    "answered_check",
    "inference",
    "cross_document_inference",
    "embeddings",
]
"""Estimated steps; ``inference``: the judge calls of every inference rule but ``cross_document`` ones,
``cross_document_inference``: of those.
"""


class StepEstimate(ArtifactModel):
    """Cost estimate: expected calls and input tokens of one step."""

    calls: int = 0
    input_tokens: int = 0


class BuildEstimate(ArtifactModel):
    """Cost estimate: plan and reuse outcome, calls and input tokens per step."""

    project_id: str
    mode: BuildMode
    change_set: ChangeSet
    fragments_by_change: dict[str, int] = {}
    fragments_by_status: dict[str, int] = {}
    dirty_fragments: int = 0
    units_to_extract: int = 0
    units_reused: int = 0
    steps: dict[EstimateStep, StepEstimate] = {}
    inference_backlog: dict[str, int] | None = None
    """Subjects per rule id the inference limits would leave queued after the build (rules
    without any left out); None in an estimate made before inference was estimated."""


class ProjectBuildStatus(ArtifactModel):
    """Building status of one project (read-only over the ``BuildStore``): ``building`` while its latest build runs,
    with that build's id, mode, running ``step``, start and ``follow_up_attempt`` (0: not a follow-up build);
    ``settlement``: the latest finished build's.
    """

    project_id: str
    building: bool = False
    build_id: str | None = None
    mode: BuildMode | None = None
    step: StepName | None = None
    started_at: AwareDatetime | None = None
    follow_up_attempt: int = 0
    settlement: Settlement | None = None


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
