"""Consolidated build metrics: pure functions over stored artifacts."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, get_args

import numpy as np
from pydantic import Field

from graph.build.answers import HUBS_LIST, STATEMENTS_LIST
from graph.build.conform import grounding_stats
from graph.build.models import (
    ArtifactModel,
    Basis,
    BuildResult,
    DiffArtifact,
    EdgesArtifact,
    HubMergeSource,
    InferenceReport,
    KnownTermsShown,
    MetricFields,
    NodesArtifact,
    PlanArtifact,
    QuarantineEntry,
    RawNode,
    ResolveArtifact,
    RuleReport,
    TargetGraph,
)
from graph.build.resolve import hub_surface, hub_words, surface_values
from graph.build.spine import SpineEdge, SpineGraph, SpineNode, condition_attributes
from graph.build.terms import NameIndex, fragment_words
from graph.build.validator import exclusive_conflicts
from graph.build.vectors import Vector
from graph.core.ontology import NodeView, Ontology

READ_BASES: Final = ("stated", "implied")
"""Edge bases of the graph before inference: what inference selection reads and ``spine_unreachable_before`` walks; an
edge without ``basis`` counts as the first.
"""
LEGALISATION_REASONS: Final = frozenset({"unknown_relation", "illegal_pair", "cue_missing", "self_loop", "locality"})
"""``TargetCounters.dropped_edges`` reasons of legalisation and locality."""
ACTIVE: Final = "active"
SKIPPED: Final = "skipped"
CHECK_METRICS: Final = (
    "spine_unreachable_before",
    "spine_unreachable",
    "exclusive_conflicts",
    "label_disagreement",
    "dropped_edges",
    "fragments_without_nodes",
)
"""Verify metrics stored before the graph-shape metrics."""
GRAPH_METRICS: Final = (
    "ownerless_requirements",
    "interfaces_without_provider",
    "features_named_like_statements",
    "relates_fan_out",
    "entity_relates_fan_out",
    "deployed_to_fan_out",
)
"""Graph-shape metrics Verify measures on the persisted graph."""
VERIFY_METRICS: Final = (*CHECK_METRICS, *GRAPH_METRICS)
"""Metrics with a stored Verify value and an artifact value (``metric_sources`` names which)."""
COUNTER_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "owner_conflicts_judged": ("owner_conflicts_judged",),
    "cycles_broken": ("cycles_broken",),
    "unsupported_entities_dropped": ("unsupported_nodes",),
    "in_scope_disagreements": ("in_scope_disagreements",),
    "in_scope_dirty_wins": ("in_scope_dirty_wins",),
}
"""Conform-count metrics and the ``TargetCounters`` field read for each."""
GROUNDING_METRICS: Final = (
    "unsnapped_quote_nodes",
    "unsnapped_source_quotes",
    "provisional_keys_left",
    "attributed_fragments_dropped",
    "nodes_missing_source_documents",
)
"""Grounding metrics (``conform.GroundingStats``), each the ``TargetCounters`` field of its name."""
STEP_COUNT_METRICS: Final = ("label_collapses", "derive_fallback_matches", "derive_fallback_refused")
"""Extraction and Conform counts shown with the metrics: ``NodeCounters.label_collapses`` (names a node call gave two or
more labels) and ``TargetCounters.derive_fallback_matches`` (``derive`` references resolved by the whole-word fallback),
``TargetCounters.derive_fallback_refused`` (those it refused).
"""
ALL_HUBS: Final = "all"
"""Key of the every-hub part of the per-hub name metrics (the others are hub labels)."""
HUB_NAME_METRICS: Final = ("hub_surfaces_per_hub", "hub_raw_names_per_hub")
CATALOG_METRICS: Final = (
    "catalog_batches",
    "catalog_calls",
    "catalog_failed",
    "catalog_reused",
    "catalog_truncation_splits",
    "catalog_hubs",
    "catalog_aliases",
    "catalog_links_refused",
    "catalog_aliases_dropped",
    "catalog_ambiguous",
)
"""Hub catalog metrics, each the ``NodeCounters`` field of its name."""
KNOWN_TERM_METRICS: Final = ("known_terms_mentioned", "known_terms_nearby")
TERM_USE_METRICS: Final = (
    "nearby_term_uses",
    "nearby_term_uses_unsupported",
    "nearby_term_uses_hubs",
    "nearby_term_uses_hubs_unsupported",
    "nearby_term_uses_statements",
    "nearby_term_uses_statements_unsupported",
    "alias_name_uses",
)
NAMING_METRICS: Final = (
    *HUB_NAME_METRICS,
    "hub_consolidation_merges",
    *CATALOG_METRICS,
    *KNOWN_TERM_METRICS,
    *TERM_USE_METRICS,
)
"""Hub-name and known-term metrics (``naming_metrics``)."""
IDENTITY_METRICS: Final = (
    "hub_merges_by_source",
    "pair_candidates_vector",
    "pair_candidates_alias",
    "pair_batches",
    "pair_calls",
    "pair_over_cap",
    "pair_merges",
    "alias_merges",
    "unsure_groups",
    "decisions_expired",
    "dedup_candidates_rich_only",
    "dedup_unsure",
    "dedup_verdicts_expired",
    "dedup_refused_by_guard",
)
"""Hub merge, pair-pass and dedup metrics (``identity_metrics``); n/a (None) on artifacts written before them."""
RESOLVE_IDENTITY_FIELDS: Final = (
    "pair_candidates_vector",
    "pair_candidates_alias",
    "pair_batches",
    "pair_calls",
    "pair_over_cap",
    "pair_merges",
    "alias_merges",
    "unsure_groups",
    "decisions_expired",
)
"""Identity metrics that are the ``ConsolidationCounters`` field of their name (resolve artifact)."""
CONFORM_IDENTITY_FIELDS: Final = ("dedup_candidates_rich_only", "dedup_unsure", "dedup_verdicts_expired")
"""Identity metrics that are the ``TargetCounters`` field of their name (conform artifact)."""
HUB_MERGE_SOURCES: Final = get_args(HubMergeSource)
NEARBY_USE: Final = "nearby"
ALIAS_USE: Final = "alias"
MODAL_VERBS: Final = ("must", "shall", "should", "will", "can", "cannot", "may", "might", "could", "would")
"""English modal verbs that mark a statement (language, not ontology)."""
_MODAL: Final = re.compile(r"\b(?:" + "|".join(MODAL_VERBS) + r")\b", re.IGNORECASE)
STATEMENT_END: Final = "."
GROUP_ROLE: Final = "group"
ANCHOR_ROLE: Final = "anchor"
VERIFY_SOURCE: Final = "verify"
SPINE_VIEW_SOURCE: Final = "spine view"
ARTIFACTS_SOURCE: Final = "artifacts"
BUILD_METRICS_SOURCE: Final = "build metrics"
NO_SOURCE: Final = "n/a"
CONFORM_SOURCE: Final = "conform artifact (before inference)"
"""Where an edge metric came from when the artifacts cannot give the persisted graph after inference
(no result, no ``spec_id`` map, or edges carried over from earlier builds): the conform artifact alone."""
METRIC_SOURCES: Final = (
    VERIFY_SOURCE,
    SPINE_VIEW_SOURCE,
    ARTIFACTS_SOURCE,
    BUILD_METRICS_SOURCE,
    CONFORM_SOURCE,
    NO_SOURCE,
)
EDGE_REF_PARTS: Final = 3
"""Parts of an ``edge_ref``: ``<source spec_id>|<RELATION>|<target spec_id>``."""
MEASURED_EDGE_METRICS: Final = ("owners_by_source", "cross_document_edges")
"""Edge metrics with a stored Verify value and an artifact value (``metric_sources`` names which)."""
EDGE_SHAPE_METRICS: Final = (
    "edges_per_requirement",
    "requirements_without_edge",
    "owners_by_source",
    "cross_document_edges",
    "cross_document_share",
    "orphans_best_elsewhere",
)
EDGE_COUNTER_METRICS: Final = (
    "foreign_edges",
    "derive_suppressed_by_edge",
    "oq_answer_judged",
    "oq_partly_kept",
    "oq_answered_dropped",
)
"""Edge metrics that are the ``TargetCounters`` field of their name (conform artifact)."""
WINDOW_METRICS: Final = ("windows_called", "windows_reused", "windows_fallback", "warm_ups")
"""Edge metrics that are the ``EdgeCounters`` field of their name (extract-edges artifact)."""
EDGE_METRICS: Final = (
    *EDGE_SHAPE_METRICS,
    *EDGE_COUNTER_METRICS,
    *WINDOW_METRICS,
    "windows_called_by_cause",
    "items_by_outcome",
    "modified_causes",
)
"""Edge and cross-document metrics (``edge_metrics``)."""
INFERENCE_METRICS: Final = (
    "inference_backlog",
    "inference_limit_hits",
    "inference_budget_tokens",
    "inference_read_edges",
    "inference_changed_queued",
    "inference_items_by_band",
    "spine_unreachable_planned",
    "inference_input_share",
    "settlement",
    "change_report_nodes",
    "change_report_edges",
)
"""Inference and operation metrics (``inference_metrics``)."""
CONFIDENCE_BANDS: Final = ((0.8, "[80,100]"), (0.7, "[70,80)"), (0.6, "[60,70)"), (0.0, "[0,60)"))
"""Confidence bands of judged items , by lower bound, highest first; percent labels."""
NO_CONFIDENCE: Final = "none"
ALL_RULES: Final = "all"
"""Key of the infer step's share in ``inference_input_share`` (the others are rule ids)."""
INFER_STEP: Final = "infer"
OWNER_BASES: Final[tuple[Basis, ...]] = ("stated", "implied", "inferred")
"""Owner-edge bases by precedence (``owners_by_source``); a requirement without any is ``NO_OWNER``."""
NO_OWNER: Final = "none"
UNRECORDED_CAUSE: Final = "unrecorded"
SHARE_DIGITS: Final = 3


class ConsolidatedMetrics(MetricFields):
    """The consolidated metric set of one build; None: the artifacts carry no data for it (n/a)."""

    nodes_by_label: dict[str, int] = Field(default_factory=dict)
    edges_by_relation_basis: dict[str, dict[str, int]] = Field(default_factory=dict)
    inferred_added_by_relation: dict[str, int] = Field(default_factory=dict)
    edges_by_relation_persisted: dict[str, int] | None = None
    metric_sources: dict[str, str] = Field(default_factory=dict)
    """Where each Verify metric (``VERIFY_METRICS``) came from (``METRIC_SOURCES``)."""
    spine_unreachable: int | None = None
    dropped_edges: dict[str, int] = Field(default_factory=dict)
    fragments_without_nodes: int | None = None
    spine_unreachable_before_by_label: dict[str, int] = Field(default_factory=dict)
    judge_calls: dict[str, int] | None = None
    """Judge calls and failed judge calls per judging step (``BuildMetrics.judge_calls``); None without a result or for
    one written before they were counted.
    """
    judge_failed_calls: dict[str, int] | None = None
    edges_per_requirement: float | None = None
    """Edge and cross-document metrics (``EDGE_METRICS``; module docstring)."""
    requirements_without_edge: float | None = None
    cross_document_share: float | None = None
    orphans_best_elsewhere: dict[str, int] | None = None
    """``orphans``, ``measured``, ``elsewhere``."""
    foreign_edges: dict[str, dict[str, int]] | None = None
    """Catalog entry path to outcome to count."""
    derive_suppressed_by_edge: int | None = None
    windows_called: int | None = None
    windows_reused: int | None = None
    windows_fallback: int | None = None
    warm_ups: int | None = None
    windows_called_by_cause: dict[str, int] | None = None
    items_by_outcome: dict[str, dict[str, int]] | None = None
    """Inference rule to outcome to count."""
    modified_causes: dict[str, int] | None = None
    inference_backlog: dict[str, dict[str, int]] | None = None
    """Inference and operation metrics (``INFERENCE_METRICS``; module docstring): rule to
    ``subjects`` and ``age_builds``."""
    inference_limit_hits: dict[str, dict[str, int]] | None = None
    """Rule to the bound that stopped it, as 1."""
    inference_budget_tokens: dict[str, int] | None = None
    inference_read_edges: dict[str, int] | None = None
    inference_changed_queued: dict[str, int] | None = None
    inference_items_by_band: dict[str, dict[str, dict[str, int]]] | None = None
    """Rule to confidence band to outcome to count."""
    spine_unreachable_planned: int | None = None
    inference_input_share: dict[str, float] | None = None
    settlement: dict[str, Any] | None = None
    """``settled``, ``documents``, ``follow_up_builds``, ``inference_backlog`` and ``reasons`` (reason to 1)."""
    change_report_nodes: dict[str, int] | None = None
    change_report_edges: dict[str, int] | None = None


# Shared metric functions (Verify calls them on the persisted graph)


def dropped_by_relation(target: TargetGraph | None) -> dict[str, int]:
    """Edges Conform dropped by legalisation and locality (``LEGALISATION_REASONS``), per relation (from
    the build's conform artifact); normalisation reasons are left out."""

    if target is None:
        return {}
    counts = {
        relation: sum(count for reason, count in reasons.items() if reason in LEGALISATION_REASONS)
        for relation, reasons in sorted(target.counters.dropped_edges.items())
    }
    return {relation: count for relation, count in counts.items() if count}


def records_quarantine(target: TargetGraph | None) -> bool:
    """Whether the conform artifact stored a quarantine list."""

    return target is not None and "quarantine" in target.model_fields_set


def conform_exclusive_conflicts(target: TargetGraph | None) -> int | None:
    """Extra targets of owner and ``exclusive`` groups as Conform counted them before normalisation;
    None when the artifact stored no quarantine, or there is no artifact."""

    return target.counters.exclusive_conflicts if target is not None and records_quarantine(target) else None


def quarantine_counts(entries: Iterable[QuarantineEntry]) -> tuple[dict[str, int], dict[str, int]]:
    """Quarantined edges per reason and per ``<RELATION>|<source label>|<target label>``, sorted by key."""

    entries = list(entries)
    by_reason = Counter(entry.reason for entry in entries)
    by_pair = Counter(f"{entry.relation}|{entry.source_label}|{entry.target_label}" for entry in entries)
    return dict(sorted(by_reason.items())), dict(sorted(by_pair.items()))


def quarantine_metrics(
    target: TargetGraph | None, result: BuildResult | None, inference: InferenceReport | None = None
) -> dict[str, dict[str, int] | None]:
    """``quarantine_by_reason`` and ``quarantine_by_pair`` of one build (see the module docstring): the result's build
    metrics when set, else Conform's entries plus inference's (``invalid_proposal``), as the build result merges them;
    None when neither artifact records quarantine.
    """

    if result is not None and result.metrics.quarantine_by_reason is not None:
        return {
            "quarantine_by_reason": dict(sorted(result.metrics.quarantine_by_reason.items())),
            "quarantine_by_pair": dict(sorted((result.metrics.quarantine_by_pair or {}).items())),
        }
    proposed = inference.quarantine if inference is not None else ()
    if not (records_quarantine(target) or proposed):
        return {"quarantine_by_reason": None, "quarantine_by_pair": None}
    by_reason, by_pair = quarantine_counts((*(target.quarantine if target is not None else ()), *proposed))
    return {"quarantine_by_reason": by_reason, "quarantine_by_pair": by_pair}


def spine_unreached(spine: SpineGraph, ontology: Ontology, bases: Collection[str] | None = None) -> list[SpineNode]:
    """Spine sources of ``spine`` that reach no spine root over edges of ``bases`` (None: every edge)."""

    return spine.unreached(ontology, ontology.spine.sources, bases)


def by_label(nodes: Iterable[SpineNode]) -> dict[str, int]:
    """Counts of ``nodes`` per label, sorted by label."""

    return dict(sorted(Counter(node.label for node in nodes).items()))


def fan_out(
    edges: Iterable[tuple[str, str, str]], relation: str, sources: Collection[str] | None = None
) -> dict[int, int]:
    """Number of distinct targets per source of ``relation``, as a histogram: targets to sources.
    ``edges`` are ``(relation, source key, target key)``; ``sources`` limits the source keys
    (None: every source)."""

    targets: dict[str, set[str]] = {}
    for name, source, target in edges:
        if name == relation and (sources is None or source in sources):
            targets.setdefault(source, set()).add(target)
    return dict(sorted(Counter(len(found) for found in targets.values()).items()))


# v2.0 metrics: conform counts and graph shape (labels and relations from flags and roles only)


def conform_counts(target: TargetGraph | None) -> dict[str, int | None]:
    """``owner_conflicts_judged``, ``cycles_broken``, ``unsupported_entities_dropped``,
    ``in_scope_disagreements`` and ``in_scope_dirty_wins`` from the conform artifact's counters
    (module docstring)."""

    out: dict[str, int | None] = dict.fromkeys(COUNTER_FIELDS)
    if target is None:
        return out
    counters = target.counters
    v2_artifact = "quarantine" in target.model_fields_set
    for metric, names in COUNTER_FIELDS.items():
        for name in names:
            if name not in type(counters).model_fields:
                continue
            if not (v2_artifact or name in counters.model_fields_set):
                break
            value = getattr(counters, name)
            if isinstance(value, Mapping):
                value = sum(item for item in value.values() if isinstance(item, int))
            out[metric] = value if isinstance(value, int) and not isinstance(value, bool) else None
            break
    return out


def grounding_counts(target: TargetGraph | None) -> dict[str, Any]:
    """The grounding metrics (``GROUNDING_METRICS``) a conform artifact recorded; None for each one it was written
    without.
    """

    out: dict[str, Any] = dict.fromkeys(GROUNDING_METRICS)
    if target is None:
        return out
    counters = target.counters
    for name in GROUNDING_METRICS:
        if name in counters.model_fields_set:
            value = getattr(counters, name)
            out[name] = dict(sorted(value.items())) if isinstance(value, Mapping) else value
    return out


@dataclass(frozen=True)
class StepArtifacts:
    """Stored step artifacts of one build that some metrics read besides the conform artifact, and what the edge metrics
    read besides them: ``spec_ids``, the ``spec_id`` of each identity key (the registry), to find the ends of the
    inferred edges the build added; ``vectors``, the search ``embedding`` vector of target nodes by identity key
    (``orphans_best_elsewhere``).
    """

    resolved: ResolveArtifact | None = None
    edges: EdgesArtifact | None = None
    nodes: NodesArtifact | None = None
    diff: DiffArtifact | None = None
    inference: InferenceReport | None = None
    spec_ids: Mapping[str, str] | None = None
    vectors: Mapping[str, Vector] | None = None


def step_counts(target: TargetGraph | None, nodes: NodesArtifact | None) -> dict[str, Any]:
    """``label_collapses`` from the extract-nodes artifact, ``derive_fallback_matches`` and
    ``derive_fallback_refused`` from the conform artifact (``STEP_COUNT_METRICS``); None for an artifact
    written without the count, or none."""

    out: dict[str, Any] = dict.fromkeys(STEP_COUNT_METRICS)
    if nodes is not None and "label_collapses" in nodes.counters.model_fields_set:
        out["label_collapses"] = nodes.counters.label_collapses
    for name in ("derive_fallback_matches", "derive_fallback_refused"):
        if target is not None and name in target.counters.model_fields_set:
            out[name] = getattr(target.counters, name)
    return out


def grounding_metrics(
    target: TargetGraph | None,
    ontology: Ontology,
    *,
    plan: PlanArtifact | None = None,
    resolved: ResolveArtifact | None = None,
    edges: EdgesArtifact | None = None,
) -> dict[str, Any]:
    """The grounding metrics of one build: those its conform artifact recorded, else measured on its stored plan,
    resolve and extract-edges artifacts, else None each.
    """

    recorded = grounding_counts(target)
    if any(value is not None for value in recorded.values()) or plan is None or resolved is None or edges is None:
        return recorded
    stats = grounding_stats(plan, resolved, edges, ontology)
    return {name: getattr(stats, name) for name in GROUNDING_METRICS}


# Hub names and known terms


def _spread(counts: Sequence[int]) -> dict[str, float]:
    """``hubs``, ``mean`` (3 decimals), ``max`` and ``share_over_1`` (3 decimals) of per-hub counts."""

    if not counts:
        return {"hubs": 0, "mean": 0.0, "max": 0, "share_over_1": 0.0}
    return {
        "hubs": len(counts),
        "mean": round(sum(counts) / len(counts), 3),
        "max": max(counts),
        "share_over_1": round(sum(1 for count in counts if count > 1) / len(counts), 3),
    }


def _spread_by_label(counts: Mapping[str, Sequence[int]]) -> dict[str, dict[str, float]]:
    every = [count for label in sorted(counts) for count in counts[label]]
    return {ALL_HUBS: _spread(every), **{label: _spread(counts[label]) for label in sorted(counts)}}


def folded_name(name: str) -> str:
    """A raw name with whitespace folded and case folded (``hub_raw_names_per_hub``)."""

    return " ".join(name.split()).casefold()


def hub_name_spread(
    nodes: NodesArtifact | None, resolved: ResolveArtifact | None, ontology: Ontology
) -> dict[str, dict[str, dict[str, float]] | None]:
    """``hub_surfaces_per_hub`` and ``hub_raw_names_per_hub`` (module docstring); None each without
    both artifacts."""

    out: dict[str, dict[str, dict[str, float]] | None] = dict.fromkeys(HUB_NAME_METRICS)
    if nodes is None or resolved is None:
        return out
    names = {raw.raw_id: raw.name for raw in nodes.raw_nodes}
    surfaces: dict[str, list[int]] = {}
    raw_names: dict[str, list[int]] = {}
    for node in resolved.canonical:
        if node.label not in ontology.hub_labels:
            continue
        member_names = [names[raw_id] for raw_id in node.member_ids if raw_id in names]
        if not member_names:
            continue
        surfaces.setdefault(node.label, []).append(len({hub_surface(name) for name in member_names}))
        raw_names.setdefault(node.label, []).append(len({folded_name(name) for name in member_names}))
    return {"hub_surfaces_per_hub": _spread_by_label(surfaces), "hub_raw_names_per_hub": _spread_by_label(raw_names)}


def catalog_counts(nodes: NodesArtifact | None) -> dict[str, int | None]:
    """The hub catalog counters (``CATALOG_METRICS``) of the extract-nodes artifact; None for each one
    the build did not measure, or without the artifact."""

    out: dict[str, int | None] = dict.fromkeys(CATALOG_METRICS)
    if nodes is None:
        return out
    for name in CATALOG_METRICS:
        value = getattr(nodes.counters, name, None)
        out[name] = value if isinstance(value, int) else None
    return out


def stores_known_terms(nodes: NodesArtifact | None) -> bool:
    """Whether the extract-nodes artifact stores the known terms shown per unit to either pass
    ( and later)."""

    return nodes is not None and (nodes.known_terms is not None or nodes.hub_known_terms is not None)


def known_terms_shown(nodes: NodesArtifact | None) -> dict[str, dict[str, float] | None]:
    """``known_terms_mentioned`` and ``known_terms_nearby``: ``units``, ``mean`` (3 decimals) and
    ``max`` per unit; None each when the artifact stores no known terms."""

    if nodes is None or nodes.known_terms is None:
        return dict.fromkeys(KNOWN_TERM_METRICS)
    shown = list(nodes.known_terms.values())

    def stats(counts: Sequence[int]) -> dict[str, float]:
        if not counts:
            return {"units": 0, "mean": 0.0, "max": 0}
        return {"units": len(counts), "mean": round(sum(counts) / len(counts), 3), "max": max(counts)}

    return {
        "known_terms_mentioned": stats([len(item.mentioned) for item in shown]),
        "known_terms_nearby": stats([len(item.nearby) for item in shown]),
    }


@dataclass(frozen=True, slots=True)
class TermUse:
    """A use of a known term by a raw node of the ``node_pass`` pass (``answers.HUBS_LIST`` or ``STATEMENTS_LIST``)
    against the terms that pass showed its unit: a hub named by a term shown by nearness, or a statement whose name
    contains one (``kind`` ``NEARBY_USE``; a statement gives one use per nearby term it contains), or a hub named by
    another name of a shown term (``ALIAS_USE``).
    """

    kind: str
    node_pass: str
    raw_id: str
    unit_id: str
    label: str
    name: str
    term: str
    fragment_ids: tuple[str, ...]
    supported: bool | None = None


@dataclass(frozen=True)
class TermNames:
    """Other names of known terms, by the term's hub surface: ``aliases``, names as written (catalog aliases);
    ``values``, ``surface`` alias values already in hub form (persisted hubs).
    """

    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    values: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    support: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def alias_surfaces(self, surface: str) -> set[str]:
        """Hub surfaces of the other names of the term of ``surface`` (support-only values left out)."""

        return {hub_surface(alias) for alias in self.aliases.get(surface, ())} | set(self.values.get(surface, ()))

    def unit_index(self, terms: Iterable[str]) -> NameIndex:
        """A ``NameIndex`` finding each of ``terms`` (its key) by its name and its other names
        (support-only values left out)."""

        listed = [(term, hub_surface(term)) for term in terms]
        return NameIndex(
            ((name, term) for term, surface in listed for name in (term, *self.aliases.get(surface, ()))),
            ((value, term) for term, surface in listed for value in self.values.get(surface, ())),
        )

    def index(self, term: str) -> NameIndex:
        """A ``NameIndex`` finding ``term`` by its name and every other name of it."""

        surface = hub_surface(term)
        values = (*self.values.get(surface, ()), *self.support.get(surface, ()))
        return NameIndex(
            ((name, term) for name in (term, *self.aliases.get(surface, ()))), ((value, term) for value in values)
        )


def _joined(pairs: Iterable[tuple[str, Iterable[str]]]) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for surface, names in pairs:
        listed = found.setdefault(surface, [])
        listed.extend(name for name in names if name and name not in listed)
    return {surface: tuple(names) for surface, names in found.items()}


def term_names(nodes: NodesArtifact, ontology: Ontology, resolved: ResolveArtifact | None = None) -> TermNames:
    """The other names of known terms (``TermNames``): catalog aliases, the ``surface`` alias values of
    persisted hubs, and for support the ``surface`` values of the identity key and aliases of each
    canonical hub (``resolved``)."""

    hubs = [node for node in (resolved.canonical if resolved is not None else ()) if node.label in ontology.hub_labels]
    return TermNames(
        aliases=_joined((hub_surface(hub.name), hub.aliases) for hub in nodes.hub_catalog),
        values=_joined((hub_surface(hub.name), hub.aliases) for hub in nodes.persisted_hubs or ()),
        support=_joined(
            (hub_surface(node.name), surface_values((node.identity_key, *node.identity_aliases))) for node in hubs
        ),
    )


def term_uses(
    nodes: NodesArtifact | None,
    ontology: Ontology,
    plan: PlanArtifact | None = None,
    resolved: ResolveArtifact | None = None,
) -> list[TermUse]:
    """The nearby-term and alias uses of a build (``TermUse``), in raw-node order: hub nodes against the hub pass's
    known terms (``NodesArtifact.hub_known_terms``), statement nodes against the statement pass's (``known_terms``);
    none for a pass whose terms the artifact does not store.
    """

    if nodes is None:
        return []
    names = term_names(nodes, ontology, resolved)
    indexes: dict[str, NameIndex] = {}
    uses: list[TermUse] = []
    for raw in nodes.raw_nodes:
        if raw.reused:
            continue
        if raw.label in ontology.hub_labels:
            shown = (nodes.hub_known_terms or {}).get(raw.unit_id)
            if shown is not None:
                uses.extend(_hub_uses(raw, shown, names))
            continue
        shown = (nodes.known_terms or {}).get(raw.unit_id)
        if shown is None or not shown.nearby:
            continue
        if raw.unit_id not in indexes:
            indexes[raw.unit_id] = names.unit_index((*shown.mentioned, *shown.nearby))
        found = indexes[raw.unit_id].count(hub_words(raw.name))
        uses.extend(
            TermUse(NEARBY_USE, STATEMENTS_LIST, raw.raw_id, raw.unit_id, raw.label, raw.name, term, raw.fragment_ids)
            for term in shown.nearby
            if found[term]
        )
    return _with_support(uses, names, plan)


def _hub_uses(raw: RawNode, shown: KnownTermsShown, names: TermNames) -> list[TermUse]:
    """The use of a known term by the hub raw node ``raw`` (at most one), against its unit's hub pass terms."""

    surface = hub_surface(raw.name)
    terms = {hub_surface(term): term for term in (*shown.mentioned, *shown.nearby)}
    nearby = {hub_surface(term): term for term in shown.nearby}
    kind, term = None, None
    if surface in nearby:
        kind, term = NEARBY_USE, nearby[surface]
    elif surface not in terms:
        term = next((name for key, name in sorted(terms.items()) if surface in names.alias_surfaces(key)), None)
        kind = ALIAS_USE if term is not None else None
    if kind is None or term is None:
        return []
    return [TermUse(kind, HUBS_LIST, raw.raw_id, raw.unit_id, raw.label, raw.name, term, raw.fragment_ids)]


def _with_support(uses: Sequence[TermUse], names: TermNames, plan: PlanArtifact | None) -> list[TermUse]:
    """``uses`` with ``supported`` set on the nearby ones: a fragment of the raw node names the term
    or another name of it (``TermNames.index``, one per term); unchanged without the plan."""

    if plan is None:
        return list(uses)
    fragments = {fragment.fragment_id: fragment for fragment in plan.fragments}
    words: dict[str, list[str]] = {}
    indexes: dict[str, NameIndex] = {}
    out = []
    for use in uses:
        if use.kind != NEARBY_USE:
            out.append(use)
            continue
        if use.term not in indexes:
            indexes[use.term] = names.index(use.term)
        index = indexes[use.term]
        supported = False
        for fid in use.fragment_ids:
            if fid not in fragments:
                continue
            if fid not in words:
                words[fid] = fragment_words(fragments[fid])
            if index.count(words[fid]):
                supported = True
                break
        out.append(replace(use, supported=supported))
    return out


def naming_metrics(
    nodes: NodesArtifact | None,
    resolved: ResolveArtifact | None,
    ontology: Ontology,
    plan: PlanArtifact | None = None,
) -> dict[str, Any]:
    """The hub-name and known-term metrics (``NAMING_METRICS``, module docstring) of one build."""

    out: dict[str, Any] = dict(hub_name_spread(nodes, resolved, ontology))
    consolidated = resolved is not None and "consolidation" in resolved.counters.model_fields_set
    out["hub_consolidation_merges"] = resolved.counters.consolidation.merges if consolidated and resolved else None
    out |= catalog_counts(nodes)
    out |= known_terms_shown(nodes)
    out |= dict.fromkeys(TERM_USE_METRICS)
    if nodes is None or not stores_known_terms(nodes):
        return out
    uses = term_uses(nodes, ontology, plan, resolved)
    measured = {HUBS_LIST: nodes.hub_known_terms is not None, STATEMENTS_LIST: nodes.known_terms is not None}
    for node_pass, metric in ((HUBS_LIST, "nearby_term_uses_hubs"), (STATEMENTS_LIST, "nearby_term_uses_statements")):
        if not measured[node_pass]:
            continue
        nearby = [use for use in uses if use.kind == NEARBY_USE and use.node_pass == node_pass]
        out[metric] = len({use.raw_id for use in nearby})
        out[f"{metric}_unsupported"] = (
            len({use.raw_id for use in nearby if use.supported is False}) if plan is not None else None
        )
    parts = [
        out[metric] for metric in ("nearby_term_uses_hubs", "nearby_term_uses_statements") if out[metric] is not None
    ]
    out["nearby_term_uses"] = sum(parts)
    if plan is not None:
        out["nearby_term_uses_unsupported"] = sum(
            out[f"{metric}_unsupported"]
            for metric in ("nearby_term_uses_hubs", "nearby_term_uses_statements")
            if out[metric] is not None
        )
    if measured[HUBS_LIST]:
        out["alias_name_uses"] = sum(1 for use in uses if use.kind == ALIAS_USE)
    return out


def identity_metrics(
    resolved: ResolveArtifact | None, target: TargetGraph | None, result: BuildResult | None = None
) -> dict[str, Any]:
    """The identity metrics (``IDENTITY_METRICS``, module docstring) of one build."""

    out: dict[str, Any] = dict.fromkeys(IDENTITY_METRICS)
    if resolved is not None and "consolidation" in resolved.counters.model_fields_set:
        consolidation = resolved.counters.consolidation
        for name in RESOLVE_IDENTITY_FIELDS:
            if name in consolidation.model_fields_set:
                out[name] = getattr(consolidation, name)
        if "merges" in resolved.model_fields_set and (resolved.merges or not consolidation.merges):
            sources = Counter(merge.source for merge in resolved.merges)
            out["hub_merges_by_source"] = {source: sources[source] for source in HUB_MERGE_SOURCES}
    if target is not None:
        for name in CONFORM_IDENTITY_FIELDS:
            if name in target.counters.model_fields_set:
                out[name] = getattr(target.counters, name)
    out["dedup_refused_by_guard"] = refused_by_guard(target, result)
    return out


def refused_by_guard(target: TargetGraph | None, result: BuildResult | None = None) -> dict[str, int] | None:
    """Guard-refused dedup pairs per guard (sorted): the result's build metric when set, else counted on
    the conform artifact's ``dedup_refused``; None when neither records them."""

    if result is not None and result.metrics.dedup_refused_by_guard is not None:
        return dict(sorted(result.metrics.dedup_refused_by_guard.items()))
    if target is None or "dedup_refused" not in target.model_fields_set:
        return None
    return dict(sorted(Counter(refused.guard for refused in target.dedup_refused).items()))


@dataclass(frozen=True, slots=True)
class MetricNode:
    """A node as the graph-shape metrics read it: key, label, name and condition attribute values."""

    key: str
    label: str
    name: Any = None
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)


def owned_sources(ontology: Ontology) -> frozenset[str]:
    """Spine-source labels that have an ``owner`` pair."""

    return frozenset(label for label in ontology.spine.sources if ontology.owner_pairs(label))


def provider_pairs(ontology: Ontology) -> tuple[tuple[str, str, str], ...]:
    """``(relation, source label, target label)`` of the provider pairs (module docstring)."""

    roots = ontology.spine.roots
    found = []
    for key in sorted(ontology.exclusive_pairs):
        relation, source, target = key
        pair = ontology.pair_rule(relation, source, target)
        if pair is None or pair.owner or source in roots or target not in roots:
            continue
        if ontology.node_type(source).role == ANCHOR_ROLE:
            found.append(key)
    return tuple(found)


def statement_checked_labels(ontology: Ontology) -> frozenset[str]:
    """``group``-role labels that are themselves owned (the source of an ``owner`` pair)."""

    return frozenset(
        label
        for label in ontology.labels
        if ontology.node_type(label).role == GROUP_ROLE and ontology.owner_pairs(label)
    )


def named_like_statement(name: Any) -> bool:
    """A name with a modal verb (``MODAL_VERBS``) or a final period."""

    if not isinstance(name, str):
        return False
    text = name.strip()
    return bool(_MODAL.search(text)) or text.endswith(STATEMENT_END)


def fallback_relation(ontology: Ontology) -> str | None:
    spec = ontology.conform_spec()
    return spec.fallback_relation if spec is not None and spec.fallback_relation in ontology.relations else None


def supported_labels(ontology: Ontology) -> frozenset[str]:
    """Labels that declare ``supported_by``."""

    return frozenset(label for label in ontology.labels if ontology.supported_by(label))


def root_otherwise_relations(ontology: Ontology) -> frozenset[str]:
    """Relations that pairs between spine-root labels name as ``otherwise``."""

    roots = ontology.spine.roots
    return frozenset(
        pair.otherwise
        for relation in ontology.relations.values()
        for pair in relation.pairs
        if pair.otherwise and pair.source in roots and pair.target in roots and pair.otherwise in ontology.relations
    )


def _oriented_fan_out(
    edges: Iterable[tuple[str, str, str]],
    labels: Mapping[str, str],
    relations: Collection[str],
    sources: Collection[str],
    targets: Collection[str],
    *,
    ontology: Ontology,
) -> dict[int, int]:
    """``fan_out`` over ``relations`` from nodes of ``sources`` labels to nodes of ``targets`` labels; an
    edge of a symmetric relation counts in the direction that fits."""

    oriented: list[tuple[str, str, str]] = []
    for relation, source, target in edges:
        if relation not in relations:
            continue
        source_label, target_label = labels.get(source), labels.get(target)
        if source_label in sources and target_label in targets:
            oriented.append((relation, source, target))
        elif ontology.is_symmetric(relation) and target_label in sources and source_label in targets:
            oriented.append((relation, target, source))
    targets_of: dict[str, set[str]] = {}
    for _, source, target in oriented:
        targets_of.setdefault(source, set()).add(target)
    return dict(sorted(Counter(len(found) for found in targets_of.values()).items()))


def graph_metrics(
    nodes: Iterable[MetricNode], edges: Iterable[tuple[str, str, str]], ontology: Ontology
) -> dict[str, Any]:
    """The v2.0 graph-shape metrics (``GRAPH_METRICS``, module docstring) of ``nodes`` and ``edges``
    (``(relation, source key, target key)``; an edge with an end missing from ``nodes`` is left out)."""

    nodes = list(nodes)
    labels = {node.key: node.label for node in nodes}
    edges = [edge for edge in edges if edge[1] in labels and edge[2] in labels]
    out: dict[str, Any] = dict.fromkeys(GRAPH_METRICS)
    roots = ontology.spine.roots

    owned = owned_sources(ontology)
    if owned:
        owners = {
            source
            for relation, source, target in edges
            if (pair := ontology.pair_rule(relation, labels[source], labels[target])) is not None and pair.owner
        }
        exclude = ontology.spine_exclude()
        out["ownerless_requirements"] = sum(
            1
            for node in nodes
            if node.label in owned
            and node.key not in owners
            and not ontology.condition_matches(exclude, NodeView(node.label, node.attributes))
        )

    providers = provider_pairs(ontology)
    if providers:
        provided = {source for _, source, _ in providers}
        holders = {
            source for relation, source, target in edges if (relation, labels[source], labels[target]) in providers
        }
        out["interfaces_without_provider"] = sum(
            1 for node in nodes if node.label in provided and node.key not in holders
        )

    checked = statement_checked_labels(ontology)
    if checked:
        out["features_named_like_statements"] = sum(
            1 for node in nodes if node.label in checked and named_like_statement(node.name)
        )

    fallback = fallback_relation(ontology)
    sources = ontology.spine.sources
    if fallback is not None:
        out["relates_fan_out"] = _oriented_fan_out(edges, labels, (fallback,), sources, roots, ontology=ontology)
        supported = supported_labels(ontology)
        if supported:
            out["entity_relates_fan_out"] = _oriented_fan_out(
                edges, labels, (fallback,), sources, supported, ontology=ontology
            )
    deployed = root_otherwise_relations(ontology)
    if deployed:
        out["deployed_to_fan_out"] = _oriented_fan_out(edges, labels, deployed, roots, roots, ontology=ontology)
    return out


def target_metric_nodes(target: TargetGraph) -> list[MetricNode]:
    """The target nodes as the graph-shape metrics read them, keyed by identity key."""

    return [
        MetricNode(key=node.identity_key, label=node.label, name=node.name, attributes=node.attributes)
        for node in target.nodes
    ]


# Edges and cross-document links


MeasuredEdge = tuple[str, str, str, str]
"""An edge as the edge metrics read it: ``(relation, source key, target key, basis)``."""


def node_documents(target: TargetGraph, plan: PlanArtifact | None = None) -> dict[str, frozenset[str]]:
    """The documents of each target node, by identity key: those of its evidence fragments (``plan``)
    and of its statements."""

    fragment_documents = {fragment.fragment_id: fragment.document_id for fragment in plan.fragments} if plan else {}
    return {
        node.identity_key: frozenset(
            {
                *(fragment_documents[e.fragment_id] for e in node.evidence if e.fragment_id in fragment_documents),
                *(statement.document_id for statement in node.statements),
            }
        )
        for node in target.nodes
    }


def requirement_edges(
    nodes: Iterable[MetricNode], edges: Iterable[tuple[str, str, str]], ontology: Ontology
) -> dict[str, float | None]:
    """``edges_per_requirement`` and ``requirements_without_edge`` (module docstring) over the
    requirements ``_owned_requirements`` gives; None each without them."""

    requirements = {node.key for node in _owned_requirements(nodes, ontology)}
    out: dict[str, float | None] = {"edges_per_requirement": None, "requirements_without_edge": None}
    if not requirements:
        return out
    touching: Counter[str] = Counter()
    for _, source, target in edges:
        touching.update(end for end in {source, target} if end in requirements)
    out["edges_per_requirement"] = round(sum(touching.values()) / len(requirements), SHARE_DIGITS)
    out["requirements_without_edge"] = round(
        sum(1 for key in requirements if not touching[key]) / len(requirements), SHARE_DIGITS
    )
    return out


def _owned_requirements(nodes: Iterable[MetricNode], ontology: Ontology) -> list[MetricNode]:
    """Requirement nodes the spine ``exclude`` does not match, in key order."""

    owned = owned_sources(ontology)
    exclude = ontology.spine_exclude()
    return sorted(
        (
            node
            for node in nodes
            if node.label in owned and not ontology.condition_matches(exclude, NodeView(node.label, node.attributes))
        ),
        key=lambda node: node.key,
    )


def owner_bases(
    nodes: Iterable[MetricNode], edges: Iterable[MeasuredEdge], ontology: Ontology
) -> dict[str, str] | None:
    """The owner basis of each requirement the spine ``exclude`` does not match (``OWNER_BASES`` by
    precedence, else ``NO_OWNER``), by key; None when the ontology has no owned spine-source label."""

    if not owned_sources(ontology):
        return None
    nodes = list(nodes)
    labels = {node.key: node.label for node in nodes}
    found: dict[str, set[str]] = {}
    for relation, source, target, basis in edges:
        if source not in labels or target not in labels:
            continue
        pair = ontology.pair_rule(relation, labels[source], labels[target])
        if pair is not None and pair.owner:
            found.setdefault(source, set()).add(basis)
    return {
        node.key: next((basis for basis in OWNER_BASES if basis in found.get(node.key, ())), NO_OWNER)
        for node in _owned_requirements(nodes, ontology)
    }


def owners_by_source(bases: Mapping[str, str] | None) -> dict[str, int] | None:
    """Requirements per owner basis (``owner_bases``), every basis and ``NO_OWNER`` listed."""

    if bases is None:
        return None
    counts = Counter(bases.values())
    return {basis: counts[basis] for basis in (*OWNER_BASES, NO_OWNER)}


def cross_document_edges(
    edges: Iterable[MeasuredEdge], documents: Mapping[str, Collection[str]]
) -> tuple[dict[str, dict[str, int]] | None, float | None]:
    """Edges whose ends share no document, per relation and basis (sorted), and their share of the edges whose ends both
    have documents; an end without documents is left out.
    """

    counts: Counter[tuple[str, str]] = Counter()
    documented = 0
    for relation, source, target, basis in edges:
        first, second = documents.get(source), documents.get(target)
        if not first or not second:
            continue
        documented += 1
        if not set(first) & set(second):
            counts[(relation, basis)] += 1
    if not documented:
        return None, None
    out: dict[str, dict[str, int]] = {}
    for (relation, basis), count in sorted(counts.items()):
        out.setdefault(relation, {})[basis] = count
    return out, round(sum(counts.values()) / documented, SHARE_DIGITS)


def orphans_best_elsewhere(
    nodes: Iterable[MetricNode],
    bases: Mapping[str, str] | None,
    ontology: Ontology,
    documents: Mapping[str, Collection[str]],
    vectors: Mapping[str, Vector] | None,
) -> dict[str, int] | None:
    """``orphans``, ``measured`` and ``elsewhere`` (module docstring) of the requirements ``bases`` names without an
    owner, ranked by their ``embedding`` ``vectors``; None without ``bases`` or ``vectors``.
    """

    if bases is None or vectors is None:
        return None
    nodes = list(nodes)
    by_key = {node.key: node for node in nodes}
    orphans = [key for key, basis in sorted(bases.items()) if basis == NO_OWNER]
    exclude = ontology.spine_exclude()
    candidates: dict[str, list[MetricNode]] = {}
    for node in sorted(nodes, key=lambda item: item.key):
        view = NodeView(node.label, node.attributes)
        if node.key in vectors and documents.get(node.key) and not ontology.condition_matches(exclude, view):
            candidates.setdefault(node.label, []).append(node)
    unit: dict[str, np.ndarray] = {}

    def normalised(key: str) -> np.ndarray:
        if key not in unit:
            vector = np.asarray(vectors[key], dtype=np.float64)
            norm = float(np.linalg.norm(vector))
            unit[key] = vector / norm if norm else vector
        return unit[key]

    measured = elsewhere = 0
    for key in orphans:
        if key not in vectors or not documents.get(key):
            continue
        orphan = by_key[key]
        view = NodeView(orphan.label, orphan.attributes)
        owners = sorted(
            {
                owner.key
                for pair in ontology.owner_pairs(orphan.label)
                for owner in candidates.get(pair.target, ())
                if owner.key != key and ontology.where_holds(pair, view, NodeView(owner.label, owner.attributes))
            }
        )
        if not owners:
            continue
        measured += 1
        scores = [float(normalised(key) @ normalised(owner)) for owner in owners]
        best = min(zip(scores, owners, strict=True), key=lambda item: (-item[0], item[1]))[1]
        if not set(documents.get(key, ())) & set(documents.get(best, ())):
            elsewhere += 1
    return {"orphans": len(orphans), "measured": measured, "elsewhere": elsewhere}


def inferred_edges(result: BuildResult | None, spec_ids: Mapping[str, str] | None) -> list[MeasuredEdge] | None:
    """The inferred edges the result names as added this build, ends by identity key (``spec_ids``
    inverted; an end not found is left out with its edge); None when there are some but no map."""

    refs = result.spec_changes.inferred_added if result is not None else ()
    if not refs:
        return []
    if spec_ids is None:
        return None
    keys = {spec_id: key for key, spec_id in spec_ids.items()}
    found = []
    for ref in refs:
        parts = ref.split("|")
        if len(parts) == EDGE_REF_PARTS and parts[0] in keys and parts[2] in keys:
            found.append((parts[1], keys[parts[0]], keys[parts[2]], "inferred"))
    return found


def carried_nothing_over(result: BuildResult | None, target: TargetGraph, diff: DiffArtifact | None) -> bool:
    """Whether the graph Persist left holds only the target's edges, so that they plus the inferred edges the result
    names as added are the persisted graph after inference: a ``first`` build (no graph before it), or a diff whose
    edges after persist (``DiffArtifact.edges_by_relation``) are the target's alone, keeping no earlier inferred (or
    unextracted stated) edge.
    """

    if result is None:
        return False
    if result.mode == "first":
        return True
    if diff is None or "edges_by_relation" not in diff.model_fields_set:
        return False
    return sum(diff.edges_by_relation.values()) == len(target.edges)


def _counter_values(model: ArtifactModel | None, names: Iterable[str]) -> dict[str, Any]:
    """The fields ``names`` of a counters model, sorted when a mapping; None each without the model."""

    out: dict[str, Any] = {}
    for name in names:
        value = getattr(model, name, None) if model is not None else None
        out[name] = _sorted_value(value)
    return out


def _sorted_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _sorted_value(item) for key, item in sorted(value.items())}
    return value


def window_causes(edges: EdgesArtifact | None) -> dict[str, int] | None:
    """Called edge windows per cause (``UNRECORDED_CAUSE`` without one), sorted; None when the artifact
    records no windows."""

    if edges is None or edges.called_windows is None:
        return None
    return dict(sorted(Counter(call.cause or UNRECORDED_CAUSE for call in edges.called_windows).items()))


def rule_items(inference: InferenceReport | None) -> dict[str, dict[str, int]] | None:
    """Judged items per outcome of each inference rule that records them; None when none does."""

    if inference is None:
        return None
    found = {
        rule.rule: dict(sorted(rule.items_by_outcome.items()))
        for rule in inference.rules
        if rule.items_by_outcome is not None
    }
    return dict(sorted(found.items())) if found else None


def edge_metrics(
    target: TargetGraph,
    result: BuildResult | None,
    ontology: Ontology,
    *,
    plan: PlanArtifact | None = None,
    steps: StepArtifacts | None = None,
    prefer_verification: bool = True,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The edge and cross-document metrics (``EDGE_METRICS``, module docstring) of one build, and where
    ``owners_by_source`` and ``cross_document_edges`` came from (``metric_sources``)."""

    steps = steps or StepArtifacts()
    nodes = target_metric_nodes(target)
    stated = [(edge.relation, edge.source_key, edge.target_key, edge.basis) for edge in target.edges]
    after = carried_nothing_over(result, target, steps.diff)
    inferred = inferred_edges(result, steps.spec_ids) if after else None
    measured = [*stated, *(inferred or ())]
    source = ARTIFACTS_SOURCE if inferred is not None else CONFORM_SOURCE
    documents = node_documents(target, plan)
    bases = owner_bases(nodes, measured, ontology)
    crossing, share = cross_document_edges(measured, documents)
    out: dict[str, Any] = {
        **requirement_edges(nodes, target_edges(target), ontology),
        "owners_by_source": owners_by_source(bases),
        "cross_document_edges": crossing,
        "cross_document_share": share,
        "orphans_best_elsewhere": orphans_best_elsewhere(nodes, bases, ontology, documents, steps.vectors),
    }
    sources = {name: source if out[name] is not None else NO_SOURCE for name in MEASURED_EDGE_METRICS}
    verified = result.verification.metrics if result is not None and result.verification is not None else None
    for name in MEASURED_EDGE_METRICS:
        stored = getattr(verified, name, None) if verified is not None and prefer_verification else None
        if stored is not None:
            out[name] = _sorted_value(stored)
            sources[name] = VERIFY_SOURCE
    out |= _counter_values(target.counters, EDGE_COUNTER_METRICS)
    out |= _counter_values(steps.edges.counters if steps.edges is not None else None, WINDOW_METRICS)
    out["windows_called_by_cause"] = window_causes(steps.edges)
    out["items_by_outcome"] = rule_items(steps.inference)
    out["modified_causes"] = _sorted_value(steps.diff.modified_causes) if steps.diff is not None else None
    return out, sources


# Inference and operation


def confidence_band(confidence: float | None) -> str:
    """The ``CONFIDENCE_BANDS`` label of a judged item's confidence; ``NO_CONFIDENCE`` without one."""

    if confidence is None:
        return NO_CONFIDENCE
    return next((label for bound, label in CONFIDENCE_BANDS if confidence >= bound), CONFIDENCE_BANDS[-1][1])


def _records_bounds(rule: RuleReport) -> bool:
    """A rule report written with the  bound fields (any of them set)."""

    return rule.limit is not None or rule.budget_tokens is not None or rule.backlog is not None


def _per_rule(rules: Iterable[RuleReport], name: str) -> dict[str, int] | None:
    found = {rule.rule: getattr(rule, name) for rule in rules if getattr(rule, name) is not None}
    return dict(sorted(found.items())) if found else None


def items_by_band(inference: InferenceReport | None) -> dict[str, dict[str, dict[str, int]]] | None:
    """Judged items per rule, confidence band and outcome, sorted; None when the report records none."""

    if inference is None or inference.judged_items is None:
        return None
    counts: Counter[tuple[str, str, str]] = Counter(
        (item.rule, confidence_band(item.confidence), item.outcome) for item in inference.judged_items
    )
    out: dict[str, dict[str, dict[str, int]]] = {}
    for (rule, band, outcome), count in sorted(counts.items()):
        out.setdefault(rule, {}).setdefault(band, {})[outcome] = count
    return out


def input_share(inference: InferenceReport | None, result: BuildResult | None) -> dict[str, float] | None:
    """Per rule recording ``budget_tokens``, those over the build's input tokens, and ``ALL_RULES``, the
    infer step's input over the same total, 3 decimals; None without a result or input."""

    total = sum(usage.input_tokens for usage in result.metrics.llm.values()) if result is not None else 0
    if result is None or not total:
        return None
    infer = result.metrics.llm.get(INFER_STEP)
    out = {ALL_RULES: round((infer.input_tokens if infer is not None else 0) / total, SHARE_DIGITS)}
    for rule in inference.rules if inference is not None else ():
        if rule.budget_tokens is not None:
            out[rule.rule] = round(rule.budget_tokens / total, SHARE_DIGITS)
    return dict(sorted(out.items()))


def settlement_counts(result: BuildResult | None) -> dict[str, Any] | None:
    """The result's settlement as counts (``ConsolidatedMetrics.settlement``); None without one."""

    settlement = result.settlement if result is not None else None
    if settlement is None:
        return None
    return {
        "settled": int(settlement.state == "settled"),
        "documents": len(settlement.documents),
        "follow_up_builds": len(settlement.follow_up_builds),
        "inference_backlog": sum(settlement.inference_backlog.values()),
        "reasons": dict.fromkeys(sorted(settlement.reasons), 1),
    }


def inference_metrics(inference: InferenceReport | None, result: BuildResult | None) -> dict[str, Any]:
    """The inference and operation metrics (``INFERENCE_METRICS``, module docstring) of one build."""

    rules = inference.rules if inference is not None else ()
    bounded = [rule for rule in rules if _records_bounds(rule)]
    backlog = inference.backlog if inference is not None else None
    report = result.change_report if result is not None else None
    return {
        "inference_backlog": None
        if backlog is None
        else {
            rule: {"subjects": len(queue.spec_ids), "age_builds": queue.age_builds}
            for rule, queue in sorted(backlog.items())
        },
        "inference_limit_hits": None
        if not bounded
        else {rule.rule: {rule.limit: 1} for rule in sorted(bounded, key=lambda rule: rule.rule) if rule.limit},
        "inference_budget_tokens": _per_rule(rules, "budget_tokens"),
        "inference_read_edges": _per_rule(rules, "read_edges"),
        "inference_changed_queued": _per_rule(rules, "changed_queued"),
        "inference_items_by_band": items_by_band(inference),
        "spine_unreachable_planned": inference.spine_unreachable_after if inference is not None else None,
        "inference_input_share": input_share(inference, result),
        "settlement": settlement_counts(result),
        "change_report_nodes": dict(sorted(report.counts.items())) if report is not None else None,
        "change_report_edges": dict(sorted(report.edge_counts.items())) if report is not None else None,
    }


# The target graph as the metrics read it


def spine_view(target: TargetGraph, ontology: Ontology) -> SpineGraph:
    """The target graph as the spine walk reads it, keyed (and ordered) by identity key, each node with
    the values of the attributes the ontology's conditions name (so the spine ``exclude`` applies, also
    through the owner, as on the persisted graph)."""

    names = condition_attributes(ontology)
    return SpineGraph(
        nodes=tuple(
            SpineNode(
                key=node.identity_key,
                spec_id=node.identity_key,
                label=node.label,
                attributes={name: node.attributes.get(name) for name in names},
            )
            for node in target.nodes
        ),
        edges=tuple(
            SpineEdge(relation=edge.relation, source=edge.source_key, target=edge.target_key, basis=edge.basis)
            for edge in target.edges
        ),
    )


def target_edges(target: TargetGraph) -> list[tuple[str, str, str]]:
    """The target edges as ``(relation, source key, target key)``."""

    return [(edge.relation, edge.source_key, edge.target_key) for edge in target.edges]


def edges_by_relation_basis(target: TargetGraph) -> dict[str, dict[str, int]]:
    """Target edges per relation and basis."""

    counts: Counter[tuple[str, str]] = Counter((edge.relation, edge.basis) for edge in target.edges)
    out: dict[str, dict[str, int]] = {}
    for (relation, basis), count in sorted(counts.items()):
        out.setdefault(relation, {})[basis] = count
    return out


def inferred_added_by_relation(result: BuildResult | None) -> dict[str, int]:
    """Inferred edges the build added, per relation (``SpecChanges.inferred_added``, ``edge_ref`` form)."""

    if result is None:
        return {}
    parts = (ref.split("|") for ref in result.spec_changes.inferred_added)
    return dict(sorted(Counter(part[1] for part in parts if len(part) == EDGE_REF_PARTS).items()))


def active_fragments(plan: PlanArtifact, result: BuildResult | None = None) -> list[str]:
    """Fragment ids active after the build, as the build result counts them: a fragment the result
    skipped is skipped, an extracted (dirty) one active, any other keeps its planned status."""

    skipped = result.skipped_fragments if result is not None else {}
    dirty = set(plan.dirty_ids)
    active = []
    for fragment in plan.fragments:
        fid = fragment.fragment_id
        status = SKIPPED if fid in skipped else ACTIVE if fid in dirty else fragment.status
        if status == ACTIVE:
            active.append(fid)
    return active


def fragments_without_nodes(active: Iterable[str], target: TargetGraph) -> int:
    """Fragments of ``active`` no target node has evidence in."""

    grounded = {lines.fragment_id for node in target.nodes for lines in node.evidence}
    return sum(1 for fid in active if fid not in grounded)


def _verify_metrics(
    target: TargetGraph,
    result: BuildResult | None,
    ontology: Ontology,
    plan: PlanArtifact | None,
    unreached: tuple[Sequence[SpineNode], Sequence[SpineNode] | None, str],
    *,
    prefer_verification: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """``unreached``: the spine sources unreached before inference and over every edge (None without a spine view), and
    the source name of the view.
    """

    before, every, view = unreached
    verified = result.verification.metrics if result is not None and result.verification is not None else None
    if verified is not None and prefer_verification:
        stored_before = verified.spine_unreachable_before
        values: dict[str, Any] = {
            "spine_unreachable_before": len(before) if stored_before is None else stored_before,
            "spine_unreachable": verified.spine_unreachable,
            "exclusive_conflicts": verified.exclusive_conflicts,
            "label_disagreement": verified.label_disagreement,
            "dropped_edges": dict(sorted(verified.dropped_edges.items())),
            "fragments_without_nodes": verified.fragments_without_nodes,
        }
        sources = dict.fromkeys(values, VERIFY_SOURCE)
        if stored_before is None:
            sources["spine_unreachable_before"] = view
        return values, sources
    sources = dict.fromkeys(CHECK_METRICS, ARTIFACTS_SOURCE)
    sources["spine_unreachable_before"] = view
    spine_unreachable = len(every) if every is not None else None
    sources["spine_unreachable"] = view
    if spine_unreachable is None:
        spine_unreachable = result.metrics.spine_unreachable if result is not None else None
        sources["spine_unreachable"] = BUILD_METRICS_SOURCE if spine_unreachable is not None else NO_SOURCE
    labels = {node.identity_key: node.label for node in target.nodes}
    values = {
        "spine_unreachable_before": len(before),
        "spine_unreachable": spine_unreachable,
        "exclusive_conflicts": conform_exclusive_conflicts(target)
        if records_quarantine(target)
        else exclusive_conflicts(target_edges(target), labels, ontology),
        "label_disagreement": sum(1 for node in target.nodes if node.label_disagreement is not None),
        "dropped_edges": dropped_by_relation(target),
        "fragments_without_nodes": None
        if plan is None
        else fragments_without_nodes(active_fragments(plan, result), target),
    }
    if plan is None:
        sources["fragments_without_nodes"] = NO_SOURCE
    return values, sources


def _graph_shape(
    target: TargetGraph, result: BuildResult | None, ontology: Ontology, *, prefer_verification: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """The graph-shape metrics and where each came from: the stored Verify value when the result carries one (not None)
    and ``prefer_verification``, else measured on the target graph (None: ``n/a``).
    """

    measured = graph_metrics(target_metric_nodes(target), target_edges(target), ontology)
    verified = result.verification.metrics if result is not None and result.verification is not None else None
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in GRAPH_METRICS:
        stored = getattr(verified, name, None) if verified is not None and prefer_verification else None
        if stored is not None:
            values[name] = dict(sorted(stored.items())) if isinstance(stored, Mapping) else stored
            sources[name] = VERIFY_SOURCE
        else:
            values[name] = measured[name]
            sources[name] = ARTIFACTS_SOURCE if measured[name] is not None else NO_SOURCE
    return values, sources


def consolidated_metrics(
    target: TargetGraph,
    result: BuildResult | None,
    ontology: Ontology,
    *,
    plan: PlanArtifact | None = None,
    spine: SpineGraph | None = None,
    prefer_verification: bool = True,
    steps: StepArtifacts | None = None,
) -> ConsolidatedMetrics:
    """The consolidated metrics of one build (see the module docstring for what each one reads); ``prefer_verification``
    false measures the six Verify metrics on the artifacts even when the result carries the stored Verify values.
    """

    steps = steps or StepArtifacts()

    view = spine if spine is not None else spine_view(target, ontology)
    unreached_before = spine_unreached(view, ontology, READ_BASES)
    unreached_all = spine_unreached(spine, ontology) if spine is not None else None
    source = SPINE_VIEW_SOURCE if spine is not None else ARTIFACTS_SOURCE
    six, sources = _verify_metrics(
        target,
        result,
        ontology,
        plan,
        (unreached_before, unreached_all, source),
        prefer_verification=prefer_verification,
    )
    shape, shape_sources = _graph_shape(target, result, ontology, prefer_verification=prefer_verification)
    by_label_all = by_label(unreached_all) if unreached_all is not None else None
    verified = result.verification.metrics if result is not None and result.verification is not None else None
    if by_label_all is None and verified is not None and prefer_verification:
        by_label_all = verified.spine_unreachable_by_label
    quarantine = quarantine_metrics(target, result)
    counts = conform_counts(target)
    edge_values, edge_sources = edge_metrics(
        target, result, ontology, plan=plan, steps=steps, prefer_verification=prefer_verification
    )
    return ConsolidatedMetrics(
        nodes_by_label=dict(sorted(Counter(node.label for node in target.nodes).items())),
        edges_by_relation_basis=edges_by_relation_basis(target),
        inferred_added_by_relation=inferred_added_by_relation(result),
        judge_calls=result.metrics.judge_calls if result is not None else None,
        judge_failed_calls=result.metrics.judge_failed_calls if result is not None else None,
        edges_by_relation_persisted=dict(sorted(result.metrics.edges_by_relation.items())) if result else None,
        metric_sources=sources | shape_sources | edge_sources,
        spine_unreachable_before_by_label=by_label(unreached_before),
        spine_unreachable_by_label=by_label_all,
        quarantine_by_reason=quarantine["quarantine_by_reason"],
        quarantine_by_pair=quarantine["quarantine_by_pair"],
        owner_conflicts_judged=counts["owner_conflicts_judged"],
        cycles_broken=counts["cycles_broken"],
        unsupported_entities_dropped=counts["unsupported_entities_dropped"],
        in_scope_disagreements=counts["in_scope_disagreements"],
        in_scope_dirty_wins=counts["in_scope_dirty_wins"],
        **grounding_metrics(target, ontology, plan=plan, resolved=steps.resolved, edges=steps.edges),
        **step_counts(target, steps.nodes),
        **naming_metrics(steps.nodes, steps.resolved, ontology, plan),
        **identity_metrics(steps.resolved, target, result),
        **edge_values,
        **inference_metrics(steps.inference, result),
        **shape,
        **six,
    )
