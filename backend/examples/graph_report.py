"""Compare stored graph builds and write the TEST report."""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final, get_args

import yaml
from pydantic import BaseModel, ValidationError

from example_lib import run_example
from graph.build import FileStores, open_file_stores
from graph.build.change_report import classify
from graph.build.metrics import (
    EDGE_REF_PARTS,
    MEASURED_EDGE_METRICS,
    NEARBY_USE,
    SHARE_DIGITS,
    VERIFY_METRICS,
    ConsolidatedMetrics,
    StepArtifacts,
    confidence_band,
    consolidated_metrics,
    inferred_edges,
    node_documents,
    provider_pairs,
    stores_known_terms,
    term_uses,
)
from graph.build.models import (
    BuildResult,
    CanonicalNode,
    ChangeClass,
    DiffArtifact,
    EdgesArtifact,
    InferenceReport,
    NodesArtifact,
    PairCandidate,
    PlanArtifact,
    ResolveArtifact,
    TargetEdge,
    TargetGraph,
    TargetNode,
    VerificationReport,
)
from graph.build.resolve import hub_surface, hub_words
from graph.build.terms import NameIndex
from graph.build.vectors import VECTOR_KIND, Vector, decode_vector, get_many, text_hash
from graph.core import load_ontology
from graph.core.identity import SpecIdRow, normalise_surface
from graph.core.ontology import Ontology
from graph.core.ontology.model import CROSS_DOCUMENT
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("examples.graph_report")

MIN_NOISE_RUNS: Final = 2
"""Baseline runs a noise band needs."""
MIN_JACCARD: Final = 0.5
MIN_CONTAINMENT: Final = 0.5
"""Share of a left-over node's fragment weight another node must hold for a merge into it or a split from it."""
NAME_SIMILARITY: Final = 0.6
"""Least name similarity (``name_similarity``) of two nodes the matcher pairs, merges or splits."""
BUILD_ID: Final = re.compile(r"[0-9a-f]{32}")
"""Shape of a build id ``run_build`` issues (``uuid4().hex``)."""
ACTIVE_SPEC_ID: Final = "active"
REFERENCE_NAME_RATIO: Final = 0.8
"""Name similarity a build node needs to match a reference node by its fragment alone."""
FILLER_WORDS: Final = frozenset(
    {
        *("a", "an", "the", "of", "to", "and", "or", "for", "in", "on", "at", "by", "with", "from", "as", "is"),
        *("are", "be", "it", "its", "their", "each", "every", "all", "only", "no", "not", "never"),
        *("can", "cannot", "must", "shall", "should", "will", "may", "what", "which", "who", "where", "when"),
        *("system", "user"),
    }
)
"""English function words, modal and question words, and the actors that open requirement names
("System can ...", "User can ..."): left out of the reference fallback's name comparison."""
TOKEN_OVERLAP: Final = 0.5
"""Least ``token_overlap`` of the reference fallback match for renamed items (``reference_matches``)."""
MIN_SHARED_WORDS: Final = 2
"""Content words a fallback name pair must share."""
SHORT_NAME_WORDS: Final = 2
SHORT_NAME_OVERLAP: Final = 0.6
"""Overlap a short name may use instead of that shared-word count."""
FALLBACK_FILE: Final = "reference-fallback.csv"
FALLBACK_COLUMNS: Final = ("role", "label", "reference_name", "fragment", "node_name", "score")
SAMPLE_NODES: Final = 50
SAMPLE_EDGES: Final = 50
MAX_LISTED: Final = 50
"""Changes listed per kind in the markdown (the JSON holds them all)."""
TOP_DELTAS: Final = 10
SAMPLE_FILE: Final = "review-sample.csv"
BODY_ONLY_FILE: Final = "body-only-hubs.txt"
NEARBY_FILE: Final = "nearby-unsupported.csv"
NEARBY_COLUMNS: Final = ("row_id", "pass", "unit_id", "label", "name", "term", "fragments", "judgement", "note")
IDENTITY_FILES: Final = {
    "merged_pairs": "merged-pairs.csv",
    "near_unmerged_pairs": "near-unmerged-pairs.csv",
    "dedup_merges": "dedup-merges.csv",
    "refused_pairs": "refused-pairs.csv",
    "derive_fallback_edges": "derive-fallback-edges.csv",
}
"""Section 10 : the candidate's identity samples, one CSV each."""
IDENTITY_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "merged_pairs": ("row_id", "source", "confidence", "label", "node", "anchor", "member", "fragments"),
    "near_unmerged_pairs": (
        "row_id",
        "sample",
        "part",
        "source",
        "outcome",
        "name_cos",
        "rich_cos",
        "first",
        "second",
        "first_node",
        "second_node",
    ),
    "dedup_merges": ("row_id", "label", "node", "merged", "fragments", "statements"),
    "refused_pairs": (
        "row_id",
        "label",
        "guard",
        "name_cosine",
        "rich_cosine",
        "first",
        "second",
        "first_quote",
        "second_quote",
    ),
    "derive_fallback_edges": (
        "row_id",
        "relation",
        "source_label",
        "source",
        "source_quote",
        "target_label",
        "target",
        "fragments",
    ),
}
"""Columns of each identity sample before ``judgement`` and ``note``."""
EDGE_FILES: Final = {
    "near_edges": "near-edges.csv",
    "xdoc_items": "xdoc-items.csv",
    "answered_drops": "answered-drops.csv",
    "owner_provider_edges": "owner-provider-edges.csv",
}
"""Section 11 : the candidate's edge samples, one CSV each."""
EDGE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "near_edges": (
        "row_id",
        "unit",
        "relation",
        "near_end",
        "source_label",
        "source",
        "target_label",
        "target",
        "fact",
        "fragments",
        "kept",
    ),
    "xdoc_items": (
        "row_id",
        "rule",
        "kind",
        "outcome",
        "confidence",
        "relation",
        "subject",
        "source_label",
        "source",
        "target_label",
        "target",
        "fragments",
    ),
    "answered_drops": ("row_id", "label", "question", "key", "fragment", "quote"),
    "owner_provider_edges": (
        "row_id",
        "kind",
        "relation",
        "basis",
        "source_label",
        "source",
        "target_label",
        "target",
        "cross_document",
        "fragments",
    ),
}
"""Columns of each edge sample before ``judgement`` and ``note``."""
OWNER_PROVIDER_SAMPLE: Final = 20
"""Owner and provider edges sampled per kind, relation and basis (stable random order)."""
XDOC_ITEM_SAMPLE: Final = 40
"""Accepted cross-document items sampled (stable random order)."""
XDOC_ACCEPTED: Final = frozenset({"accepted", "downgraded"})
"""``JudgedItem.outcome`` values of an item written to the graph (``downgraded``: under the rule's
``writes`` relation)."""
OWNER_KIND: Final = "owner"
PROVIDER_KIND: Final = "provider"
EMBEDDING_MODEL_PROPERTY: Final = "embedding_model"
"""The node property naming the embedding model of a build's vectors (diff node operations)."""
NEAR_TOP: Final = 15
NEAR_RANDOM: Final = 15
"""Near unmerged pairs sampled: the closest by cosine (candidate vector, else name) and a stable
pseudo-random share of the others."""
STATEMENT_SEPARATOR: Final = " || "
MULTI_MATCH: Final = 2
INFERENCE_FILES: Final = {
    "inferred_edges": "inferred-edges.csv",
    "change_report_nodes": "change-report-nodes.csv",
}
"""Section 12 : the candidate's inference and change-report samples, one CSV each."""
INFERENCE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "inferred_edges": (
        "row_id",
        "rule",
        "band",
        "kind",
        "outcome",
        "confidence",
        "relation",
        "subject",
        "source_label",
        "source",
        "target_label",
        "target",
        "fragments",
    ),
    "change_report_nodes": (
        "row_id",
        "class",
        "classes",
        "causes",
        "spec_id",
        "previous_spec_ids",
        "label",
        "name",
    ),
}
"""Columns of each section 12 sample before ``judgement`` and ``note``."""
INFERRED_PER_BAND: Final = 40
"""Accepted inferred items sampled per rule and confidence band (stable random order)."""
CHANGED_PER_CLASS: Final = 30
"""Change-report nodes sampled per class (stable random order; a node sampled once, under its first
class in ``CHANGE_CLASSES`` order that still has room)."""
CHANGE_CLASSES: Final = get_args(ChangeClass)
INFERENCE_STEPS: Final = ("inference", "cross_document_inference")
"""The estimate steps of inference : ``cross_document`` rules, and every other rule."""
NOT_ESTIMATED: Final = "not_estimated:"
"""Prefix ``graph_eval.py`` gives the actual usage of a prompt no estimate step covers."""
"""Target nodes matching one reference hub from which it counts as matched more than once."""
SUMMARY_KEYS: Final = ("nodes", "edges", "node_changes", "edge_changes", "unexplained_changes")
"""Summary fields the script prints."""
ROLES: Final = ("original", "baseline", "candidate")
"""Fixed roles; further baseline runs are ``baseline2``, ``baseline3``, ... (``ordered_roles``)."""
BASELINE_ROLE: Final = re.compile(r"baseline(?:[2-9]|[1-9]\d+)?")
NA: Final = "n/a"
NO_DIFFERENCE: Final = "no difference"
WITHIN_RANGE: Final = "no difference (within the baseline range)"
WITHIN_NOISE: Final = "no difference (|delta| <= noise)"
SAME: Final = frozenset({NO_DIFFERENCE, WITHIN_RANGE, WITHIN_NOISE})
UNEXPLAINED: Final = "unexplained"
MERGE_EXPLANATION: Final = "merge (Resolve)"
"""A merge the candidate's artifacts show: its Resolve (or Conform) folded the merged-away nodes' names or
identity keys into the merged node."""
MERGE_UNKNOWN: Final = "merged (cause unknown)"
SPLIT_EXPLANATION: Final = "split: the baseline's Resolve had merged them"
SPLIT_UNKNOWN: Final = "split (cause unknown)"
NOT_EXPLAINED: Final = frozenset({UNEXPLAINED, MERGE_UNKNOWN, SPLIT_UNKNOWN})
"""Explanations the summary counts as unexplained."""
NEW_TYPE_TOPICS: Final = "T5/T13/T17"
JUDGEMENTS: Final = ("correct", "wrong", "unsure")
BLANK: Final = "blank"
NODE_KINDS: Final = ("new", "gone", "relabelled", "merged", "split")
EDGE_KINDS: Final = ("new", "gone", "relation_changed", "direction_changed")
SIZE_PREFIXES: Final = (
    "nodes_by_label",
    "edges_by_relation_basis",
    "inferred_added_by_relation",
    "edges_by_relation_persisted",
)
NOT_NUMERIC: Final = frozenset({"metric_sources"})
"""Consolidated fields that are no metric (the builds table shows where the Verify metrics came from)."""
RECOMPUTED_SUFFIX: Final = "(artifacts)"
QUARANTINE_FIELDS: Final = ("quarantine_by_reason", "quarantine_by_pair")
QUARANTINE_KEYS: Final = frozenset({"source_key", "target_key"})
"""Fields of a conform quarantine entry naming its ends by identity key (kept out of the report)."""
QUARANTINE_TOPICS: Final = {
    "unknown_relation": "T1",
    "illegal_pair": "T1",
    "cue_missing": "T1",
    "locality": "T1",
    "condition_failed": "T15",
    "external_owner": "T15",
    "owner_conflict": "T2/T3",
    "judge_rejected": "T2",
    "depth_cut": "T16",
    "cycle_broken": "T7",
    "invalid_proposal": "P8.4",
    "self_loop": "T3/T7/T9",
    "redundant": "T3/T7/T9",
    "unsupported": "T17",
}
"""Quarantine reason (and counted-only drop) to the decision topic that explains it."""
REVIEW_COLUMNS: Final = (
    "row_id",
    "kind",
    "side",
    "change",
    "label",
    "name",
    "source",
    "target",
    "fragments",
    "explanation",
    "judgement",
    "note",
)


# Stored builds


@dataclass(frozen=True)
class BuildSpec:
    """Where a build is stored: state folder, project id, build id (None: latest finished)."""

    state: Path
    project: str
    build_id: str | None = None


@dataclass(frozen=True)
class StoredBuild:
    """One build as the report reads it: conform artifact, result, plan and quarantine entries (raw,
    ends by ``spec_id``: ``quarantine_entries``)."""

    role: str
    project: str
    build_id: str
    target: TargetGraph
    result: BuildResult | None = None
    plan: PlanArtifact | None = None
    quarantine: tuple[dict[str, Any], ...] = ()
    labels: frozenset[str] = frozenset()
    """Labels of the ontology the build ran with (new types are judged against the baseline's)."""
    spec_ids: dict[str, str] = field(default_factory=dict)
    """Active ``spec_id`` by identity key, from the project's registry (its current state)."""
    resolved: dict[str, frozenset[str]] = field(default_factory=dict)
    """What Resolve put into each canonical node, by its identity key and aliases: the normalised names of
    its raw nodes and the identity keys it folded in (``resolved_members``)."""
    ontology: str = ""
    """The ontology the build is measured with: name, version and file."""
    resolve_artifact: ResolveArtifact | None = None
    edges_artifact: EdgesArtifact | None = None
    """The resolve and extract-edges artifacts, when they load."""
    nodes_artifact: NodesArtifact | None = None
    """The extract-nodes artifact, when it loads (``label_collapses``)."""
    diff_artifact: DiffArtifact | None = None
    inference_artifact: InferenceReport | None = None
    """The diff and inference artifacts, when they load (edge metrics, section 11)."""
    vectors: dict[str, Vector] | None = None
    """Search ``embedding`` vectors of the target nodes by identity key, read from the extraction store;
    None when the build's embedding model is unknown (``orphans_best_elsewhere`` n/a)."""
    change_report_offline: bool = False
    """The result's change report was classified offline (``offline_change_report``): the result was written before
    change reports were.
    """

    def steps(self) -> StepArtifacts:
        """The stored artifacts the metrics read besides the conform artifact."""

        return StepArtifacts(
            resolved=self.resolve_artifact,
            edges=self.edges_artifact,
            nodes=self.nodes_artifact,
            diff=self.diff_artifact,
            inference=self.inference_artifact,
            spec_ids=self.spec_ids,
            vectors=self.vectors,
        )


def parse_build_spec(text: str) -> BuildSpec:
    """``STATE:PROJECT[:BUILD_ID]``, split from the right so the state path may hold ``:``; the last part
    is a build id only when it has the shape ``run_build`` gives one (``BUILD_ID``)."""

    head, _, last = text.rpartition(":")
    if BUILD_ID.fullmatch(last):
        state, _, project = head.rpartition(":")
        if state and project:
            return BuildSpec(Path(state), project, last)
    elif head and last:
        return BuildSpec(Path(head), last)
    raise argparse.ArgumentTypeError(f"expected STATE:PROJECT[:BUILD_ID], got {text!r}")


def target_graph(payload: Mapping[str, Any]) -> tuple[TargetGraph, tuple[dict[str, Any], ...]]:
    """The conform artifact and its raw quarantine entries: ends by identity key (``source_key``, ``target_key``), as
    Conform stores them.
    """

    raw = payload.get("quarantine") or ()
    return TargetGraph.model_validate(payload), tuple(item for item in raw if isinstance(item, dict))


def quarantine_entries(raw: Iterable[Mapping[str, Any]], spec_ids: Mapping[str, str]) -> tuple[dict[str, Any], ...]:
    """Raw quarantine entries as the report reads them: ends by ``spec_id`` (``source_spec_id``, ``target_spec_id``;
    from ``spec_ids`` by identity key, None when unknown), identity keys left out (they can hold document text).
    """

    entries = []
    for item in raw:
        entry = {name: value for name, value in item.items() if name not in QUARANTINE_KEYS}
        for end in ("source", "target"):
            if entry.get(f"{end}_spec_id") is None:
                entry[f"{end}_spec_id"] = spec_ids.get(str(item.get(f"{end}_key")))
        entries.append(entry)
    return tuple(entries)


def resolved_members(resolve: Mapping[str, Any] | None, nodes: Mapping[str, Any] | None) -> dict[str, frozenset[str]]:
    """From the raw resolve and extract-nodes artifacts (read as JSON, so a change of their models does
    not break the report): per canonical node, by its identity key and each alias, the normalised names
    of its raw nodes and its aliases."""

    if not resolve:
        return {}
    names = {raw.get("raw_id"): norm_name(str(raw.get("name", ""))) for raw in (nodes or {}).get("raw_nodes") or ()}
    found: dict[str, set[str]] = defaultdict(set)
    for canonical in resolve.get("canonical") or ():
        aliases = [str(alias) for alias in canonical.get("identity_aliases") or ()]
        members = {names[raw] for raw in canonical.get("member_ids") or () if raw in names} | set(aliases)
        for key in (str(canonical.get("identity_key", "")), *aliases):
            found[key] |= members
    return {key: frozenset(value) for key, value in sorted(found.items())}


async def load_build(spec: BuildSpec, role: str, ontology: Ontology, described: str = "") -> StoredBuild:
    """Read one build from its file stores; its latest finished build when ``spec`` names none.
    ``described``: the ontology's name, version and file, for the report header."""

    stores = open_file_stores(spec.state)
    builds = stores.builds
    build_id = spec.build_id
    if build_id is None:
        record = await builds.latest_build(spec.project, finished_only=True)
        if record is None:
            raise ValueError(f"no finished build of {spec.project!r} under {spec.state}")
        build_id = record.build_id
    payload = await builds.get_artifact(spec.project, build_id, "conform")
    if payload is None:
        raise ValueError(f"build {build_id!r} of {spec.project!r} has no conform artifact")
    target, raw_quarantine = target_graph(payload)
    plan_payload = await builds.get_artifact(spec.project, build_id, "plan")
    rows = await stores.registry.taken(spec.project)
    spec_ids = {row.identity_key: row.spec_id for _, row in sorted(rows.items()) if row.state == ACTIVE_SPEC_ID}
    resolve = await builds.get_artifact(spec.project, build_id, "resolve")
    nodes = await builds.get_artifact(spec.project, build_id, "extract_nodes")
    edges = await builds.get_artifact(spec.project, build_id, "extract_edges")
    diff_artifact = _loaded(DiffArtifact, await builds.get_artifact(spec.project, build_id, "diff"), build_id)
    inference = _loaded(InferenceReport, await builds.get_artifact(spec.project, build_id, "infer"), build_id)
    verification = _loaded(VerificationReport, await builds.get_artifact(spec.project, build_id, "verify"), build_id)
    stored = await builds.get_result(spec.project, build_id)
    result = offline_change_report(stored, diff_artifact, inference, verification, ontology=ontology, rows=rows)
    model = embedding_model(diff_artifact)
    return StoredBuild(
        role=role,
        project=spec.project,
        build_id=build_id,
        target=target,
        result=result,
        plan=PlanArtifact.model_validate(plan_payload) if plan_payload is not None else None,
        quarantine=quarantine_entries(raw_quarantine, spec_ids),
        labels=frozenset(ontology.labels),
        spec_ids=spec_ids,
        resolved=resolved_members(resolve, nodes),
        ontology=described,
        resolve_artifact=_loaded(ResolveArtifact, resolve, build_id),
        edges_artifact=_loaded(EdgesArtifact, edges, build_id),
        nodes_artifact=_loaded(NodesArtifact, nodes, build_id),
        diff_artifact=diff_artifact,
        inference_artifact=inference,
        vectors=None if model is None else await node_vectors(stores, spec.project, model, target),
        change_report_offline=result is not stored,
    )


def offline_change_report(
    result: BuildResult | None,
    diff_artifact: DiffArtifact | None,
    inference: InferenceReport | None,
    verification: VerificationReport | None,
    *,
    ontology: Ontology,
    rows: Mapping[str, SpecIdRow],
) -> BuildResult | None:
    """``result`` as stored, or, when it records no change report for a build that did not fail, with one classified
    offline from its diff, inference and verification artifacts (``graph.build.change_report.classify``; the ends of an
    older diff's edge operations through the registry ``rows``, its current state).
    """

    if result is None or result.change_report is not None or result.status == "failed" or diff_artifact is None:
        return result
    report = classify(diff_artifact, inference, verification, ontology, registry=rows)
    return result.model_copy(update={"change_report": report})


def embedding_model(diff_artifact: DiffArtifact | None) -> str | None:
    """The embedding model the build's diff wrote on its node operations; None when it names none."""

    for operation in diff_artifact.operations if diff_artifact is not None else ():
        value = getattr(operation, "properties", {}).get(EMBEDDING_MODEL_PROPERTY)
        if isinstance(value, str) and value:
            return value
    return None


async def node_vectors(stores: FileStores, project: str, model: str, target: TargetGraph) -> dict[str, Vector]:
    """The search ``embedding`` vector of each target node the extraction store holds, by identity key
    (key and decoding as ``graph.build.vectors.VectorCache``, read only)."""

    texts = {node.identity_key: node.embedding_text for node in target.nodes if node.embedding_text}
    keys = {node: f"{project}|{model}|{text_hash(text)}" for node, text in texts.items()}
    found = await get_many(stores.extraction, VECTOR_KIND, sorted(set(keys.values())), model)
    out: dict[str, Vector] = {}
    for node, key in keys.items():
        payload = found.get(key)
        dim = payload.get("dim") if payload is not None else None
        vector = decode_vector(payload, dim) if payload is not None and isinstance(dim, int) else None
        if vector is not None:
            out[node] = vector
    return out


def _loaded[T: BaseModel](model: type[T], payload: Mapping[str, Any] | None, build_id: str) -> T | None:
    """``payload`` as ``model``, or None when there is none or it does not fit the current model (logged
    with the reason: the metrics that need it read n/a)."""

    if payload is None:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        log_event(
            logger,
            WARNING,
            "example.graph_report.artifact_unreadable",
            build_id=build_id,
            model=model.__name__,
            errors=error.error_count(),
            reason="; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors()[:3]),
        )
        return None


def load_eval(text: str) -> tuple[str, dict[str, Any]]:
    """``ROLE=PATH[#LABEL]``: the role and that build of a ``graph_eval.py --out`` file (default: its last)."""

    role, _, spec = text.partition("=")
    if not (role in ROLES or is_baseline(role)) or not spec:
        raise argparse.ArgumentTypeError(
            f"expected ROLE=PATH[#LABEL] with ROLE in {ROLES} or baseline<N>, got {text!r}"
        )
    path, _, label = spec.partition("#")
    builds = json.loads(Path(path).read_text(encoding="utf-8")).get("builds") or {}
    if not builds:
        raise argparse.ArgumentTypeError(f"{path}: no builds")
    chosen = label or list(builds)[-1]
    if chosen not in builds:
        raise argparse.ArgumentTypeError(f"{path}: no build {chosen!r}")
    return role, builds[chosen]


# Node and edge matching


@dataclass(frozen=True)
class NodeRef:
    key: str
    label: str
    name: str
    fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeRef:
    relation: str
    source: NodeRef
    target: NodeRef
    basis: str
    fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeChange:
    """``kind``: new, gone, relabelled, merged (n -> 1) or split (1 -> n)."""

    kind: str
    before: tuple[NodeRef, ...]
    after: tuple[NodeRef, ...]
    explanation: str


@dataclass(frozen=True)
class EdgeChange:
    """``kind``: new, gone, relation_changed or direction_changed."""

    kind: str
    before: EdgeRef | None
    after: EdgeRef | None
    explanation: str


@dataclass(frozen=True)
class NodeMatching:
    """``pairs``: one-to-one matches, baseline key to candidate key. ``merged``: baseline nodes merged
    away, to the candidate node they went into; ``split``: candidate nodes split off, to the baseline
    node they came from."""

    pairs: dict[str, str]
    merged: dict[str, str]
    split: dict[str, str]


@dataclass(frozen=True)
class Comparison:
    """How the candidate's graph differs from the baseline's."""

    pairs: dict[str, str]
    groups: tuple[dict[str, str], dict[str, str]]
    nodes: tuple[NodeChange, ...]
    edges: tuple[EdgeChange, ...]
    unchanged_edges: tuple[tuple[EdgeRef, EdgeRef], ...] = ()


def norm_name(name: str) -> str:
    return normalise_surface(name)


def fragments_of(node: TargetNode) -> frozenset[str]:
    return frozenset(lines.fragment_id for lines in node.evidence)


def node_ref(node: TargetNode) -> NodeRef:
    return NodeRef(node.identity_key, node.label, node.name, tuple(sorted(fragments_of(node))))


def jaccard(left: Collection[str], right: Collection[str]) -> float:
    union = len(set(left) | set(right))
    return len(set(left) & set(right)) / union if union else 0.0


def name_similarity(left: str, right: str) -> float:
    """Similarity of two names: 1 when their normalised forms are equal, else the larger of the
    difflib ratio and the Jaccard of their word sets (so word order does not count against a pair)."""

    left, right = norm_name(left), norm_name(right)
    if left == right:
        return 1.0
    words = (set(left.split()), set(right.split()))
    return max(difflib.SequenceMatcher(None, left, right).ratio(), jaccard(*words))


def fragment_weights(baseline: TargetGraph, candidate: TargetGraph) -> dict[str, float]:
    """``1 / n`` per fragment, ``n`` the most nodes either build grounds in it: a fragment many nodes
    share says little about which of them is which (IDF-style)."""

    counts: Counter[str] = Counter()
    for graph in (baseline, candidate):
        side = Counter(fid for node in graph.nodes for fid in fragments_of(node))
        for fid, count in side.items():
            counts[fid] = max(counts[fid], count)
    return {fid: 1 / count for fid, count in counts.items()}


@dataclass(frozen=True)
class _Overlap:
    """One baseline and one candidate node sharing evidence: weighted Jaccard, the share of each one's
    fragment weight the other holds, and their name similarity."""

    before: TargetNode
    after: TargetNode
    score: float
    before_in_after: float
    after_in_before: float
    similarity: float

    @property
    def similar(self) -> bool:
        return self.similarity >= NAME_SIMILARITY

    def likeness(self) -> tuple[Any, ...]:
        """Tie-breaks: same label, same normalised name, more similar name, keys."""

        return (
            self.before.label != self.after.label,
            self.similarity < 1,
            -self.similarity,
            self.before.identity_key,
            self.after.identity_key,
        )


def _overlapping(baseline: TargetGraph, candidate: TargetGraph) -> list[_Overlap]:
    """Every (baseline, candidate) node pair that shares an evidence fragment, by fragment weight."""

    weights = fragment_weights(baseline, candidate)

    def weight(fragments: Iterable[str]) -> float:
        return sum(weights[fid] for fid in fragments)

    by_fragment: dict[str, list[TargetNode]] = defaultdict(list)
    for node in candidate.nodes:
        for fid in fragments_of(node):
            by_fragment[fid].append(node)
    found: list[_Overlap] = []
    for before in baseline.nodes:
        mine = fragments_of(before)
        seen: set[str] = set()
        for fid in sorted(mine):
            for after in by_fragment.get(fid, ()):
                if after.identity_key in seen:
                    continue
                seen.add(after.identity_key)
                theirs = fragments_of(after)
                shared, union = weight(mine & theirs), weight(mine | theirs)
                found.append(
                    _Overlap(
                        before,
                        after,
                        shared / union,
                        shared / weight(mine),
                        shared / weight(theirs),
                        name_similarity(before.name, after.name),
                    )
                )
    return found


def _pair_greedily(ranked: Iterable[_Overlap], pairs: dict[str, str], taken: set[str]) -> None:
    for item in ranked:
        before, after = item.before.identity_key, item.after.identity_key
        if before not in pairs and after not in taken:
            pairs[before] = after
            taken.add(after)


def _best(found: dict[str, tuple[tuple[Any, ...], str]], key: str, rank: tuple[Any, ...], value: str) -> None:
    if key not in found or rank < found[key][0]:
        found[key] = (rank, value)


def match_nodes(baseline: TargetGraph, candidate: TargetGraph) -> NodeMatching:
    """Match nodes by their weighted evidence fragments and names (module docstring): one to one by best weighted
    Jaccard >= ``MIN_JACCARD`` among pairs with similar names (``NAME_SIMILARITY``), then same label and normalised name
    with a shared fragment; the similar-named nodes left over go by weighted containment >= ``MIN_CONTAINMENT`` into the
    node of the other side holding most of their fragments (merged or split); a merge or split of a single node into an
    unmatched one is a match.
    """

    overlapping = [item for item in _overlapping(baseline, candidate) if item.similar]
    pairs: dict[str, str] = {}
    taken: set[str] = set()
    by_score = sorted(
        (item for item in overlapping if item.score >= MIN_JACCARD), key=lambda item: (-item.score, *item.likeness())
    )
    _pair_greedily(by_score, pairs, taken)
    same_name = [item for item in overlapping if item.before.label == item.after.label and item.similarity == 1]
    _pair_greedily(sorted(same_name, key=lambda item: (-item.score, *item.likeness())), pairs, taken)

    merged: dict[str, tuple[tuple[Any, ...], str]] = {}
    for item in overlapping:
        if item.before.identity_key not in pairs and item.before_in_after >= MIN_CONTAINMENT:
            rank = (-item.before_in_after, -item.score, *item.likeness())
            _best(merged, item.before.identity_key, rank, item.after.identity_key)
    into = {key: value[1] for key, value in merged.items()}
    targets = set(into.values())
    split: dict[str, tuple[tuple[Any, ...], str]] = {}
    for item in overlapping:
        after, before = item.after.identity_key, item.before.identity_key
        if after in taken or after in targets or before in into or item.after_in_before < MIN_CONTAINMENT:
            continue
        _best(split, after, (-item.after_in_before, -item.score, *item.likeness()), before)
    out_of = {key: value[1] for key, value in split.items()}

    # A merge or split of a single node into an unmatched one is a match whose fragments drifted.
    partner = set(pairs.values())
    for target, members in _grouped(into).items():
        if target not in partner and len(members) == 1:
            pairs[members[0]] = target
            del into[members[0]]
    for source, parts in _grouped(out_of).items():
        if source not in pairs and len(parts) == 1:
            pairs[source] = parts[0]
            del out_of[parts[0]]
    return NodeMatching(dict(sorted(pairs.items())), dict(sorted(into.items())), dict(sorted(out_of.items())))


def _grouped(mapping: Mapping[str, str]) -> dict[str, list[str]]:
    """``key -> value`` inverted to ``value -> sorted keys``, by value."""

    groups: dict[str, list[str]] = defaultdict(list)
    for key, value in sorted(mapping.items()):
        groups[value].append(key)
    return dict(sorted(groups.items()))


def _absorbed(build: StoredBuild, node: TargetNode) -> frozenset[str]:
    """Identity keys and normalised names ``build`` folded into ``node``: its identity aliases (Conform,
    Resolve) and what its Resolve put into the canonical node with its key (``StoredBuild.resolved``)."""

    found = set(node.identity_aliases)
    for key in (node.identity_key, *node.identity_aliases):
        found |= build.resolved.get(key, frozenset())
    return frozenset(found)


def _folded(node: TargetNode, absorbed: Collection[str]) -> bool:
    return node.identity_key in absorbed or norm_name(node.name) in absorbed


def _new_type(label: str, known: Collection[str]) -> str | None:
    return None if label in known else f"relabel into new type {label} ({NEW_TYPE_TOPICS})"


def compare(baseline: StoredBuild, candidate: StoredBuild, ontology: Ontology | None = None) -> Comparison:
    """Match ``candidate``'s graph against ``baseline``'s and explain each change (module docstring)."""

    before_nodes = {node.identity_key: node for node in baseline.target.nodes}
    after_nodes = {node.identity_key: node for node in candidate.target.nodes}
    known = baseline.labels or frozenset(node.label for node in baseline.target.nodes)
    matching = match_nodes(baseline.target, candidate.target)
    pairs = matching.pairs
    partner = {after: before for before, after in pairs.items()}
    before_group = dict(pairs)
    after_group = {after: after for after in pairs.values()}
    changes: list[NodeChange] = []
    # (side, key) of a node merged away, split off, gone or new: its change kind and explanation.
    regrouped: dict[tuple[str, str], tuple[str, str]] = {}

    for into, members in _grouped(matching.merged).items():
        absorbed = _absorbed(candidate, after_nodes[into])
        shown = all(_folded(before_nodes[key], absorbed) for key in members)
        explanation = MERGE_EXPLANATION if shown else MERGE_UNKNOWN
        for key in members:
            before_group[key] = into
            regrouped[("before", key)] = ("merged", explanation)
        after_group[into] = into
        everyone = sorted({*members, *([partner[into]] if into in partner else [])})
        merged_refs = tuple(node_ref(before_nodes[key]) for key in everyone)
        changes.append(NodeChange("merged", merged_refs, (node_ref(after_nodes[into]),), explanation))
    for source, parts in _grouped(matching.split).items():
        absorbed = _absorbed(baseline, before_nodes[source])
        explanation = SPLIT_EXPLANATION if all(_folded(after_nodes[key], absorbed) for key in parts) else SPLIT_UNKNOWN
        group = pairs.get(source, parts[0])
        before_group[source] = group
        for key in parts:
            after_group[key] = group
            regrouped[("after", key)] = ("split", explanation)
        everyone = sorted({*parts, *([pairs[source]] if source in pairs else [])})
        split_refs = tuple(node_ref(after_nodes[key]) for key in everyone)
        changes.append(NodeChange("split", (node_ref(before_nodes[source]),), split_refs, explanation))
    in_groups = {*_grouped(matching.merged), *(pairs[source] for source in matching.split.values() if source in pairs)}
    for before_key, after_key in pairs.items():
        before, after = before_nodes[before_key], after_nodes[after_key]
        if before.label != after.label and after_key not in in_groups:
            explanation = _new_type(after.label, known) or UNEXPLAINED
            changes.append(NodeChange("relabelled", (node_ref(before),), (node_ref(after),), explanation))
    for key in sorted(set(before_nodes) - set(before_group)):
        changes.append(NodeChange("gone", (node_ref(before_nodes[key]),), (), UNEXPLAINED))
        regrouped[("before", key)] = ("gone", UNEXPLAINED)
    for key in sorted(set(after_nodes) - set(after_group)):
        node = after_nodes[key]
        explanation = f"new type {node.label} ({NEW_TYPE_TOPICS})" if node.label not in known else UNEXPLAINED
        changes.append(NodeChange("new", (), (node_ref(node),), explanation))
        regrouped[("after", key)] = ("new", explanation)

    edge_changes, unchanged = _compare_edges(baseline, candidate, (before_group, after_group), regrouped, ontology)
    return Comparison(
        pairs=pairs,
        groups=(before_group, after_group),
        nodes=tuple(sorted(changes, key=_node_change_key)),
        edges=tuple(edge_changes),
        unchanged_edges=tuple(unchanged),
    )


def _node_change_key(change: NodeChange) -> tuple[Any, ...]:
    first = (change.before or change.after)[0]
    return (change.kind, first.label, norm_name(first.name), first.key)


def _symmetric(relation: str, ontology: Ontology | None) -> bool:
    return ontology is not None and relation in ontology.relations and ontology.is_symmetric(relation)


def _edge_ref(edge: TargetEdge, nodes: Mapping[str, TargetNode]) -> EdgeRef:
    return EdgeRef(
        edge.relation,
        node_ref(nodes[edge.source_key]),
        node_ref(nodes[edge.target_key]),
        edge.basis,
        tuple(sorted(edge.fragment_ids)),
    )


def _edge_sort(ref: EdgeRef) -> tuple[str, ...]:
    return (ref.relation, ref.source.label, norm_name(ref.source.name), ref.target.label, norm_name(ref.target.name))


def _edge_change_key(change: EdgeChange) -> tuple[str, ...]:
    ref = change.before if change.before is not None else change.after
    return (change.kind, *_edge_sort(ref)) if ref is not None else (change.kind,)


EdgeKey = tuple[str, str, str]
"""Relation and its ends' group ids; a symmetric relation's ends in sorted order."""


def _keyed_edges(
    build: StoredBuild, group: Mapping[str, str], side: str, ontology: Ontology | None
) -> dict[EdgeKey, EdgeRef]:
    """The build's edges by ``EdgeKey`` (an unmatched end keys by its side and own key)."""

    nodes = {node.identity_key: node for node in build.target.nodes}
    keyed: dict[EdgeKey, EdgeRef] = {}
    for edge in sorted(build.target.edges, key=lambda item: item.edge_key):
        if edge.source_key not in nodes or edge.target_key not in nodes:
            continue
        ends = [group.get(key, f"{side}:{key}") for key in (edge.source_key, edge.target_key)]
        if _symmetric(edge.relation, ontology):
            ends.sort()
        keyed.setdefault((edge.relation, ends[0], ends[1]), _edge_ref(edge, nodes))
    return keyed


def _same_ends_other_relation(gone: EdgeKey, new: EdgeKey, ontology: Ontology | None) -> bool:
    """Another relation between the same ends: in the same direction, or either way when one of the
    relations is symmetric."""

    if gone[0] == new[0]:
        return False
    if _symmetric(gone[0], ontology) or _symmetric(new[0], ontology):
        return {gone[1], gone[2]} == {new[1], new[2]}
    return gone[1:] == new[1:]


def _compare_edges(
    baseline: StoredBuild,
    candidate: StoredBuild,
    groups: tuple[dict[str, str], dict[str, str]],
    regrouped: Mapping[tuple[str, str], tuple[str, str]],
    ontology: Ontology | None,
) -> tuple[list[EdgeChange], list[tuple[EdgeRef, EdgeRef]]]:
    """Unchanged, gone, new, direction-changed and relation-changed edges, each change explained."""

    before = _keyed_edges(baseline, groups[0], "before", ontology)
    after = _keyed_edges(candidate, groups[1], "after", ontology)
    unchanged = [(before[key], after[key]) for key in sorted(set(before) & set(after))]
    new = sorted(set(after) - set(before))
    by_ends: dict[frozenset[str], list[EdgeKey]] = defaultdict(list)
    for key in new:
        by_ends[frozenset(key[1:])].append(key)
    left = set(new)
    changes: list[EdgeChange] = []
    for key in sorted(set(before) - set(after)):
        ref = before[key]
        quarantined = _quarantine_explanation(ref, key, baseline, candidate, groups[0], ontology=ontology)
        flipped = (key[0], key[2], key[1])
        if not _symmetric(key[0], ontology) and flipped in left:
            left.discard(flipped)
            changes.append(EdgeChange("direction_changed", ref, after[flipped], quarantined or UNEXPLAINED))
            continue
        other = next(
            (
                item
                for item in by_ends.get(frozenset(key[1:]), ())
                if item in left and _same_ends_other_relation(key, item, ontology)
            ),
            None,
        )
        if other is not None:
            left.discard(other)
            changes.append(EdgeChange("relation_changed", ref, after[other], quarantined or UNEXPLAINED))
            continue
        explanation = quarantined or _end_explanation(ref, regrouped, "before") or UNEXPLAINED
        changes.append(EdgeChange("gone", ref, None, explanation))
    for key in sorted(left):
        ref = after[key]
        changes.append(EdgeChange("new", None, ref, _end_explanation(ref, regrouped, "after") or UNEXPLAINED))
    changes.sort(key=_edge_change_key)
    return changes, unchanged


def _end_explanation(ref: EdgeRef, regrouped: Mapping[tuple[str, str], tuple[str, str]], side: str) -> str | None:
    """The change of an end that was merged away, split off, gone or new (not of a surviving partner)."""

    for end in (ref.source, ref.target):
        found = regrouped.get((side, end.key))
        if found is not None:
            kind, explanation = found
            return f"end node {kind}: {explanation}"
    return None


def _quarantine_explanation(
    ref: EdgeRef,
    key: EdgeKey,
    baseline: StoredBuild,
    candidate: StoredBuild,
    group: Mapping[str, str],
    *,
    ontology: Ontology | None,
) -> str | None:
    """The reason of the candidate's quarantine entry for the baseline edge ``ref``: same relation, a shared fragment
    (required), and the same ends: by ``spec_id`` when the entry carries them and the ends' ``spec_id``s are known
    (baseline registry, or the candidate's for the matched nodes), else by the baseline edge's end labels.
    """

    ends = (ref.source.key, ref.target.key)
    known: set[tuple[str, str]] = set()
    for spec_ids, keys in ((baseline.spec_ids, ends), (candidate.spec_ids, tuple(group.get(end, "") for end in ends))):
        source, target = spec_ids.get(keys[0]), spec_ids.get(keys[1])
        if source and target:
            known.add((source, target))
            if _symmetric(key[0], ontology):
                known.add((target, source))
    for entry in candidate.quarantine:
        fragments = set(entry.get("fragment_ids") or ())
        if entry.get("relation") != ref.relation or not fragments & set(ref.fragments):
            continue
        by_ids = (entry.get("source_spec_id"), entry.get("target_spec_id"))
        if all(by_ids) and known:
            if by_ids not in known:
                continue
        elif (entry.get("source_label"), entry.get("target_label")) != (ref.source.label, ref.target.label):
            continue
        reason = str(entry.get("reason"))
        return f"quarantine: {reason} ({QUARANTINE_TOPICS.get(reason, '?')})"
    return None


# Metrics, deltas and noise


@dataclass(frozen=True)
class MetricRow:
    """One numeric metric: value per build role (None: n/a), delta, noise band and verdict."""

    name: str
    values: dict[str, float | None]
    delta: float | None = None
    noise: float | None = None
    verdict: str = NA
    delta_original: float | None = None
    source: str = ""
    """Where a Verify metric's values came from, per role in role order (``metric_sources``)."""


def flatten(metrics: ConsolidatedMetrics | None) -> dict[str, float | None]:
    """Numeric leaves by dotted name; a None field (or no metrics) is one None leaf."""

    if metrics is None:
        return {}
    out: dict[str, float | None] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for key in sorted(value):
                walk(f"{prefix}.{key}", value[key])
        elif isinstance(value, int | float) or value is None:
            out[prefix] = value

    for name in sorted(set(ConsolidatedMetrics.model_fields) - NOT_NUMERIC):
        walk(name, getattr(metrics, name))
    return out


def noise_band(*runs: float | None) -> float | None:
    """The range (max - min) of the baseline runs; None unless at least two runs have a value."""

    values = [run for run in runs if run is not None]
    return max(values) - min(values) if len(values) >= MIN_NOISE_RUNS else None


def verdict(
    delta: float | None, noise: float | None, candidate: float | None = None, runs: Sequence[float] = ()
) -> str:
    """The verdict on a delta: no difference when the candidate lies within the baseline runs' range
    (``WITHIN_RANGE``, preferred), else when ``|delta|`` is within the noise band (``WITHIN_NOISE``),
    else when it is zero; otherwise higher or lower."""

    if delta is None:
        return NA
    if candidate is not None and len(runs) >= MIN_NOISE_RUNS and min(runs) <= candidate <= max(runs):
        return WITHIN_RANGE
    if noise is not None and abs(delta) <= noise:
        return WITHIN_NOISE
    if delta == 0:
        return NO_DIFFERENCE
    return "higher" if delta > 0 else "lower"


def is_baseline(role: str) -> bool:
    return BASELINE_ROLE.fullmatch(role) is not None


def ordered_roles(roles: Iterable[str]) -> list[str]:
    """``original``, the baseline runs in run order, ``candidate``; unknown roles are left out."""

    found = set(roles)
    runs = sorted((role for role in found if is_baseline(role)), key=lambda role: int(role[len("baseline") :] or 1))
    return [*(["original"] if "original" in found else []), *runs, *(["candidate"] if "candidate" in found else [])]


def _value(flat: Mapping[str, float | None], name: str, *, present: bool) -> float | None:
    """A leaf's value; a leaf missing under a present (non-None) mapping field counts 0."""

    if name in flat:
        return flat[name]
    return 0.0 if present else None


def metric_rows(metrics: Mapping[str, ConsolidatedMetrics | None]) -> list[MetricRow]:
    """One row per numeric leaf of any role's metrics, in name order; before is ``baseline``."""

    flats = {role: flatten(value) for role, value in metrics.items()}
    names = sorted({name for flat in flats.values() for name in flat})
    parents = {name.split(".")[0] for name in names if "." in name}
    rows = []
    for name in names:
        if name in parents:  # a mapping None in one role, filled in another: its leaves carry the n/a
            continue
        top = name.split(".")[0]
        values: dict[str, float | None] = {}
        for role in ordered_roles(metrics):
            item = metrics[role]
            present = item is not None and getattr(item, top) is not None and "." in name
            values[role] = _value(flats[role], name, present=present)
        before, after = values.get("baseline"), values.get("candidate")
        delta = None if before is None or after is None else after - before
        runs = [value for role, value in values.items() if is_baseline(role) and value is not None]
        noise = noise_band(*runs)
        original = values.get("original")
        rows.append(
            MetricRow(
                name=name,
                values=values,
                delta=delta,
                noise=noise,
                verdict=verdict(delta, noise, after, runs),
                delta_original=None if original is None or after is None else after - original,
                source=_source(metrics, top),
            )
        )
    return rows


def _source(metrics: Mapping[str, ConsolidatedMetrics | None], field_name: str) -> str:
    """The sources of a Verify metric in role order, one value when they all agree; empty for others."""

    if field_name not in (*VERIFY_METRICS, *MEASURED_EDGE_METRICS):
        return ""
    found = [
        item.metric_sources.get(field_name, NA) if item is not None else NA
        for role in ordered_roles(metrics)
        for item in (metrics[role],)
    ]
    return found[0] if len(set(found)) == 1 else " / ".join(found)


def recomputed_rows(
    primary: Sequence[MetricRow], recomputed: Mapping[str, ConsolidatedMetrics | None]
) -> list[MetricRow]:
    """Rows of the Verify metrics measured on the artifacts (``--recompute-metrics``) where some role's value differs
    from ``primary``'s, named ``<metric> (artifacts)``.
    """

    shown = {row.name: row for row in primary}
    out = []
    for row in metric_rows(recomputed):
        if row.name.split(".")[0] not in (*VERIFY_METRICS, *MEASURED_EDGE_METRICS):
            continue
        before = shown.get(row.name)
        if before is None or before.values != row.values:
            out.append(replace(row, name=f"{row.name} {RECOMPUTED_SUFFIX}"))
    return out


# Precision and recall against a reference set


@dataclass(frozen=True)
class Reference:
    nodes: tuple[dict[str, str], ...] = ()
    edges: tuple[dict[str, str], ...] = ()


def load_reference(path: Path) -> Reference:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes = tuple({key: str(value) for key, value in item.items()} for item in data.get("nodes") or ())
    edges = tuple({key: str(value) for key, value in item.items()} for item in data.get("edges") or ())
    return Reference(nodes, edges)


def _matches_reference(node: TargetNode, item: Mapping[str, str]) -> bool:
    if node.label != item.get("label"):
        return False
    left, right = norm_name(node.name), norm_name(item.get("name", ""))
    if left == right:
        return True
    fragment = item.get("fragment")
    return (
        bool(fragment)
        and fragment in fragments_of(node)
        and (difflib.SequenceMatcher(None, left, right).ratio() >= REFERENCE_NAME_RATIO)
    )


def _reference_end(reference: Reference, edge: Mapping[str, str], side: str) -> int | None:
    """The reference node an edge end names: by label and normalised name when the edge gives the
    end's label (``source_label``/``target_label``), else by normalised name (the first one)."""

    name, label = norm_name(edge.get(side, "")), edge.get(f"{side}_label")
    for index, item in enumerate(reference.nodes):
        if norm_name(item.get("name", "")) == name and (not label or item.get("label") == label):
            return index
    return None


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _scores(reference: int, found: int, in_scope: int, correct: int) -> dict[str, Any]:
    """Counts with precision and recall; both None (n/a) when the reference has no item of the type."""

    return {
        "reference": reference,
        "found": found,
        "recall": _ratio(found, reference),
        "in_scope": in_scope,
        "correct": correct,
        "precision": _ratio(correct, in_scope) if reference else None,
    }


def content_words(name: str) -> list[str]:
    """A name's words in hub form (case and plurals folded, ``-``/``_`` as spaces) without
    ``FILLER_WORDS``."""

    return [word for word in hub_words(name) if word not in FILLER_WORDS]


def token_overlap(left: str, right: str) -> float:
    """Shared content words (``content_words``) of two names over those of the shorter one; 0 when
    either has none."""

    words = set(content_words(left)), set(content_words(right))
    smaller = min(len(words[0]), len(words[1]))
    return len(words[0] & words[1]) / smaller if smaller else 0.0


def _hub_surface_contains(left: str, right: str) -> bool:
    """Whether the hub surface words of the shorter name equal the longer one's as a set (another
    order, hyphenation, plurals) or start it ("Claude" / "Claude clients"); a leading qualifier names
    a narrower thing ("Web app" / "Admin web app": refused)."""

    words = sorted((hub_words(hub_surface(left)), hub_words(hub_surface(right))), key=len)
    shorter, longer = words
    return bool(shorter) and (set(shorter) == set(longer) or longer[: len(shorter)] == shorter)


def _fallback_score(node: TargetNode, item: Mapping[str, str], hub_labels: Collection[str] = ()) -> float | None:
    """The fallback score of a node for a reference item (``reference_matches``): the larger of the ``name_similarity``
    of their content words and their ``token_overlap``, when the node has the item's label and holds its fragment among
    its evidence, the similarity reaches ``NAME_SIMILARITY`` and the overlap ``TOKEN_OVERLAP``, they share
    ``MIN_SHARED_WORDS`` content words (or the shorter has at most ``SHORT_NAME_WORDS`` and the overlap reaches
    ``SHORT_NAME_OVERLAP``), and, for a hub label, one hub surface contains the other (``_hub_surface_contains``); else
    None.
    """

    fragment = item.get("fragment")
    if node.label != item.get("label") or not fragment or fragment not in fragments_of(node):
        return None
    name = item.get("name", "")
    words = set(content_words(node.name)), set(content_words(name))
    shared, shorter = len(words[0] & words[1]), min(len(words[0]), len(words[1]))
    similarity = name_similarity(" ".join(content_words(node.name)), " ".join(content_words(name)))
    overlap = token_overlap(node.name, name)
    enough = shared >= MIN_SHARED_WORDS or (shorter <= SHORT_NAME_WORDS and overlap >= SHORT_NAME_OVERLAP)
    if similarity < NAME_SIMILARITY or overlap < TOKEN_OVERLAP or not enough:
        return None
    if node.label in hub_labels and not _hub_surface_contains(node.name, name):
        return None
    return max(similarity, overlap)


@dataclass(frozen=True)
class FallbackMatch:
    """A reference item matched by the renamed-item fallback: its index, the node and the score."""

    index: int
    key: str
    score: float


def reference_matching(
    target: TargetGraph, reference: Reference, hub_labels: Collection[str] = ()
) -> tuple[dict[str, list[int]], list[FallbackMatch]]:
    """Reference node indexes each target node matches, by identity key, and the fallback matches among them."""

    matched = {
        node.identity_key: [i for i, item in enumerate(reference.nodes) if _matches_reference(node, item)]
        for node in target.nodes
    }
    found = {index for hits in matched.values() for index in hits}
    taken = {key for key, hits in matched.items() if hits}
    fallbacks: list[FallbackMatch] = []
    for index, item in enumerate(reference.nodes):
        if index in found:
            continue
        scored = sorted(
            (
                (score, node.identity_key)
                for node in target.nodes
                if node.identity_key not in taken and (score := _fallback_score(node, item, hub_labels)) is not None
            ),
            reverse=True,
        )
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            score, key = scored[0]
            matched[key].append(index)
            taken.add(key)
            fallbacks.append(FallbackMatch(index, key, round(score, 3)))
    return matched, fallbacks


def reference_matches(
    target: TargetGraph, reference: Reference, hub_labels: Collection[str] = ()
) -> dict[str, list[int]]:
    """The reference node indexes each target node matches (``reference_matching``)."""

    return reference_matching(target, reference, hub_labels)[0]


def precision_recall(target: TargetGraph, reference: Reference, ontology: Ontology | None = None) -> dict[str, Any]:
    """Per label and per relation: reference items found (recall) and build items in the reference's scope that are in
    it (precision).
    """

    scope = {item["fragment"] for item in reference.nodes if item.get("fragment")}
    matched, fallbacks = reference_matching(target, reference, ontology.hub_labels if ontology else ())
    nodes_out: dict[str, dict[str, Any]] = {}
    for label in sorted({item.get("label", "") for item in reference.nodes} | {n.label for n in target.nodes}):
        refs = [i for i, item in enumerate(reference.nodes) if item.get("label") == label]
        found = sum(1 for i in refs if any(i in hits for hits in matched.values()))
        in_scope = [n for n in target.nodes if n.label == label and (not scope or fragments_of(n) & scope)]
        correct = sum(1 for n in in_scope if matched[n.identity_key])
        nodes_out[label] = _scores(len(refs), found, len(in_scope), correct)
    wanted: set[tuple[str, int, int]] = set()
    for item in reference.edges:
        source, target_index = _reference_end(reference, item, "source"), _reference_end(reference, item, "target")
        if source is not None and target_index is not None:
            wanted.add((item.get("relation", ""), source, target_index))
    edges_out: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, int, int]] = set()
    in_scope_edges: Counter[str] = Counter()
    correct_edges: Counter[str] = Counter()
    for edge in target.edges:
        ends = matched.get(edge.source_key) or [], matched.get(edge.target_key) or []
        if not ends[0] or not ends[1]:
            continue
        in_scope_edges[edge.relation] += 1
        symmetric = _symmetric(edge.relation, ontology)
        hits = {
            (edge.relation, s, t)
            for s in ends[0]
            for t in ends[1]
            if (edge.relation, s, t) in wanted or (symmetric and (edge.relation, t, s) in wanted)
        }
        hits = {(r, s, t) if (r, s, t) in wanted else (r, t, s) for r, s, t in hits}
        if hits:
            correct_edges[edge.relation] += 1
            seen |= hits
    for relation in sorted({item.get("relation", "") for item in reference.edges} | set(in_scope_edges)):
        edge_refs = [key for key in wanted if key[0] == relation]
        found = sum(1 for key in edge_refs if key in seen)
        edges_out[relation] = _scores(len(edge_refs), found, in_scope_edges[relation], correct_edges[relation])
    names = {node.identity_key: node.name for node in target.nodes}
    rows = [
        {
            "label": reference.nodes[match.index].get("label", ""),
            "reference_name": reference.nodes[match.index].get("name", ""),
            "fragment": reference.nodes[match.index].get("fragment", ""),
            "node_name": names[match.key],
            "score": match.score,
        }
        for match in fallbacks
    ]
    by_label = Counter(row["label"] for row in rows)
    return {
        "nodes": nodes_out,
        "edges": edges_out,
        "fallback": dict(sorted(by_label.items())),
        "fallback_matches": rows,
    }


# Hub names


@dataclass(frozen=True)
class HubSubsets:
    """Reference hubs (items of a hub label) split by where their name occurs: in no document title and
    no heading of the plan (``body_only``), else ``heading``; reference node indexes, in order."""

    body_only: tuple[int, ...]
    heading: tuple[int, ...]

    @property
    def every(self) -> tuple[int, ...]:
        return tuple(sorted((*self.body_only, *self.heading)))


def heading_texts(plan: PlanArtifact) -> list[str]:
    """Every document title and every heading of a fragment's heading path, each once, in plan order."""

    texts = [document.title for document in plan.documents]
    texts += [heading for fragment in plan.fragments for heading in fragment.heading_path]
    return list(dict.fromkeys(text for text in texts if text.strip()))


def hub_subsets(reference: Reference, plan: PlanArtifact, hub_labels: Collection[str]) -> HubSubsets:
    """The body-only and heading reference hubs (``HubSubsets``): a name occurs in a title or heading
    when its words in hub form (``NameIndex``: ``resolve.hub_surface``, plurals and case folded) occur
    there contiguously. Deterministic: same reference and plan, same subsets."""

    headings = [hub_words(text) for text in heading_texts(plan)]
    body_only: list[int] = []
    heading: list[int] = []
    for index, item in enumerate(reference.nodes):
        if item.get("label") not in hub_labels:
            continue
        names = NameIndex([(item.get("name", ""), str(index))])
        named = bool(names) and any(names.count(words) for words in headings)
        (heading if named else body_only).append(index)
    return HubSubsets(tuple(body_only), tuple(heading))


def _recall(indexes: Collection[int], found: Collection[int]) -> dict[str, Any]:
    hits = sum(1 for index in indexes if index in found)
    return {"reference": len(indexes), "found": hits, "recall": _ratio(hits, len(indexes))}


def subset_recall(
    target: TargetGraph, reference: Reference, subsets: HubSubsets, hub_labels: Collection[str] = ()
) -> dict[str, Any]:
    """Recall (``reference_matches``) of the body-only, heading and every reference hub, and the reference
    hubs two or more target nodes match (``Label: name (n)``, in reference order)."""

    matched = reference_matches(target, reference, hub_labels)
    counts: Counter[int] = Counter(index for hits in matched.values() for index in hits)
    multi = [
        f"{reference.nodes[index].get('label')}: {reference.nodes[index].get('name')} ({counts[index]})"
        for index in subsets.every
        if counts[index] >= MULTI_MATCH
    ]
    return {
        "body_only": _recall(subsets.body_only, counts),
        "heading": _recall(subsets.heading, counts),
        "all": _recall(subsets.every, counts),
        "multi_matched": len(multi),
        "multi_matched_hubs": multi,
    }


def catalog_recall(nodes: NodesArtifact | None, reference: Reference, subsets: HubSubsets) -> dict[str, Any] | None:
    """Recall of the body-only, heading and every reference hub in the build's hub catalog: the hub
    surface of the reference name (``resolve.hub_surface``) equals that of a catalog name or alias (any
    label). None when the build stored no catalog (n/a)."""

    if nodes is None or not nodes.hub_catalog:
        return None
    surfaces = {hub_surface(name) for hub in nodes.hub_catalog for name in (hub.name, *hub.aliases)}
    found = {index for index in subsets.every if hub_surface(reference.nodes[index].get("name", "")) in surfaces}
    return {
        "body_only": _recall(subsets.body_only, found),
        "heading": _recall(subsets.heading, found),
        "all": _recall(subsets.every, found),
    }


def nearby_unsupported_rows(build: StoredBuild, ontology: Ontology) -> list[dict[str, str]] | None:
    """The build's nearby-term uses its fragments do not support (``metrics.term_uses``: hub nodes against the hub
    pass's terms, statements whose name contains a nearby term against the statement pass's), as rows of ``NEARBY_FILE``
    (one per statement and term), in a stable pseudo-random order (the first 20 are the over-application sample); None
    when the build stored no known terms or has no plan.
    """

    nodes = build.nodes_artifact
    if nodes is None or build.plan is None or not stores_known_terms(nodes):
        return None
    found = term_uses(nodes, ontology, build.plan, build.resolve_artifact)
    uses = [use for use in found if use.kind == NEARBY_USE and use.supported is False]
    uses.sort(key=lambda use: (_stable(f"{use.raw_id}|{use.term}"), use.raw_id, use.term))
    return [
        {
            "row_id": str(number),
            "pass": use.node_pass,
            "unit_id": use.unit_id,
            "label": use.label,
            "name": use.name,
            "term": use.term,
            "fragments": " ".join(use.fragment_ids),
            "judgement": "",
            "note": "",
        }
        for number, use in enumerate(uses, start=1)
    ]


def hub_names(
    builds: Mapping[str, StoredBuild], ontologies: Mapping[str, Ontology], reference: Reference | None
) -> dict[str, Any]:
    """Section 9: the reference hub subsets (on the candidate's plan and ontology), recall per subset and
    run, catalog recall, reference hubs matched by two or more nodes, and the candidate's unsupported
    nearby-term uses (``NEARBY_FILE``)."""

    candidate = builds["candidate"]
    ontology = ontologies["candidate"]
    roles = ordered_roles(builds)
    subsets = (
        hub_subsets(reference, candidate.plan, ontology.hub_labels)
        if reference is not None and candidate.plan is not None
        else None
    )
    rows = nearby_unsupported_rows(candidate, ontology)
    out: dict[str, Any] = {
        "hub_labels": sorted(ontology.hub_labels),
        "plan": candidate.build_id if subsets is not None else None,
        "body_only_hubs": None,
        "heading_hubs": None,
        "recall": None,
        "catalog_recall": None,
        "nearby_unsupported": {"file": NEARBY_FILE if rows is not None else None, "rows": rows},
    }
    if reference is None or subsets is None:
        return out
    out["body_only_hubs"] = [
        {"label": reference.nodes[index].get("label", ""), "name": reference.nodes[index].get("name", "")}
        for index in subsets.body_only
    ]
    out["heading_hubs"] = len(subsets.heading)
    out["recall"] = {
        role: subset_recall(builds[role].target, reference, subsets, ontologies[role].hub_labels) for role in roles
    }
    out["catalog_recall"] = {role: catalog_recall(builds[role].nodes_artifact, reference, subsets) for role in roles}
    return out


# Identity samples (section 10)


def _owners(resolved: ResolveArtifact) -> dict[str, CanonicalNode]:
    """Canonical nodes by identity key and each identity alias."""

    return {key: node for node in resolved.canonical for key in (node.identity_key, *node.identity_aliases)}


def _numbered(rows: Iterable[tuple[str, dict[str, str]]], prefix: str) -> list[dict[str, str]]:
    """Rows in a stable pseudo-random order (by their sort text), numbered, ``judgement`` and ``note`` empty."""

    ordered = sorted(rows, key=lambda item: (_stable(item[0]), item[0]))
    return [
        {"row_id": f"{prefix}{index:03d}", **row, "judgement": "", "note": ""}
        for index, (_, row) in enumerate(ordered, start=1)
    ]


def merged_pair_rows(resolved: ResolveArtifact | None) -> list[dict[str, str]] | None:
    """Resolve's hub merges (``ResolveArtifact.merges``) as rows, stable random order (judge the first
    40); None when the artifact records no merges although its consolidation merged."""

    if resolved is None or "merges" not in resolved.model_fields_set:
        return None
    if not resolved.merges and resolved.counters.consolidation.merges:
        return None
    owners = _owners(resolved)
    rows = []
    for merge in resolved.merges:
        node = owners.get(merge.anchor_key)
        rows.append(
            (
                f"{merge.anchor_key}|{merge.member_key}",
                {
                    "source": merge.source,
                    "confidence": merge.confidence or "",
                    "label": node.label if node else "",
                    "node": node.name if node else "",
                    "anchor": merge.anchor_key,
                    "member": merge.member_key,
                    "fragments": " ".join(node.fragment_ids) if node else "",
                },
            )
        )
    return _numbered(rows, "M")


def near_unmerged_rows(
    resolved: ResolveArtifact | None, top: int = NEAR_TOP, spread: int = NEAR_RANDOM
) -> list[dict[str, str]] | None:
    """The pair candidates (``ResolveArtifact.pair_candidates``) whose ends stayed in different canonical nodes: the
    ``top`` closest (candidate-vector cosine, else name cosine) and ``spread`` others in a stable pseudo-random order;
    None when the artifact records no pair candidates field.
    """

    if resolved is None or "pair_candidates" not in resolved.model_fields_set:
        return None
    owners = _owners(resolved)

    def apart(candidate: PairCandidate) -> bool:
        first, second = owners.get(candidate.key_a), owners.get(candidate.key_b)
        return first is None or second is None or first.identity_key != second.identity_key

    unmerged = [candidate for candidate in resolved.pair_candidates if apart(candidate)]
    unmerged.sort(key=lambda c: (-(c.rich_cos if c.rich_cos is not None else c.name_cos), c.key_a, c.key_b))
    chosen = [("top", candidate) for candidate in unmerged[:top]]
    rest = sorted(unmerged[top:], key=lambda c: (_stable(f"{c.key_a}|{c.key_b}"), c.key_a, c.key_b))
    chosen += [("random", candidate) for candidate in rest[:spread]]
    rows = []
    for index, (sample, candidate) in enumerate(chosen, start=1):
        first, second = owners.get(candidate.key_a), owners.get(candidate.key_b)
        rows.append(
            {
                "row_id": f"P{index:03d}",
                "sample": sample,
                "part": candidate.part,
                "source": candidate.source,
                "outcome": candidate.outcome,
                "name_cos": f"{candidate.name_cos:.3f}",
                "rich_cos": "" if candidate.rich_cos is None else f"{candidate.rich_cos:.3f}",
                "first": candidate.key_a,
                "second": candidate.key_b,
                "first_node": f"{first.label}: {first.name}" if first else "",
                "second_node": f"{second.label}: {second.name}" if second else "",
                "judgement": "",
                "note": "",
            }
        )
    return rows


def dedup_merge_rows(target: TargetGraph, ontology: Ontology) -> list[dict[str, str]]:
    """Conform's requirement dedup merges as rows (stable random order): one per identity alias of a
    target node outside the hub labels (dedup folds the other statements' keys into the survivor's
    aliases; hub aliases come from Resolve), with the survivor's statements."""

    rows = [
        (
            f"{node.identity_key}|{alias}",
            {
                "label": node.label,
                "node": node.name,
                "merged": alias,
                "fragments": " ".join(item.fragment_id for item in node.evidence),
                "statements": STATEMENT_SEPARATOR.join(statement.text for statement in node.statements),
            },
        )
        for node in target.nodes
        if node.label not in ontology.hub_labels
        for alias in node.identity_aliases
    ]
    return _numbered(rows, "D")


def refused_pair_rows(target: TargetGraph) -> list[dict[str, str]] | None:
    """The guard-refused dedup pairs of the conform artifact (``TargetGraph.dedup_refused``) as rows,
    stable random order (judge the first 20); None for an artifact written without the list."""

    if "dedup_refused" not in target.model_fields_set:
        return None
    nodes = {node.identity_key: node for node in target.nodes}
    rows = []
    for refused in target.dedup_refused:
        first, second = nodes.get(refused.first_key), nodes.get(refused.second_key)
        rows.append(
            (
                f"{refused.first_key}|{refused.second_key}|{refused.guard}",
                {
                    "label": refused.label,
                    "guard": refused.guard,
                    "name_cosine": f"{refused.name_cosine:.3f}",
                    "rich_cosine": "" if refused.rich_cosine is None else f"{refused.rich_cosine:.3f}",
                    "first": first.name if first else refused.first_key,
                    "second": second.name if second else refused.second_key,
                    "first_quote": (first.quote or "") if first else "",
                    "second_quote": (second.quote or "") if second else "",
                },
            )
        )
    return _numbered(rows, "R")


def derive_fallback_rows(target: TargetGraph) -> list[dict[str, str]] | None:
    """The edges only the derive fallback gave (``TargetGraph.derive_fallback_edges``) as rows, stable
    random order, with the source's quote; None for an artifact written without the list."""

    if "derive_fallback_edges" not in target.model_fields_set:
        return None
    nodes = {node.identity_key: node for node in target.nodes}
    edges = {edge.edge_key: edge for edge in target.edges}
    rows = []
    for key in target.derive_fallback_edges:
        edge = edges.get(key)
        if edge is None:
            continue
        source, end = nodes.get(edge.source_key), nodes.get(edge.target_key)
        rows.append(
            (
                key,
                {
                    "relation": edge.relation,
                    "source_label": source.label if source else "",
                    "source": source.name if source else edge.source_key,
                    "source_quote": (source.quote or "") if source else "",
                    "target_label": end.label if end else "",
                    "target": end.name if end else edge.target_key,
                    "fragments": " ".join(edge.fragment_ids),
                },
            )
        )
    return _numbered(rows, "F")


def identity_samples(build: StoredBuild, ontology: Ontology) -> dict[str, dict[str, Any]]:
    """Section 10: the candidate's identity samples (``IDENTITY_FILES``), each ``{file, rows}``; rows None
    (and no file) when the build's artifacts do not record what the sample reads."""

    found = {
        "merged_pairs": merged_pair_rows(build.resolve_artifact),
        "near_unmerged_pairs": near_unmerged_rows(build.resolve_artifact),
        "dedup_merges": dedup_merge_rows(build.target, ontology),
        "refused_pairs": refused_pair_rows(build.target),
        "derive_fallback_edges": derive_fallback_rows(build.target),
    }
    return {
        name: {"file": IDENTITY_FILES[name] if rows is not None else None, "rows": rows} for name, rows in found.items()
    }


# Edge samples (section 11)


def _target_nodes(target: TargetGraph) -> dict[str, TargetNode]:
    """Target nodes by identity key and each identity alias (the key first)."""

    found: dict[str, TargetNode] = {}
    for node in target.nodes:
        found[node.identity_key] = node
    for node in target.nodes:
        for alias in node.identity_aliases:
            found.setdefault(alias, node)
    return found


def _end(node: TargetNode | None, fallback: str) -> tuple[str, str]:
    """``(label, name)`` of an edge end; an unknown end by ``fallback`` (its key or ``spec_id``)."""

    return (node.label, node.name) if node is not None else ("", fallback)


def near_edge_rows(build: StoredBuild) -> list[dict[str, str]] | None:
    """The raw edges with an end its unit's catalog took by nearness (``CatalogParts.near``), stable random
    order; ``kept``: a target edge of the relation joins the ends' nodes (either direction). None when the
    build records no catalog parts or has no resolve artifact."""

    edges, resolved = build.edges_artifact, build.resolve_artifact
    if edges is None or edges.catalog_parts is None or resolved is None:
        return None
    canonical = {node.cid: node for node in resolved.canonical}
    nodes = _target_nodes(build.target)
    joined = {(edge.relation, edge.source_key, edge.target_key) for edge in build.target.edges}

    def node_of(cid: str) -> TargetNode | None:
        found = canonical.get(cid)
        return nodes.get(found.identity_key) if found is not None else None

    rows = []
    for index, raw in enumerate(edges.raw_edges):
        parts = edges.catalog_parts.get(raw.unit_id)
        near = set(parts.near) if parts is not None else set()
        ends = [side for side, cid in (("source", raw.source_cid), ("target", raw.target_cid)) if cid in near]
        if not ends:
            continue
        source, target = node_of(raw.source_cid), node_of(raw.target_cid)
        kept = (
            source is not None
            and target is not None
            and bool(
                {
                    (raw.relation, source.identity_key, target.identity_key),
                    (raw.relation, target.identity_key, source.identity_key),
                }
                & joined
            )
        )
        source_label, source_name = _end(source, raw.source_cid)
        target_label, target_name = _end(target, raw.target_cid)
        rows.append(
            (
                f"{raw.unit_id}|{index}|{raw.relation}|{raw.source_cid}|{raw.target_cid}",
                {
                    "unit": raw.unit_id,
                    "relation": raw.relation,
                    "near_end": " ".join(ends),
                    "source_label": source_label,
                    "source": source_name,
                    "target_label": target_label,
                    "target": target_name,
                    "fact": raw.fact or "",
                    "fragments": " ".join(raw.fragment_ids),
                    "kept": "yes" if kept else "no",
                },
            )
        )
    return _numbered(rows, "N")


def xdoc_item_rows(
    build: StoredBuild, limit: int = XDOC_ITEM_SAMPLE, *, ontology: Ontology | None = None
) -> list[dict[str, str]] | None:
    """The items cross-document rules accepted (``XDOC_ACCEPTED`` outcomes of ``InferenceReport.judged_items``; with
    ``ontology``, only items of its ``cross_document`` rules, since reports items of every rule), ends named through the
    registry and the target graph, the first ``limit`` in a stable random order; None when the report records no items.
    """

    report = build.inference_artifact
    if report is None or report.judged_items is None:
        return None
    xdoc = (
        None if ontology is None else {rule.id for rule in ontology.inference.rules if rule.template == CROSS_DOCUMENT}
    )
    keys = {spec_id: key for key, spec_id in build.spec_ids.items()}
    nodes = _target_nodes(build.target)

    def named(spec_id: str | None) -> tuple[str, str]:
        if spec_id is None:
            return "", ""
        return _end(nodes.get(keys.get(spec_id, "")), spec_id)

    rows = []
    for item in report.judged_items:
        if item.outcome not in XDOC_ACCEPTED or (xdoc is not None and item.rule not in xdoc):
            continue
        source_label, source = named(item.source_spec_id)
        target_label, target = named(item.target_spec_id)
        rows.append(
            (
                f"{item.rule}|{item.kind}|{item.source_spec_id}|{item.relation}|{item.target_spec_id}",
                {
                    "rule": item.rule,
                    "kind": item.kind,
                    "outcome": item.outcome,
                    "confidence": "" if item.confidence is None else f"{item.confidence:.2f}",
                    "relation": item.relation,
                    "subject": named(item.subject)[1],
                    "source_label": source_label,
                    "source": source,
                    "target_label": target_label,
                    "target": target,
                    "fragments": " ".join(item.fragment_ids),
                },
            )
        )
    return _numbered(rows, "X")[:limit]


def answered_drop_rows(build: StoredBuild) -> list[dict[str, str]] | None:
    """The open questions dropped as answered elsewhere (``TargetGraph.answered_elsewhere``), named from
    the resolve artifact, stable random order; None when the check did not run or is not recorded."""

    target = build.target
    if target.counters.oq_answer_judged is None and not target.answered_elsewhere:
        return None
    owners = _owners(build.resolve_artifact) if build.resolve_artifact is not None else {}
    rows = []
    for entry in target.answered_elsewhere:
        node = owners.get(entry.key)
        rows.append(
            (
                f"{entry.key}|{entry.fragment}",
                {
                    "label": node.label if node else "",
                    "question": node.name if node else "",
                    "key": entry.key,
                    "fragment": entry.fragment,
                    "quote": entry.quote,
                },
            )
        )
    return _numbered(rows, "A")


def owner_provider_rows(
    build: StoredBuild, ontology: Ontology, per_group: int = OWNER_PROVIDER_SAMPLE
) -> list[dict[str, str]]:
    """Edges on ``owner`` pairs and on provider pairs: the target edges and the inferred edges this build added (ends
    through the registry), at most ``per_group`` per kind, relation and basis in a stable random order;
    ``cross_document``: the ends share no document (empty when one has none).
    """

    nodes = {node.identity_key: node for node in build.target.nodes}
    providers = set(provider_pairs(ontology))
    documents = node_documents(build.target, build.plan)
    measured = [
        *(
            (edge.relation, edge.source_key, edge.target_key, edge.basis, edge.fragment_ids)
            for edge in build.target.edges
        ),
        *((*edge, ()) for edge in inferred_edges(build.result, build.spec_ids) or ()),
    ]
    groups: dict[tuple[str, str, str], list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for relation, source_key, target_key, basis, fragments in measured:
        source, target = nodes.get(source_key), nodes.get(target_key)
        if source is None or target is None:
            continue
        pair = ontology.pair_rule(relation, source.label, target.label)
        if pair is not None and pair.owner:
            kind = OWNER_KIND
        elif (relation, source.label, target.label) in providers:
            kind = PROVIDER_KIND
        else:
            continue
        first, second = documents.get(source_key), documents.get(target_key)
        crossing = "" if not first or not second else "no" if first & second else "yes"
        groups[(kind, relation, basis)].append(
            (
                f"{source_key}|{relation}|{target_key}|{basis}",
                {
                    "kind": kind,
                    "relation": relation,
                    "basis": basis,
                    "source_label": source.label,
                    "source": source.name,
                    "target_label": target.label,
                    "target": target.name,
                    "cross_document": crossing,
                    "fragments": " ".join(fragments),
                },
            )
        )
    chosen = [
        row
        for group in sorted(groups)
        for row in sorted(groups[group], key=lambda item: (_stable(item[0]), item[0]))[:per_group]
    ]
    return [
        {"row_id": f"O{index:03d}", **row, "judgement": "", "note": ""}
        for index, (_, row) in enumerate(chosen, start=1)
    ]


def edge_samples(build: StoredBuild, ontology: Ontology) -> dict[str, dict[str, Any]]:
    """Section 11: the candidate's edge samples (``EDGE_FILES``), each ``{file, rows}``; rows None (and no
    file) when the build's artifacts do not record what the sample reads."""

    found = {
        "near_edges": near_edge_rows(build),
        "xdoc_items": xdoc_item_rows(build, ontology=ontology),
        "answered_drops": answered_drop_rows(build),
        "owner_provider_edges": owner_provider_rows(build, ontology),
    }
    return {
        name: {"file": EDGE_FILES[name] if rows is not None else None, "rows": rows} for name, rows in found.items()
    }


# Section 12: inference and change report


def _sampled(
    groups: Mapping[tuple[str, ...], Sequence[tuple[str, dict[str, str]]]], per_group: int, prefix: str
) -> list[dict[str, str]]:
    """The first ``per_group`` rows of each group in a stable pseudo-random order (by their sort text),
    groups in key order, numbered, ``judgement`` and ``note`` empty."""

    chosen = [
        row
        for _, rows in sorted(groups.items())
        for _, row in sorted(rows, key=lambda item: (_stable(item[0]), item[0]))[:per_group]
    ]
    return [
        {"row_id": f"{prefix}{index:03d}", **row, "judgement": "", "note": ""}
        for index, row in enumerate(chosen, start=1)
    ]


def inferred_edge_rows(build: StoredBuild, per_band: int = INFERRED_PER_BAND) -> list[dict[str, str]] | None:
    """The inferred items rules accepted (``XDOC_ACCEPTED`` outcomes of ``InferenceReport.judged_items``, every
    template), at most ``per_band`` per rule and confidence band (``confidence_band``), then the inferred edges the
    result names as added this build that no item names, at most ``per_band``; ends named through the registry and the
    target graph.
    """

    report, result = build.inference_artifact, build.result
    items = report.judged_items if report is not None and report.judged_items is not None else None
    added = result.spec_changes.inferred_added if result is not None else ()
    if items is None and not added:
        return None
    keys = {spec_id: key for key, spec_id in build.spec_ids.items()}
    nodes = _target_nodes(build.target)

    def named(spec_id: str | None) -> tuple[str, str]:
        if spec_id is None:
            return "", ""
        return _end(nodes.get(keys.get(spec_id, "")), spec_id)

    groups: dict[tuple[str, ...], list[tuple[str, dict[str, str]]]] = defaultdict(list)
    covered: set[str] = set()
    for item in items or ():
        if item.outcome not in XDOC_ACCEPTED:
            continue
        ends = (item.source_spec_id, item.target_spec_id)
        covered |= {f"{ends[0]}|{item.relation}|{ends[1]}", f"{ends[1]}|{item.relation}|{ends[0]}"}
        band = confidence_band(item.confidence)
        source_label, source = named(ends[0])
        target_label, target = named(ends[1])
        groups[(item.rule, band)].append(
            (
                f"{item.rule}|{item.kind}|{ends[0]}|{item.relation}|{ends[1]}",
                {
                    "rule": item.rule,
                    "band": band,
                    "kind": item.kind,
                    "outcome": item.outcome,
                    "confidence": "" if item.confidence is None else f"{item.confidence:.2f}",
                    "relation": item.relation,
                    "subject": named(item.subject)[1],
                    "source_label": source_label,
                    "source": source,
                    "target_label": target_label,
                    "target": target,
                    "fragments": " ".join(item.fragment_ids),
                },
            )
        )
    for ref in added:
        parts = ref.split("|")
        if ref in covered or len(parts) != EDGE_REF_PARTS:
            continue
        source_label, source = named(parts[0])
        target_label, target = named(parts[2])
        groups[(NA, NA)].append(
            (
                ref,
                {
                    "rule": NA,
                    "band": NA,
                    "kind": "",
                    "outcome": "accepted",
                    "confidence": "",
                    "relation": parts[1],
                    "subject": "",
                    "source_label": source_label,
                    "source": source,
                    "target_label": target_label,
                    "target": target,
                    "fragments": "",
                },
            )
        )
    return _sampled(groups, per_band, "I")


def change_report_rows(build: StoredBuild, per_class: int = CHANGED_PER_CLASS) -> list[dict[str, str]] | None:
    """The result's change-report nodes (``BuildResult.change_report``), at most ``per_class`` per class
    (``CHANGED_PER_CLASS``), named through the registry and the target graph (a removed node by its
    ``spec_id``). None when the result records no change report."""

    report = build.result.change_report if build.result is not None else None
    if report is None:
        return None
    keys = {spec_id: key for key, spec_id in build.spec_ids.items()}
    nodes = _target_nodes(build.target)
    groups: dict[tuple[str, ...], list[tuple[str, dict[str, str]]]] = {}
    taken: set[str] = set()
    for index, change_class in enumerate(CHANGE_CLASSES):
        members = sorted(
            (node for node in report.nodes if change_class in node.classes and node.spec_id not in taken),
            key=lambda node: (_stable(node.spec_id), node.spec_id),
        )[:per_class]
        taken |= {node.spec_id for node in members}
        groups[(f"{index:02d}",)] = [
            (
                node.spec_id,
                {
                    "class": change_class,
                    "classes": " ".join(node.classes),
                    "causes": " ".join(node.causes),
                    "spec_id": node.spec_id,
                    "previous_spec_ids": " ".join(node.previous_spec_ids),
                    "label": node.label,
                    "name": _end(nodes.get(keys.get(node.spec_id, "")), "")[1],
                },
            )
            for node in members
        ]
    return _sampled(groups, per_class, "C")


def inference_estimate(
    evals: Mapping[str, Mapping[str, Any]], ontologies: Mapping[str, Ontology]
) -> dict[str, dict[str, Any]] | None:
    """Estimate vs actual of the inference steps (``INFERENCE_STEPS``) per role with an eval file: the step's estimate
    and actual ``[calls, input tokens]`` and ``ratio`` (actual over estimated input tokens); a role's inference prompts
    no estimate step covers (``NOT_ESTIMATED``, older eval files) count as actual of their step (``cross_document``
    rules: ``cross_document_inference``).
    """

    if not evals:
        return None
    out: dict[str, dict[str, Any]] = {}
    for role in ordered_roles(evals):
        versus = evals[role].get("estimate_vs_actual") or {}
        ontology = ontologies.get(role) or ontologies.get("candidate")
        rules = ontology.inference.rules if ontology is not None else ()
        prompts = {f"{NOT_ESTIMATED}{rule.prompt}": _inference_step(rule.template) for rule in rules}
        out[role] = {
            step: _step_versus([versus[name] for name in (step, *_named(prompts, step)) if name in versus])
            for step in INFERENCE_STEPS
        }
    return out


def _inference_step(template: str) -> str:
    """The estimate step of an inference rule's judge calls (``INFERENCE_STEPS``)."""

    return INFERENCE_STEPS[1] if template == CROSS_DOCUMENT else INFERENCE_STEPS[0]


def _named(steps: Mapping[str, str], step: str) -> list[str]:
    return [name for name, value in steps.items() if value == step]


def _step_versus(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Estimate and actual ``[calls, input tokens]`` summed over ``found`` (estimate vs actual entries of
    one step), and ``ratio``, actual over estimated input tokens; None each when not recorded."""

    estimates = [entry["estimate"] for entry in found if entry.get("estimate") is not None]
    estimate = [sum(values) for values in zip(*estimates, strict=True)] if estimates else None
    actuals = [entry["actual"] for entry in found]
    actual = [sum(values) for values in zip(*actuals, strict=True)] if actuals else None
    ratio = None
    if estimate is not None and actual is not None and estimate[1]:
        ratio = round(actual[1] / estimate[1], SHARE_DIGITS)
    return {"estimate": estimate, "actual": actual, "ratio": ratio}


def inference_samples(build: StoredBuild) -> dict[str, dict[str, Any]]:
    """Section 12: the candidate's inference and change-report samples (``INFERENCE_FILES``), each
    ``{file, rows}``; rows None (and no file) when the build's artifacts do not record them."""

    found = {"inferred_edges": inferred_edge_rows(build), "change_report_nodes": change_report_rows(build)}
    return {
        name: {"file": INFERENCE_FILES[name] if rows is not None else None, "rows": rows}
        for name, rows in found.items()
    }


def body_only_text(section: Mapping[str, Any]) -> str | None:
    """``BODY_ONLY_FILE``: a comment header, then ``<label>\t<name>`` per body-only reference hub; None
    when the section has no subsets."""

    hubs = section.get("body_only_hubs")
    if hubs is None:
        return None
    total = len(hubs) + int(section.get("heading_hubs") or 0)
    lines = [
        f"# Body-only reference hubs: name in no document title or heading (plan of build {section['plan']}).",
        f"# {len(hubs)} of {total} reference hubs ({', '.join(section['hub_labels'])}).",
        *(f"{hub['label']}\t{hub['name']}" for hub in hubs),
    ]
    return "\n".join(lines) + "\n"


# Reviewed sample


def _stable(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _node_row(ref: NodeRef, side: str, change: str, explanation: str) -> dict[str, str]:
    return {
        "kind": "node",
        "side": side,
        "change": change,
        "label": ref.label,
        "name": ref.name,
        "source": "",
        "target": "",
        "fragments": " ".join(ref.fragments),
        "explanation": explanation,
    }


def _edge_row(ref: EdgeRef, side: str, change: str, explanation: str) -> dict[str, str]:
    return {
        "kind": "edge",
        "side": side,
        "change": change,
        "label": ref.relation,
        "name": ref.basis,
        "source": f"{ref.source.label}: {ref.source.name}",
        "target": f"{ref.target.label}: {ref.target.name}",
        "fragments": " ".join(ref.fragments),
        "explanation": explanation,
    }


def _pick(
    changed: list[tuple[str, dict[str, str]]], unchanged: list[tuple[str, dict[str, str]]], size: int
) -> list[dict[str, str]]:
    """``size`` rows: changed ones first, then unchanged ones, each in a stable pseudo-random order."""

    ordered = sorted(changed, key=lambda item: _stable(item[0]))[:size]
    ordered += sorted(unchanged, key=lambda item: _stable(item[0]))[: size - len(ordered)]
    return [row for _, row in ordered]


def review_sample(
    baseline: StoredBuild,
    candidate: StoredBuild,
    comparison: Comparison,
    nodes: int = SAMPLE_NODES,
    edges: int = SAMPLE_EDGES,
) -> list[dict[str, str]]:
    """About ``nodes`` node rows and ``edges`` edge rows to judge, the changed ones first; ``side``
    ``before`` rows are baseline items, ``after`` rows candidate items. ``judgement`` is left empty."""

    changed_nodes = [
        (f"{side}|{ref.key}", _node_row(ref, side, change.kind, change.explanation))
        for change in comparison.nodes
        for side, refs in (("before", change.before), ("after", change.after))
        for ref in refs
    ]
    changed_keys = {key for key, _ in changed_nodes}
    before_nodes = {node.identity_key: node for node in baseline.target.nodes}
    after_nodes = {node.identity_key: node for node in candidate.target.nodes}
    unchanged_nodes = []
    for index, (before_key, after_key) in enumerate(sorted(comparison.pairs.items())):
        side, key, node = (
            ("after", after_key, after_nodes[after_key])
            if index % 2
            else ("before", before_key, before_nodes[before_key])
        )
        if f"{side}|{key}" not in changed_keys:
            unchanged_nodes.append((f"{side}|{key}", _node_row(node_ref(node), side, "unchanged", "")))
    changed_edges = []
    for change in comparison.edges:
        for side, ref in (("before", change.before), ("after", change.after)):
            if ref is not None:
                key = f"{side}|{ref.relation}|{ref.source.key}|{ref.target.key}"
                changed_edges.append((key, _edge_row(ref, side, change.kind, change.explanation)))
    unchanged_edges = []
    for index, pair in enumerate(comparison.unchanged_edges):
        side, ref = ("after", pair[1]) if index % 2 else ("before", pair[0])
        key = f"{side}|{ref.relation}|{ref.source.key}|{ref.target.key}"
        unchanged_edges.append((key, _edge_row(ref, side, "unchanged", "")))
    rows = _pick(changed_nodes, unchanged_nodes, nodes) + _pick(changed_edges, unchanged_edges, edges)
    return [
        {"row_id": f"{'N' if row['kind'] == 'node' else 'E'}{index:03d}", **row, "judgement": "", "note": ""}
        for index, row in enumerate(rows, start=1)
    ]


def write_review_sample(rows: Sequence[Mapping[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def review_counts(rows: Iterable[Mapping[str, str]]) -> dict[str, dict[str, dict[str, int]]]:
    """Judgements per kind and side (``before``/``after``): correct, wrong, unsure, blank."""

    out: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        judgement = (row.get("judgement") or "").strip().lower()
        slot = out.setdefault(row.get("kind", ""), {}).setdefault(
            row.get("side", ""), dict.fromkeys((*JUDGEMENTS, BLANK), 0)
        )
        slot[judgement if judgement in JUDGEMENTS else BLANK] += 1
    return {kind: dict(sorted(sides.items())) for kind, sides in sorted(out.items())}


def read_review(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


# The report


def quarantine_counts(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Quarantine entries per reason and per ``<RELATION>|<source label>|<target label>``."""

    entries = list(entries)
    by_reason = Counter(str(entry.get("reason")) for entry in entries)
    by_pair = Counter(
        "|".join(str(entry.get(key)) for key in ("relation", "source_label", "target_label")) for entry in entries
    )
    return {
        "quarantine_by_reason": dict(sorted(by_reason.items())),
        "quarantine_by_pair": dict(sorted(by_pair.items())),
    }


def _quarantine_counts(metrics: ConsolidatedMetrics, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The metrics' quarantine counts; while they are n/a, the counts of the artifact's raw entries if any."""

    counts: dict[str, Any] = {name: getattr(metrics, name) for name in QUARANTINE_FIELDS}
    if entries and all(value is None for value in counts.values()):
        return dict(quarantine_counts(entries))
    return counts


USAGE_KEYS: Final = ("calls", "input_tokens", "output_tokens", "cached_input_tokens")
"""Usage counts per call class in the time and cost section; ``cached_input_tokens`` (a part of the input tokens) is 0
in eval files written before it was recorded.
"""


def _time_and_cost(evals: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    if not evals:
        return None
    out: dict[str, Any] = {}
    for role in ordered_roles(evals):
        build = evals[role]
        out[role] = {
            "wall_s": build.get("wall_s"),
            "steps_s": {name: step.get("wall_s") for name, step in sorted((build.get("steps") or {}).items())},
            "by_class": {
                name: {key: counts.get(key, 0) for key in USAGE_KEYS}
                for name, counts in sorted(((build.get("total") or {}).get("by_class") or {}).items())
            },
            "estimate_vs_actual": build.get("estimate_vs_actual"),
        }
    return out


def build_report(
    builds: Mapping[str, StoredBuild],
    ontologies: Mapping[str, Ontology],
    *,
    evals: Mapping[str, Mapping[str, Any]] | None = None,
    reference: Reference | None = None,
    review: Sequence[Mapping[str, str]] | None = None,
    sample_path: str | None = None,
    recompute: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The report as JSON-ready data (sections 1-12) and the review sample rows."""

    baseline, candidate = builds["baseline"], builds["candidate"]
    metrics = {
        role: consolidated_metrics(build.target, build.result, ontologies[role], plan=build.plan, steps=build.steps())
        for role, build in builds.items()
    }
    rows = metric_rows(metrics)
    extra: list[MetricRow] = []
    if recompute:
        recomputed = {
            role: consolidated_metrics(
                build.target,
                build.result,
                ontologies[role],
                plan=build.plan,
                prefer_verification=False,
                steps=build.steps(),
            )
            for role, build in builds.items()
        }
        extra = recomputed_rows(rows, recomputed)
    comparison = compare(baseline, candidate, ontologies["candidate"])
    sample = review_sample(baseline, candidate, comparison)
    size = [asdict(row) for row in rows if row.name.startswith(SIZE_PREFIXES)]
    other = [
        asdict(row)
        for row in sorted((*rows, *extra), key=lambda row: row.name)
        if not row.name.startswith(SIZE_PREFIXES)
    ]
    node_kinds = Counter(change.kind for change in comparison.nodes)
    edge_kinds = Counter(change.kind for change in comparison.edges)
    unexplained = sum(1 for change in comparison.nodes if change.explanation in NOT_EXPLAINED) + sum(
        1 for change in comparison.edges if change.explanation in NOT_EXPLAINED
    )
    moved = [row for row in rows if row.verdict != NA and row.verdict not in SAME]
    moved.sort(key=lambda row: (-abs(row.delta or 0), row.name))
    quarantine = {
        role: _quarantine_counts(metrics[role], builds[role].quarantine) for role in ("baseline", "candidate")
    }
    report: dict[str, Any] = {
        "builds": {
            role: {
                "project": build.project,
                "build_id": build.build_id,
                "mode": build.result.mode if build.result else None,
                "verify_metrics": ", ".join(sorted(set(metrics[role].metric_sources.values()))),
                "ontology": build.ontology or NA,
            }
            for role, build in builds.items()
        },
        "recomputed": len(extra) if recompute else None,
        "1_summary": {
            "verdict": None,
            "decisions": None,
            "nodes": {role: len(build.target.nodes) for role, build in builds.items()},
            "edges": {role: len(build.target.edges) for role, build in builds.items()},
            "node_changes": dict(sorted(node_kinds.items())),
            "edge_changes": dict(sorted(edge_kinds.items())),
            "unexplained_changes": unexplained,
            "key_deltas": [asdict(row) for row in moved[:TOP_DELTAS]],
            "noise_band": sum(1 for role in builds if is_baseline(role)) >= MIN_NOISE_RUNS,
            "baseline_runs": sum(1 for role in builds if is_baseline(role)),
        },
        "2_time_and_cost": _time_and_cost(evals or {}),
        "3_graph_size": size,
        "4_metrics": other,
        "5_graph_comparison": {
            "matched_nodes": len(comparison.pairs),
            "nodes": [asdict(change) for change in comparison.nodes],
            "edges": [asdict(change) for change in comparison.edges],
        },
        "6_quarantine": {
            "topics": QUARANTINE_TOPICS,
            **quarantine,
        },
        "7_reviewed_sample": {
            "sample": sample_path,
            "rows": len(sample),
            "judgements": review_counts(review) if review is not None else None,
        },
        "8_precision_recall": (
            {role: precision_recall(builds[role].target, reference, ontologies[role]) for role in ordered_roles(builds)}
            if reference is not None
            else None
        ),
        "9_hub_names": hub_names(builds, ontologies, reference),
        "10_identity_samples": identity_samples(candidate, ontologies["candidate"]),
        "11_edge_samples": edge_samples(candidate, ontologies["candidate"]),
        "12_inference": {
            "estimate_vs_actual": inference_estimate(evals or {}, ontologies),
            "samples": inference_samples(candidate),
            "change_report_offline": candidate.change_report_offline,
        },
    }
    return report, sample


# Markdown


def fmt(value: Any) -> str:
    if value is None:
        return NA
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def signed(value: float | None) -> str:
    return NA if value is None else "0" if value == 0 else f"{value:+g}"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(fmt(cell).replace("|", "\\|") for cell in row) + " |" for row in rows]
    return lines


def _metric_table(rows: Sequence[Mapping[str, Any]], roles: Sequence[str]) -> list[str]:
    if not rows:
        return [NA]
    headers = ["metric", *roles, "delta", "noise", "verdict"]
    with_original = "original" in roles
    if with_original:
        headers.append("delta vs original")
    with_source = any(row["source"] for row in rows)
    if with_source:
        headers.append(f"source ({' / '.join(roles)})")
    body = []
    for row in rows:
        cells = [row["name"], *(row["values"].get(role) for role in roles)]
        cells += [signed(row["delta"]), row["noise"], row["verdict"]]
        if with_original:
            cells.append(signed(row["delta_original"]))
        if with_source:
            cells.append(row["source"])
        body.append(cells)
    return table(headers, body)


def _node_text(ref: Mapping[str, Any]) -> str:
    return f"{ref['label']} '{ref['name']}'"


def _nodes_text(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> str:
    """``before => after``; a new or gone node alone."""

    texts = [", ".join(_node_text(ref) for ref in refs) for refs in (before, after) if refs]
    return " => ".join(texts)


def _edge_text(ref: Mapping[str, Any] | None) -> str:
    if ref is None:
        return NA
    return f"{_node_text(ref['source'])} -{ref['relation']}-> {_node_text(ref['target'])}"


def _listed(items: Sequence[str]) -> list[str]:
    lines = [f"- {item}" for item in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        lines.append(f"- ... {len(items) - MAX_LISTED} more (report.json)")
    return lines


def _summary_lines(report: Mapping[str, Any], roles: Sequence[str]) -> list[str]:
    summary = report["1_summary"]
    out = ["Proposed verdict: _to be filled by the reviewer (approve / reject)._", ""]
    out += ["Decisions to take: _to be filled by the reviewer._", ""]
    out += table(
        ["", *roles],
        [["nodes", *(summary["nodes"].get(r) for r in roles)], ["edges", *(summary["edges"].get(r) for r in roles)]],
    )
    nodes = ", ".join(f"{k} {v}" for k, v in summary["node_changes"].items()) or "none"
    edges = ", ".join(f"{k} {v}" for k, v in summary["edge_changes"].items()) or "none"
    out += ["", f"Node changes: {nodes}. Edge changes: {edges}. Unexplained: {summary['unexplained_changes']}."]
    if not summary["noise_band"]:
        out += ["", "One baseline run only: no noise band, every non-zero delta counts."]
    else:
        out += [
            "",
            (
                f"Noise band: the range (max - min) over {summary['baseline_runs']} baseline runs; the delta is "
                "against the first run. No difference: the candidate lies within the runs' range, or |delta| <= "
                "the band (the verdict says which)."
            ),
        ]
    out += ["", "Key deltas (outside the noise band):", ""]
    deltas = [
        f"{row['name']}: {fmt(row['values'].get('baseline'))} -> {fmt(row['values'].get('candidate'))} "
        f"({signed(row['delta'])}, noise {fmt(row['noise'])})"
        for row in summary["key_deltas"]
    ]
    return out + (_listed(deltas) or ["- none"])


def _cost_lines(cost: Mapping[str, Any] | None, roles: Sequence[str]) -> list[str]:
    if not cost:
        return [NA]
    present = [role for role in roles if role in cost]
    steps = sorted({name for role in present for name in cost[role]["steps_s"]})
    out = table(
        ["step (s)", *present],
        [
            ["total", *(cost[r]["wall_s"] for r in present)],
            *([name, *(cost[r]["steps_s"].get(name) for r in present)] for name in steps),
        ],
    )
    classes = sorted({name for role in present for name in cost[role]["by_class"]})
    out += [
        "",
        *table(
            ["call class", *(f"{r} calls / in / out / cached in" for r in present)],
            [*_class_rows(cost, present, classes), ["total", *(_slash(_usage_total(cost[r])) for r in present)]],
        ),
        "",
        "Cached input share: " + ", ".join(f"{r} {_cached_share(_usage_total(cost[r]))}" for r in present) + ".",
    ]
    for role in present:
        versus = cost[role]["estimate_vs_actual"]
        if versus:
            out += ["", f"Estimate vs actual ({role}, calls / input tokens):", ""]
            out += table(
                ["step", "estimate", "actual"],
                [[name, _slash(v["estimate"]), _slash(v["actual"])] for name, v in versus.items()],
            )
    return out


def _slash(values: Iterable[Any] | None) -> str:
    return " / ".join(str(value) for value in values) if values else NA


def _usage_total(role_cost: Mapping[str, Any]) -> list[int]:
    """``USAGE_KEYS`` summed over the call classes of one role."""

    return [sum(counts.get(key, 0) for counts in role_cost["by_class"].values()) for key in USAGE_KEYS]


def _cached_share(total: Sequence[int]) -> str:
    """Cached input tokens as a percentage of the input tokens of one ``_usage_total``."""

    input_tokens, cached = total[USAGE_KEYS.index("input_tokens")], total[USAGE_KEYS.index("cached_input_tokens")]
    return f"{100 * cached / input_tokens:.1f}%" if input_tokens else NA


def _class_rows(cost: Mapping[str, Any], roles: Sequence[str], classes: Sequence[str]) -> list[list[str]]:
    return [
        [name, *(_slash(cost[r]["by_class"][name].values()) if name in cost[r]["by_class"] else NA for r in roles)]
        for name in classes
    ]


def _comparison_lines(comparison: Mapping[str, Any]) -> list[str]:
    out = [f"Matched nodes (one to one): {comparison['matched_nodes']}."]
    for kind in NODE_KINDS:
        items = [change for change in comparison["nodes"] if change["kind"] == kind]
        texts = [f"{_nodes_text(change['before'], change['after'])}: {change['explanation']}" for change in items]
        out += ["", f"### Nodes {kind} ({len(items)})", "", *(_listed(texts) or ["- none"])]
    for kind in EDGE_KINDS:
        items = [change for change in comparison["edges"] if change["kind"] == kind]
        texts = [
            f"{_edge_text(change['before'])} => {_edge_text(change['after'])}: {change['explanation']}"
            if kind in ("relation_changed", "direction_changed")
            else f"{_edge_text(change['before'] or change['after'])}: {change['explanation']}"
            for change in items
        ]
        out += ["", f"### Edges {kind.replace('_', ' ')} ({len(items)})", "", *(_listed(texts) or ["- none"])]
    return out


def _quarantine_lines(quarantine: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    sides = ("baseline", "candidate")
    for name in QUARANTINE_FIELDS:
        values = {role: quarantine[role][name] for role in sides}
        keys = sorted({key for value in values.values() if value for key in value})
        out += [f"{name.replace('_', ' ')}:", ""]
        if not keys:
            out += [NA if all(value is None for value in values.values()) else "none", ""]
            continue
        with_topic = name == "quarantine_by_reason"
        rows = [
            [
                key,
                *(None if values[r] is None else values[r].get(key, 0) for r in sides),
                *([QUARANTINE_TOPICS.get(key, "")] if with_topic else []),
            ]
            for key in keys
        ]
        out += [*table(["key", *sides, *(["topic"] if with_topic else [])], rows), ""]
    return out


def _review_lines(sample: Mapping[str, Any]) -> list[str]:
    judgements = sample["judgements"]
    if judgements is None:
        return [
            (
                f"{sample['rows']} rows exported to `{sample['sample']}`: fill `judgement` (correct / wrong / unsure) "
                "and pass the file back with `--review`."
            ),
            "",
            f"Judgements: {NA}.",
        ]
    out = [f"Judgements read from `{sample['sample']}` (the sample is not exported again)."]
    body = [
        [kind, side, *(counts[key] for key in (*JUDGEMENTS, BLANK))]
        for kind, sides in judgements.items()
        for side, counts in sides.items()
    ]
    return [*out, "", *table(["kind", "side", *JUDGEMENTS, BLANK], body)]


def _score_lines(scores: Mapping[str, Any] | None) -> list[str]:
    if scores is None:
        return [NA]
    out: list[str] = []
    roles = list(scores)
    for part in ("nodes", "edges"):
        keys = sorted({key for role in roles for key in scores[role][part]})
        out += [f"{part.capitalize()} (precision / recall (correct/in scope, found/reference)):", ""]
        out += table(["type", *roles], [[key, *(_pr(scores[role][part].get(key)) for role in roles)] for key in keys])
        out.append("")
    labels = sorted({label for role in roles for label in scores[role].get("fallback") or {}})
    out += [f"Renamed-item fallback matches per type (listed in `{FALLBACK_FILE}`):", ""]
    if labels:
        out += table(
            ["type", *roles], [[label, *(scores[role]["fallback"].get(label, 0) for role in roles)] for label in labels]
        )
    else:
        out.append("none")
    return out


def _pr(item: Mapping[str, Any] | None) -> str:
    if item is None:
        return NA
    counts = f"{item['correct']}/{item['in_scope']}, {item['found']}/{item['reference']}"
    return f"{fmt(item['precision'])} / {fmt(item['recall'])} ({counts})"


def _found(item: Mapping[str, Any] | None) -> str:
    if item is None:
        return NA
    return f"{fmt(item['recall'])} ({item['found']}/{item['reference']})"


def _hub_name_lines(section: Mapping[str, Any], roles: Sequence[str]) -> list[str]:
    out = [f"Hub labels: {', '.join(section['hub_labels']) or NA}.", ""]
    hubs = section["body_only_hubs"]
    if hubs is None:
        out += [f"Body-only subset: {NA} (needs `--reference` and the candidate's plan).", ""]
    else:
        out += [
            (
                f"Body-only subset: {len(hubs)} of {len(hubs) + section['heading_hubs']} reference hubs name no "
                f"document title or heading (plan of build {section['plan']}; `{BODY_ONLY_FILE}`)."
            ),
            "",
            *_listed([f"{hub['label']}: {hub['name']}" for hub in hubs]),
            "",
            "Recall by reference match and catalog recall (name or alias in the hub catalog), as recall (found/ref):",
            "",
        ]
        recall, catalog = section["recall"], section["catalog_recall"]
        body = [
            [f"{kind} {subset}", *(_found((values[role] or {}).get(subset)) for role in roles)]
            for kind, values in (("extraction", recall), ("catalog", catalog))
            for subset in ("body_only", "heading", "all")
        ]
        body.append(["reference hubs matched by >= 2 nodes", *(recall[role]["multi_matched"] for role in roles)])
        out += [*table(["", *roles], body), ""]
        for role in roles:
            listed = recall[role]["multi_matched_hubs"]
            if listed:
                out += [f"Matched by >= 2 nodes ({role}):", "", *_listed(listed), ""]
    nearby = section["nearby_unsupported"]
    if nearby["rows"] is None:
        out.append(f"Unsupported nearby-term uses (candidate): {NA} (no known terms stored).")
    else:
        out.append(
            f"Unsupported nearby-term uses (candidate): {len(nearby['rows'])} rows in `{nearby['file']}` "
            "(stable random order: judge the first 20)."
        )
    return out


def _identity_lines(section: Mapping[str, Any] | None) -> list[str]:
    if not section:
        return [f"Identity samples: {NA}."]
    out = []
    for name, item in section.items():
        title = name.replace("_", " ")
        rows = item["rows"]
        if rows is None:
            out.append(f"- {title}: {NA} (not recorded by the candidate's artifacts)")
        else:
            out.append(f"- {title}: {len(rows)} rows in `{item['file']}`")
    return [
        "Candidate samples to judge (stable random order; near unmerged pairs: the closest first, then random):",
        "",
        *out,
    ]


def _edge_sample_lines(section: Mapping[str, Any] | None) -> list[str]:
    if not section:
        return [f"Edge samples: {NA}."]
    out = []
    for name, item in section.items():
        title = name.replace("_", " ")
        rows = item["rows"]
        if rows is None:
            out.append(f"- {title}: {NA} (not recorded by the candidate's artifacts)")
        else:
            out.append(f"- {title}: {len(rows)} rows in `{item['file']}`")
    return [
        (
            "Candidate samples to judge (stable random order; cross-document items: the accepted ones, "
            f"at most {XDOC_ITEM_SAMPLE}; owner and provider edges: at most {OWNER_PROVIDER_SAMPLE} per kind, "
            "relation and basis):"
        ),
        "",
        *out,
    ]


def _inference_lines(section: Mapping[str, Any] | None) -> list[str]:
    if not section:
        return [f"Inference: {NA}."]
    out = ["Estimate vs actual of the inference steps (calls / input tokens; ratio: actual / estimated input):", ""]
    versus = section.get("estimate_vs_actual")
    if versus:
        out += table(
            ["role", "step", "estimate", "actual", "ratio"],
            [
                [role, step, _slash(item["estimate"]), _slash(item["actual"]), fmt(item["ratio"])]
                for role, steps in versus.items()
                for step, item in steps.items()
            ],
        )
    else:
        out.append(f"{NA} (no eval file)")
    out += [
        "",
        (
            f"Candidate samples to judge (stable random order; inferred edges: the accepted items, at most "
            f"{INFERRED_PER_BAND} per rule and confidence band; change-report nodes: at most {CHANGED_PER_CLASS} "
            "per class):"
        ),
        "",
    ]
    for name, item in (section.get("samples") or {}).items():
        title = name.replace("_", " ")
        rows = item["rows"]
        if rows is None:
            out.append(f"- {title}: {NA} (not recorded by the candidate's artifacts)")
        else:
            out.append(f"- {title}: {len(rows)} rows in `{item['file']}`")
    if section.get("change_report_offline"):
        out += ["", "The candidate's change report was classified offline from its stored artifacts."]
    return out


def render_markdown(report: Mapping[str, Any]) -> str:
    """The report's 12 sections as markdown, from ``build_report``'s data."""

    roles = ordered_roles(report["builds"])
    out = ["# Build comparison report", ""]
    out += table(
        ["role", "project", "build", "mode", "Verify metrics from", "ontology"],
        [
            [role, b["project"], b["build_id"], b["mode"], b["verify_metrics"], b["ontology"]]
            for role, b in report["builds"].items()
        ],
    )
    if report.get("recomputed") is not None:
        out += ["", f"Verify metrics recomputed from the artifacts: {report['recomputed']} rows differ (section 4)."]
    sections = (
        ("1. Summary", _summary_lines(report, roles)),
        ("2. Time and cost", _cost_lines(report["2_time_and_cost"], roles)),
        ("3. Graph size", _metric_table(report["3_graph_size"], roles)),
        ("4. Metrics", _metric_table(report["4_metrics"], roles)),
        ("5. Graph comparison", _comparison_lines(report["5_graph_comparison"])),
        ("6. Quarantine", _quarantine_lines(report["6_quarantine"])),
        ("7. Reviewed sample", _review_lines(report["7_reviewed_sample"])),
        ("8. Precision and recall", _score_lines(report["8_precision_recall"])),
        ("9. Hub names and known terms", _hub_name_lines(report["9_hub_names"], roles)),
        ("10. Identity samples", _identity_lines(report.get("10_identity_samples"))),
        ("11. Edge samples", _edge_sample_lines(report.get("11_edge_samples"))),
        ("12. Inference and change report", _inference_lines(report.get("12_inference"))),
    )
    for title, lines in sections:
        while lines and not lines[-1]:
            lines.pop()
        out += ["", f"## {title}", "", *lines]
    return "\n".join(line.rstrip() for line in out).rstrip() + "\n"


def write_report(report: Mapping[str, Any], sample: Sequence[Mapping[str, str]] | None, out: Path) -> None:
    """``report.md`` and ``report.json`` under ``out``, and ``review-sample.csv`` unless ``sample`` is None
    (a run with ``--review`` keeps the judged file, even when it is that same path)."""

    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, indent=1, sort_keys=False, default=str) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    if sample is not None:
        write_review_sample(sample, out / SAMPLE_FILE)
    section = report.get("9_hub_names") or {}
    body_only = body_only_text(section)
    if body_only is not None:
        (out / BODY_ONLY_FILE).write_text(body_only, encoding="utf-8")
    rows = (section.get("nearby_unsupported") or {}).get("rows")
    if rows is not None:
        _write_csv(out / NEARBY_FILE, NEARBY_COLUMNS, rows)
    scores = report.get("8_precision_recall")
    if scores is not None:
        fallback = [{"role": role} | row for role, item in scores.items() for row in item.get("fallback_matches", ())]
        _write_csv(out / FALLBACK_FILE, FALLBACK_COLUMNS, fallback)
    samples = (
        (report.get("10_identity_samples"), IDENTITY_COLUMNS),
        (report.get("11_edge_samples"), EDGE_COLUMNS),
        ((report.get("12_inference") or {}).get("samples"), INFERENCE_COLUMNS),
    )
    for section_samples, columns in samples:
        for name, item in (section_samples or {}).items():
            if item["rows"] is not None:
                _write_csv(out / item["file"], (*columns[name], "judgement", "note"), item["rows"])


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


# CLI


def describe_ontology(ontology: Ontology, path: Path | None) -> str:
    """``<name> <version> (<file>)``; the file is ``shipped`` for the package's own."""

    return f"{ontology.name} {ontology.version} ({path if path is not None else 'shipped'})"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--baseline", required=True, action="append", type=parse_build_spec, help="STATE:PROJECT[:BUILD]; once or more"
    )
    parser.add_argument("--candidate", required=True, type=parse_build_spec, help="STATE:PROJECT[:BUILD]")
    parser.add_argument("--original", type=parse_build_spec, help="the v1.3 baseline, for cumulative deltas")
    parser.add_argument("--eval-json", action="append", default=[], type=load_eval, help="ROLE=PATH[#LABEL]")
    parser.add_argument("--reference", type=Path, help="reference.yaml (precision and recall per type)")
    parser.add_argument("--review", type=Path, help="review.csv with judgements")
    parser.add_argument("--baseline-ontology", type=Path, help="ontology file of the baseline and original builds")
    parser.add_argument("--candidate-ontology", type=Path, help="ontology file of the candidate build")
    parser.add_argument(
        "--recompute-metrics",
        action="store_true",
        help="also measure the Verify metrics on the artifacts; differing rows show next to the stored ones",
    )
    parser.add_argument("--out", required=True, type=Path, help="folder for report.md, report.json, review-sample.csv")
    args = parser.parse_args(argv)
    if args.baseline_ontology is not None and args.candidate_ontology is None:
        parser.error("--baseline-ontology needs --candidate-ontology (name the shipped file to compare with it)")
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    paths = {"baseline": args.baseline_ontology, "candidate": args.candidate_ontology}
    loaded = {role: load_ontology(path) for role, path in paths.items()}
    specs = {"baseline": args.baseline[0], "candidate": args.candidate}
    for run, spec in enumerate(args.baseline[1:], start=2):
        specs[f"baseline{run}"] = spec
    if args.original is not None:
        specs["original"] = args.original
    side = {role: "candidate" if role == "candidate" else "baseline" for role in specs}  # original: baseline
    ontologies = {role: loaded[side[role]] for role in specs}
    builds = {
        role: await load_build(spec, role, ontologies[role], describe_ontology(ontologies[role], paths[side[role]]))
        for role, spec in specs.items()
    }
    report, sample = build_report(
        builds,
        ontologies,
        evals=dict(args.eval_json),
        reference=load_reference(args.reference) if args.reference else None,
        review=await asyncio.to_thread(read_review, args.review) if args.review else None,
        sample_path=str(args.review or args.out / SAMPLE_FILE),
        recompute=args.recompute_metrics,
    )
    write_report(report, None if args.review else sample, args.out)
    summary = report["1_summary"]
    print(json.dumps({"out": str(args.out)} | {key: summary[key] for key in SUMMARY_KEYS}))


if __name__ == "__main__":
    raise SystemExit(run_example(main))
