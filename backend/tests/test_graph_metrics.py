"""Consolidated build metrics offline (``graph.build.metrics``)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import Field

from graph.build.answers import HUBS_LIST, STATEMENTS_LIST
from graph.build.metrics import (
    ALIAS_USE,
    CATALOG_METRICS,
    CHECK_METRICS,
    COUNTER_FIELDS,
    EDGE_METRICS,
    GRAPH_METRICS,
    GROUNDING_METRICS,
    IDENTITY_METRICS,
    INFERENCE_METRICS,
    NAMING_METRICS,
    NEARBY_USE,
    STEP_COUNT_METRICS,
    TERM_USE_METRICS,
    ConsolidatedMetrics,
    StepArtifacts,
    active_fragments,
    confidence_band,
    conform_counts,
    consolidated_metrics,
    cross_document_edges,
    dropped_by_relation,
    edges_by_relation_basis,
    fan_out,
    grounding_counts,
    grounding_metrics,
    identity_metrics,
    inference_metrics,
    named_like_statement,
    naming_metrics,
    provider_pairs,
    quarantine_counts,
    quarantine_metrics,
    spine_view,
    statement_checked_labels,
    step_counts,
    term_uses,
)
from graph.build.models import (
    BacklogQueue,
    Basis,
    BuildMetrics,
    BuildResult,
    CanonicalNode,
    ChangeReport,
    ConsolidationCounters,
    DiffArtifact,
    EdgeCounters,
    EdgesArtifact,
    EdgeWindowCall,
    EvidenceLines,
    FragmentRec,
    FragmentStatus,
    HubMerge,
    HubName,
    InferenceReport,
    JudgedItem,
    KnownTermsShown,
    NodeCounters,
    NodesArtifact,
    PlanArtifact,
    QuarantinedEdge,
    QuarantinedTargetEdge,
    RawNode,
    RefusedDedupPair,
    ResolveArtifact,
    ResolveCounters,
    RuleReport,
    Settlement,
    SpecChanges,
    StatementRec,
    TargetCounters,
    TargetEdge,
    TargetGraph,
    TargetNode,
    VerificationMetrics,
    VerificationSummary,
)
from graph.build.models import ChangeSet as PlanChanges
from graph.build.spine import SpineEdge, SpineGraph, SpineNode
from graph.build.validator import exclusive_conflicts
from graph.core.llm.usage import UsageCounts
from graph_support import frozen_ontology, shipped_ontology

# Mechanics tests: pinned to the frozen v1.3 ontology (graph_support.FROZEN_ONTOLOGY_FILE), not the shipped one.
ONTOLOGY = frozen_ontology()
FR, FEAT, MOD, CAP = "FunctionalRequirement", "Feature", "Module", "Capability"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
V2_FIELDS = (
    "quarantine_by_reason",
    "quarantine_by_pair",
    "owner_conflicts_judged",
    "cycles_broken",
    "interfaces_without_provider",
    "features_named_like_statements",
    "unsupported_entities_dropped",
    "entity_relates_fan_out",
    "relates_fan_out",
    "deployed_to_fan_out",
    "ownerless_requirements",
    "in_scope_disagreements",
    "in_scope_dirty_wins",
    "unsnapped_quote_nodes",
    "unsnapped_source_quotes",
    "provisional_keys_left",
    "attributed_fragments_dropped",
    "nodes_missing_source_documents",
    "label_collapses",
    "derive_fallback_matches",
    "derive_fallback_refused",
)


def tnode(key: str, label: str, fragments: Sequence[str] = ("f1",), **extra: Any) -> TargetNode:
    return TargetNode(
        identity_key=key,
        label=label,
        name=key,
        evidence=tuple(EvidenceLines(fragment_id=fid, line_start=1, line_end=2) for fid in fragments),
        **extra,
    )


def tedge(relation: str, source: str, target: str, basis: Basis = "stated") -> TargetEdge:
    return TargetEdge(
        edge_key=f"{source}|{relation}|{target}",
        relation=relation,
        source_key=source,
        target_key=target,
        basis=basis,
        fragment_ids=("f1",),
    )


def result(**values: object) -> BuildResult:
    base: dict[str, object] = {
        "project_id": "p",
        "build_id": "b1",
        "mode": "first",
        "status": "succeeded",
        "started_at": T0,
        "finished_at": T0,
    }
    return BuildResult.model_validate(base | values)


def fragment(fid: str, status: FragmentStatus = "active") -> FragmentRec:
    return FragmentRec(
        fragment_id=fid,
        document_id="d1.md",
        heading_path=(),
        ordinal=0,
        line_start=1,
        line_end=2,
        text="x",
        content_hash="h",
        tokens=1,
        status=status,
    )


def plan(fragments: Sequence[FragmentRec], dirty: Sequence[str]) -> PlanArtifact:
    return PlanArtifact(
        mode="first", change_set=PlanChanges(), documents=(), fragments=tuple(fragments), dirty_ids=tuple(dirty)
    )


def shop_graph() -> TargetGraph:
    nodes = [
        tnode("r1", FR, label_disagreement=FEAT),
        tnode("r2", FR, ("f2",)),
        tnode("r3", FR),
        tnode("f1", FEAT),
        tnode("f2", FEAT),
        tnode("m1", MOD),
        tnode("m2", MOD),
        tnode("c", CAP),
    ]
    edges = [
        tedge("PART_OF", "r1", "f1"),
        tedge("PART_OF", "r1", "f2", "implied"),  # second exclusive target: one conflict
        tedge("PART_OF", "f1", "m1"),
        tedge("PART_OF", "f2", "m1"),
        tedge("PART_OF", "r2", "m2"),
        tedge("RELATES_TO", "r2", "m1"),
        tedge("GROUPED_BY", "f1", "c"),
    ]
    counters = TargetCounters(dropped_edges={"RELATES_TO": {"illegal_pair": 2}, "PART_OF": {"self_loop": 0}})
    return TargetGraph(nodes=tuple(nodes), edges=tuple(edges), counters=counters)


def test_size_metrics_count_target_nodes_and_edges_by_relation_and_basis() -> None:
    changes = SpecChanges(inferred_added=("FR::R3|PART_OF|MOD::M1", "malformed"))
    totals = BuildMetrics(edges_by_relation={"PART_OF": 6, "RELATES_TO": 1, "GROUPED_BY": 1})
    metrics = consolidated_metrics(shop_graph(), result(spec_changes=changes, metrics=totals), ONTOLOGY)
    assert metrics.nodes_by_label == {CAP: 1, FEAT: 2, FR: 3, MOD: 2}
    assert list(metrics.nodes_by_label) == sorted(metrics.nodes_by_label)
    assert metrics.edges_by_relation_basis == {
        "GROUPED_BY": {"stated": 1},
        "PART_OF": {"implied": 1, "stated": 4},
        "RELATES_TO": {"stated": 1},
    }
    assert metrics.inferred_added_by_relation == {"PART_OF": 1}  # added this build, not the total
    assert edges_by_relation_basis(shop_graph()) == metrics.edges_by_relation_basis
    assert metrics.edges_by_relation_persisted == {"GROUPED_BY": 1, "PART_OF": 6, "RELATES_TO": 1}
    assert consolidated_metrics(shop_graph(), None, ONTOLOGY).edges_by_relation_persisted is None


def test_existing_metrics_are_computed_from_the_artifacts_without_verify_values() -> None:
    graph = shop_graph()
    fragments = [fragment("f1"), fragment("f2"), fragment("f3"), fragment("f4", "retired"), fragment("f5", "skipped")]
    metrics = consolidated_metrics(graph, None, ONTOLOGY, plan=plan(fragments, dirty=("f1", "f2", "f3")))
    assert metrics.metric_sources == {
        "spine_unreachable_before": "artifacts",
        "spine_unreachable": "n/a",
        "exclusive_conflicts": "artifacts",
        "label_disagreement": "artifacts",
        "dropped_edges": "artifacts",
        "fragments_without_nodes": "artifacts",
    } | dict.fromkeys(GRAPH_METRICS, "n/a") | {  # the frozen v1.3 ontology declares none of their flags
        "owners_by_source": "n/a",
        "cross_document_edges": "conform artifact (before inference)",  # no result: no inferred edges known
    }
    assert metrics.exclusive_conflicts == 1
    assert metrics.label_disagreement == 1
    assert metrics.dropped_edges == {"RELATES_TO": 2}
    assert metrics.fragments_without_nodes == 1  # f3: active, no node; f4 retired, f5 skipped
    assert metrics.spine_unreachable_before == 1  # r3 has no edge
    assert metrics.spine_unreachable_before_by_label == {FR: 1}
    # Both need the inferred edges: no result, no spine view.
    assert metrics.spine_unreachable is None and metrics.spine_unreachable_by_label is None
    unverified = consolidated_metrics(graph, result(metrics=BuildMetrics(spine_unreachable=3)), ONTOLOGY)
    assert (unverified.spine_unreachable, unverified.fragments_without_nodes) == (3, None)
    sources = unverified.metric_sources
    assert (sources["spine_unreachable"], sources["fragments_without_nodes"]) == ("build metrics", "n/a")


def test_stored_verify_values_win_over_the_artifacts() -> None:
    stored = VerificationMetrics(
        spine_unreachable_before=7,
        spine_unreachable=2,
        exclusive_conflicts=5,
        label_disagreement=4,
        dropped_edges={"RELATES_TO": 9},
        fragments_without_nodes=3,
    )
    fragments = [fragment("f1"), fragment("f3")]
    metrics = consolidated_metrics(
        shop_graph(),
        result(verification=VerificationSummary(ok=True, metrics=stored)),
        ONTOLOGY,
        plan=plan(fragments, dirty=("f1", "f3")),
    )
    assert {metrics.metric_sources[name] for name in CHECK_METRICS} == {"verify"}
    assert (
        metrics.spine_unreachable_before,
        metrics.spine_unreachable,
        metrics.exclusive_conflicts,
        metrics.label_disagreement,
        metrics.dropped_edges,
        metrics.fragments_without_nodes,
    ) == (7, 2, 5, 4, {"RELATES_TO": 9}, 3)

    older = VerificationSummary(ok=True, metrics=VerificationMetrics(spine_unreachable=2))
    measured = consolidated_metrics(shop_graph(), result(verification=older), ONTOLOGY)
    assert (measured.spine_unreachable_before, measured.metric_sources["spine_unreachable_before"]) == (1, "artifacts")
    # Forced onto the artifacts, the stored values are ignored.
    forced = consolidated_metrics(
        shop_graph(),
        result(verification=VerificationSummary(ok=True, metrics=stored)),
        ONTOLOGY,
        plan=plan(fragments, dirty=("f1", "f3")),
        prefer_verification=False,
    )
    assert (forced.exclusive_conflicts, forced.dropped_edges, forced.fragments_without_nodes) == (
        1,
        {"RELATES_TO": 2},
        1,
    )
    assert forced.metric_sources["exclusive_conflicts"] == "artifacts"


def test_a_spine_view_gives_spine_unreachable_and_by_label_over_every_edge() -> None:
    view = SpineGraph(
        nodes=(SpineNode("r1", "FR::R1", FR), SpineNode("r2", "FR::R2", FR), SpineNode("m", "MOD::M", MOD)),
        edges=(SpineEdge("PART_OF", "r1", "m", "stated"), SpineEdge("PART_OF", "r2", "m", "inferred")),
    )
    metrics = consolidated_metrics(shop_graph(), result(), ONTOLOGY, spine=view)
    assert (metrics.spine_unreachable_before, metrics.spine_unreachable) == (1, 0)
    assert metrics.metric_sources["spine_unreachable"] == "spine view"
    assert metrics.spine_unreachable_before_by_label == {FR: 1}
    assert metrics.spine_unreachable_by_label == {}


def test_active_fragments_follow_the_build_result_statuses() -> None:
    fragments = [fragment("a", "skipped"), fragment("b", "skipped"), fragment("c"), fragment("d", "retired")]
    found = active_fragments(plan(fragments, dirty=("a", "c")), result(skipped_fragments={"c": "overflow"}))
    assert found == ["a"]  # a extracted now; b keeps skipped; c skipped this build; d retired


def test_v2_fields_are_none_on_v1_3_shaped_artifacts() -> None:
    metrics = consolidated_metrics(shop_graph(), result(), ONTOLOGY)
    assert all(getattr(metrics, name) is None for name in V2_FIELDS)
    assert set(V2_FIELDS) < set(ConsolidatedMetrics.model_fields)


def quarantined(reason: str, relation: str, source: str, target: str) -> QuarantinedTargetEdge:
    return QuarantinedTargetEdge.model_validate(
        {
            "reason": reason,
            "relation": relation,
            "source_label": source,
            "target_label": target,
            "basis": "stated",
            "source_key": "s",
            "target_key": "t",
        }
    )


QUARANTINE = (
    quarantined("illegal_pair", "PART_OF", CAP, CAP),
    quarantined("illegal_pair", "PART_OF", CAP, CAP),
    quarantined("locality", "RELATES_TO", MOD, FR),
)


def test_quarantine_metrics_count_the_conform_artifact_entries_per_reason_and_pair() -> None:
    by_reason = {"illegal_pair": 2, "locality": 1}
    by_pair = {f"PART_OF|{CAP}|{CAP}": 2, f"RELATES_TO|{MOD}|{FR}": 1}
    assert quarantine_counts(QUARANTINE) == (by_reason, by_pair)
    graph = shop_graph().model_copy(update={"quarantine": QUARANTINE})
    metrics = consolidated_metrics(graph, result(), ONTOLOGY)
    assert (metrics.quarantine_by_reason, metrics.quarantine_by_pair) == (by_reason, by_pair)
    # an artifact that quarantined nothing (the field stored, empty) reads zero, not n/a
    stored = TargetGraph.model_validate_json(shop_graph().model_dump_json())
    assert consolidated_metrics(stored, None, ONTOLOGY).quarantine_by_reason == {}
    # the result's build metrics win when set
    counted = result(metrics=BuildMetrics(quarantine_by_reason={"locality": 3}, quarantine_by_pair={"X|A|B": 3}))
    metrics = consolidated_metrics(graph, counted, ONTOLOGY)
    assert (metrics.quarantine_by_reason, metrics.quarantine_by_pair) == ({"locality": 3}, {"X|A|B": 3})


def test_metrics_round_trip_through_json() -> None:
    metrics = consolidated_metrics(shop_graph(), result(), ONTOLOGY).model_copy(update={"relates_fan_out": {1: 2}})
    assert ConsolidatedMetrics.model_validate_json(metrics.model_dump_json()) == metrics


def test_identity_metrics_are_fields_of_both_metric_sets_and_na_on_older_artifacts() -> None:
    """the hub merge, pair-pass and dedup metrics exist and default to n/a (None)."""

    assert set(IDENTITY_METRICS) <= set(ConsolidatedMetrics.model_fields)
    assert set(IDENTITY_METRICS) <= set(VerificationMetrics.model_fields)
    metrics = consolidated_metrics(shop_graph(), result(), ONTOLOGY)
    assert all(getattr(metrics, name) is None for name in IDENTITY_METRICS)
    assert all(getattr(VerificationMetrics(), name) is None for name in IDENTITY_METRICS)


def test_identity_metrics_are_measured_on_stored_artifacts_and_na_on_older_ones() -> None:
    """counters and merges of artifacts that record them; n/a for artifacts."""

    resolved = ResolveArtifact(
        counters=ResolveCounters(
            consolidation=ConsolidationCounters(merges=3, pair_merges=1, alias_merges=1, unsure_groups=2)
        ),
        merges=(
            HubMerge(anchor_key="a", member_key="b", source="block", confidence="high"),
            HubMerge(anchor_key="a", member_key="c", source="pair", confidence="medium"),
            HubMerge(anchor_key="d", member_key="e", source="block"),
        ),
    )
    target = TargetGraph(
        counters=TargetCounters(dedup_unsure=1, dedup_verdicts_expired=2),
        dedup_refused=(
            RefusedDedupPair(first_key="x", second_key="y", label=FR, guard="numbers", name_cosine=0.9),
            RefusedDedupPair(first_key="x", second_key="z", label=FR, guard="numbers", name_cosine=0.88),
            RefusedDedupPair(first_key="y", second_key="z", label=FR, guard="negation", name_cosine=0.87),
        ),
    )
    stored_resolve = ResolveArtifact.model_validate_json(resolved.model_dump_json())
    stored_target = TargetGraph.model_validate_json(target.model_dump_json())
    values = identity_metrics(stored_resolve, stored_target)
    assert values == {
        "hub_merges_by_source": {"block": 2, "pair": 1, "stored": 0},
        "pair_candidates_vector": 0,
        "pair_candidates_alias": 0,
        "pair_batches": 0,
        "pair_calls": 0,
        "pair_over_cap": 0,
        "pair_merges": 1,
        "alias_merges": 1,
        "unsure_groups": 2,
        "decisions_expired": 0,
        "dedup_candidates_rich_only": 0,
        "dedup_unsure": 1,
        "dedup_verdicts_expired": 2,
        "dedup_refused_by_guard": {"negation": 1, "numbers": 2},
    }
    metrics = consolidated_metrics(stored_target, None, ONTOLOGY, steps=StepArtifacts(resolved=stored_resolve))
    assert {name: getattr(metrics, name) for name in IDENTITY_METRICS} == values
    # the result's build metric wins for the refused pairs
    counted = result(metrics=BuildMetrics(dedup_refused_by_guard={"ids": 4}))
    assert identity_metrics(stored_resolve, stored_target, counted)["dedup_refused_by_guard"] == {"ids": 4}

    # artifacts: no merges, pair-pass or dedup fields stored
    old_resolve = stored_resolve.model_dump(mode="json")
    for name in ("merges", "pair_candidates"):
        del old_resolve[name]
    for name in (*IDENTITY_METRICS, "pair_batches"):
        old_resolve["counters"]["consolidation"].pop(name, None)
    old_target = stored_target.model_dump(mode="json")
    del old_target["dedup_refused"]
    for name in ("dedup_candidates_rich_only", "dedup_unsure", "dedup_verdicts_expired"):
        del old_target["counters"][name]
    old = identity_metrics(ResolveArtifact.model_validate(old_resolve), TargetGraph.model_validate(old_target))
    assert old == dict.fromkeys(IDENTITY_METRICS)
    # merges recorded nowhere although the consolidation merged
    unrecorded = ResolveArtifact.model_validate_json(
        ResolveArtifact(counters=ResolveCounters(consolidation=ConsolidationCounters(merges=3))).model_dump_json()
    )
    assert identity_metrics(unrecorded, None)["hub_merges_by_source"] is None
    assert identity_metrics(None, None) == dict.fromkeys(IDENTITY_METRICS)


def test_empty_target_graph() -> None:
    metrics = consolidated_metrics(TargetGraph(), None, ONTOLOGY)
    assert metrics.nodes_by_label == {} and metrics.edges_by_relation_basis == {}
    assert (metrics.spine_unreachable_before, metrics.exclusive_conflicts, metrics.dropped_edges) == (0, 0, {})


def test_exclusive_conflicts_leave_out_unlabelled_and_missing_ends() -> None:
    labels: dict[str, str | None] = {"r": FR, "f1": FEAT, "f2": FEAT, "x": None}
    edges = [("PART_OF", "r", target) for target in ("f1", "f2", "x", "ghost")]
    assert exclusive_conflicts(edges, labels, ONTOLOGY) == 1
    assert dropped_by_relation(None) == {}


def test_fan_out_histogram() -> None:
    edges = [("R", "a", "x"), ("R", "a", "y"), ("R", "a", "y"), ("R", "b", "x"), ("S", "c", "x")]
    assert fan_out(edges, "R") == {1: 1, 2: 1}
    assert fan_out(edges, "R", sources={"b"}) == {1: 1}
    assert fan_out(edges, "T") == {}


def test_spine_view_is_keyed_by_identity_key() -> None:
    view = spine_view(shop_graph(), ONTOLOGY)
    assert {node.key for node in view.nodes} >= {"r1", "m1"}
    assert len(view.edges) == len(shop_graph().edges)


def test_the_spine_exclude_applies_on_the_artifact_view_also_through_the_owner() -> None:
    shipped = shipped_ontology()  # v2.0: spine exclude {in_scope: [no]}, FR -> Feature PART_OF an owner pair
    target = TargetGraph(
        nodes=(
            tnode("owned", FR),  # its owner feature is out of scope: excluded
            tnode("out", FR, attributes={"in_scope": "no"}),  # out of scope itself: excluded
            tnode("lost", FR),  # in scope, no path: counted
            tnode("feat", FEAT, attributes={"in_scope": "no"}),
        ),
        edges=(tedge("PART_OF", "owned", "feat"),),
    )
    view = spine_view(target, shipped)
    assert {node.key: dict(node.attributes) for node in view.nodes}["out"]["in_scope"] == "no"
    metrics = consolidated_metrics(target, None, shipped)
    assert metrics.spine_unreachable_before == 1
    assert metrics.spine_unreachable_before_by_label == {FR: 1}


# v2.0 metrics on the shipped ontology (labels and relations from flags and roles only)

SHIPPED = shipped_ontology()
IFC, ENT = "Interface", "DataEntity"


def v2node(key: str, label: str, name: str | None = None, **attributes: str) -> TargetNode:
    return TargetNode(
        identity_key=key,
        label=label,
        name=name or key,
        evidence=(EvidenceLines(fragment_id="f1", line_start=1, line_end=2),),
        attributes=attributes,
    )


def v2_graph() -> TargetGraph:
    nodes = (
        v2node("owned", FR),
        v2node("orphan", FR),
        v2node("out", FR, in_scope="no"),  # spine exclude: not counted ownerless
        v2node("fa", FEAT, "Users must sign in"),
        v2node("fb", FEAT, "Checkout."),
        v2node("fc", FEAT, "Scan and pay"),
        v2node("cap", CAP, "Can pay."),  # group, not owned: not checked
        v2node("ent", ENT, "Order."),  # group, not owned: not checked
        v2node("ifc", IFC),
        v2node("lonely", IFC),
        v2node("ma", MOD, type="backend"),
        v2node("mb", MOD, type="backend"),
        v2node("d1", MOD, type="deployment"),
        v2node("d2", MOD, type="deployment"),
    )
    edges = (
        tedge("PART_OF", "owned", "fa"),
        tedge("PART_OF", "fa", "ma"),
        tedge("RELATES_TO", "orphan", "ma"),
        tedge("RELATES_TO", "mb", "orphan"),  # symmetric, stored module first
        tedge("RELATES_TO", "owned", "ma"),
        tedge("RELATES_TO", "ent", "owned"),
        tedge("PROVIDED_BY", "ifc", "ma"),
        tedge("DEPLOYED_TO", "ma", "d1"),
        tedge("DEPLOYED_TO", "ma", "d2"),
        tedge("DEPLOYED_TO", "mb", "d1"),
        tedge("STORED_IN", "ent", "ma", "implied"),
    )
    return TargetGraph(nodes=nodes, edges=edges, quarantine=())


def test_v2_graph_metrics_on_the_target_graph() -> None:
    metrics = consolidated_metrics(v2_graph(), None, SHIPPED)
    assert metrics.ownerless_requirements == 1  # orphan; out is excluded
    assert metrics.interfaces_without_provider == 1  # lonely
    assert metrics.features_named_like_statements == 2  # a modal verb, a final period
    assert metrics.relates_fan_out == {1: 1, 2: 1}  # owned: ma; orphan: ma, mb
    assert metrics.entity_relates_fan_out == {1: 1}
    assert metrics.deployed_to_fan_out == {1: 1, 2: 1}
    assert {metrics.metric_sources[name] for name in GRAPH_METRICS} == {"artifacts"}
    assert (metrics.quarantine_by_reason, metrics.quarantine_by_pair) == ({}, {})


def test_graph_metrics_read_the_flags_not_names() -> None:
    assert provider_pairs(SHIPPED) == (("PROVIDED_BY", IFC, MOD),)  # module -> module containment: root to root
    assert statement_checked_labels(SHIPPED) == {FEAT}
    assert provider_pairs(ONTOLOGY) == () and statement_checked_labels(ONTOLOGY) == frozenset()


def test_stored_verify_values_win_for_the_graph_metrics() -> None:
    stored = VerificationMetrics(ownerless_requirements=4, relates_fan_out={3: 1}, spine_unreachable_by_label={FR: 2})
    verified = result(verification=VerificationSummary(ok=True, metrics=stored))
    metrics = consolidated_metrics(v2_graph(), verified, SHIPPED)
    assert (metrics.ownerless_requirements, metrics.relates_fan_out) == (4, {3: 1})
    assert metrics.metric_sources["ownerless_requirements"] == "verify"
    assert metrics.interfaces_without_provider == 1
    assert metrics.metric_sources["interfaces_without_provider"] == "artifacts"
    assert metrics.spine_unreachable_by_label == {FR: 2}
    forced = consolidated_metrics(v2_graph(), verified, SHIPPED, prefer_verification=False)
    assert forced.ownerless_requirements == 1 and forced.spine_unreachable_by_label is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Users must sign in", True),
        ("The system SHALL log", True),
        ("Cannot refund twice", True),
        ("Checkout.", True),
        ("Checkout", False),
        ("Scan and pay", False),  # "can" only as a word
        ("Willow payments", False),
        (None, False),
    ],
)
def test_named_like_a_statement(name: object, *, expected: bool) -> None:
    assert named_like_statement(name) is expected


class V2Counters(TargetCounters):
    """The conform counters adds (read by name, defensively)."""

    owner_conflicts_judged: int = 0
    cycles_broken: int = 0
    unsupported_nodes: dict[str, int] = Field(default_factory=dict)
    in_scope_disagreements: int = 0


def test_conform_counts_come_from_the_counters_of_a_v2_artifact() -> None:
    counters = V2Counters(owner_conflicts_judged=3, cycles_broken=1, unsupported_nodes={ENT: 2, "X": 1})
    graph = TargetGraph(counters=counters, quarantine=())
    assert conform_counts(graph) == {
        "owner_conflicts_judged": 3,
        "cycles_broken": 1,
        "unsupported_entities_dropped": 3,
        "in_scope_disagreements": 0,
        "in_scope_dirty_wins": 0,
    }
    metrics = consolidated_metrics(graph, None, SHIPPED)
    assert (metrics.owner_conflicts_judged, metrics.unsupported_entities_dropped) == (3, 3)
    # A v1.3 artifact (no quarantine stored, counters unset): n/a.
    assert set(conform_counts(TargetGraph(counters=V2Counters())).values()) == {None}
    assert set(conform_counts(None).values()) == {None}
    # A v2 artifact reads 0 for a counter field Conform has, n/a for one it has not.
    plain = conform_counts(TargetGraph(quarantine=()))
    fields = TargetCounters.model_fields
    assert plain == {
        metric: 0 if any(name in fields for name in names) else None for metric, names in COUNTER_FIELDS.items()
    }


def test_exclusive_conflicts_and_dropped_edges_of_a_v2_artifact_come_from_conform() -> None:
    counters = TargetCounters(
        exclusive_conflicts=4,  # before normalisation; the target edges hold one conflict
        dropped_edges={
            "RELATES_TO": {"illegal_pair": 2, "locality": 1, "redundant": 3},
            "PART_OF": {"owner_conflict": 2, "depth_cut": 1, "self_loop": 1},
            "DEPENDS_ON": {"cycle_broken": 1},
        },
    )
    v2 = shop_graph().model_copy(update={"counters": counters, "quarantine": ()})
    metrics = consolidated_metrics(v2, None, ONTOLOGY)
    assert metrics.exclusive_conflicts == 4
    # Legalisation and locality only, comparable with v1.3; normalisation drops are left out.
    assert metrics.dropped_edges == dropped_by_relation(v2) == {"PART_OF": 1, "RELATES_TO": 3}
    # A v1.3 artifact (no quarantine stored): counted on the target edges.
    assert (
        consolidated_metrics(shop_graph().model_copy(update={"counters": counters}), None, ONTOLOGY).exclusive_conflicts
        == 1
    )


def test_quarantine_metrics_add_inference_invalid_proposals() -> None:
    proposed = QuarantinedEdge.model_validate(
        {
            "reason": "invalid_proposal",
            "relation": "PART_OF",
            "source_label": FR,
            "target_label": MOD,
            "basis": "inferred",
            "source_spec_id": "REQ::FR::A",
            "target_spec_id": "MOD::B",
        }
    )
    inference = InferenceReport(quarantine=(proposed,))
    graph = shop_graph().model_copy(update={"quarantine": QUARANTINE})
    found = quarantine_metrics(graph, None, inference)
    assert found["quarantine_by_reason"] == {"illegal_pair": 2, "invalid_proposal": 1, "locality": 1}
    assert found["quarantine_by_pair"] == {
        f"PART_OF|{CAP}|{CAP}": 2,
        f"PART_OF|{FR}|{MOD}": 1,
        f"RELATES_TO|{MOD}|{FR}": 1,
    }
    # Only inference recorded quarantine (v1.3 conform artifact): still counted.
    assert quarantine_metrics(shop_graph(), None, inference)["quarantine_by_reason"] == {"invalid_proposal": 1}
    assert quarantine_metrics(None, None, None) == {"quarantine_by_reason": None, "quarantine_by_pair": None}


# Grounding stats.


def grounding_artifacts() -> tuple[PlanArtifact, ResolveArtifact, EdgesArtifact]:
    """Two documents stating one requirement; written before per-source quotes (``quotes`` unset) and its quote
    attribute snaps in the first document only.
    """

    texts = {"a.md": "Users can export orders.", "b.md": "Orders are exported for users."}
    fragments = tuple(
        fragment(f"{document}#s0").model_copy(update={"document_id": document, "text": text, "line_end": 1})
        for document, text in texts.items()
    )
    plan_ = PlanArtifact(
        mode="first",
        change_set=PlanChanges(new=tuple(texts)),
        documents=(),
        fragments=fragments,
        dirty_ids=tuple(item.fragment_id for item in fragments),
    )
    canonical = {
        "cid": "c1",
        "identity_key": "requirement|span|users can export orders",
        "provisional": True,
        "name": "Users can export orders",
        "label": FR,
        "member_ids": ["raw-1"],
        "fragment_ids": ["a.md#s0", "b.md#s0"],
    }
    resolved = ResolveArtifact.model_validate({"canonical": [canonical]})
    edges = EdgesArtifact(attributes={"c1": {"quote": "Users can export orders"}})
    return plan_, resolved, edges


def test_grounding_metrics_come_from_the_conform_counters_else_from_the_stored_artifacts() -> None:
    plan_, resolved, edges = grounding_artifacts()
    measured = {
        "unsnapped_quote_nodes": {},
        "unsnapped_source_quotes": None,  # n/a before Resolve kept one quote per source
        "provisional_keys_left": 0,
        "attributed_fragments_dropped": 1,  # b.md holds no evidence
        "nodes_missing_source_documents": 1,
    }
    older = TargetGraph(quarantine=())
    assert set(grounding_counts(older).values()) == {None}
    assert grounding_metrics(older, ONTOLOGY, plan=plan_, resolved=resolved, edges=edges) == measured
    metrics = consolidated_metrics(older, None, ONTOLOGY, plan=plan_, steps=StepArtifacts(resolved, edges))
    assert {name: getattr(metrics, name) for name in GROUNDING_METRICS} == measured
    assert all(getattr(consolidated_metrics(older, None, ONTOLOGY), name) is None for name in GROUNDING_METRICS)
    # a conform artifact that recorded them wins over the artifacts
    counters = TargetCounters(
        unsnapped_quote_nodes={FR: 2},
        unsnapped_source_quotes=3,
        provisional_keys_left=1,
        attributed_fragments_dropped=0,
        nodes_missing_source_documents=0,
    )
    recorded = TargetGraph(counters=counters, quarantine=())
    assert grounding_metrics(recorded, ONTOLOGY, plan=plan_, resolved=resolved, edges=edges) == {
        "unsnapped_quote_nodes": {FR: 2},
        "unsnapped_source_quotes": 3,
        "provisional_keys_left": 1,
        "attributed_fragments_dropped": 0,
        "nodes_missing_source_documents": 0,
    }


def test_label_collapses_and_derive_fallback_matches_come_from_the_step_artifacts() -> None:
    nodes = NodesArtifact(counters=NodeCounters(label_collapses=2))
    target = TargetGraph(counters=TargetCounters(derive_fallback_matches=3, derive_fallback_refused=1), quarantine=())
    assert step_counts(target, nodes) == {
        "label_collapses": 2,
        "derive_fallback_matches": 3,
        "derive_fallback_refused": 1,
    }
    metrics = consolidated_metrics(target, None, ONTOLOGY, steps=StepArtifacts(nodes=nodes))
    assert (metrics.label_collapses, metrics.derive_fallback_matches, metrics.derive_fallback_refused) == (2, 3, 1)
    # artifacts written without the counts (or none): n/a
    assert step_counts(TargetGraph(quarantine=()), NodesArtifact()) == dict.fromkeys(STEP_COUNT_METRICS)
    assert step_counts(None, None) == dict.fromkeys(STEP_COUNT_METRICS)


def raw(raw_id: str, name: str, label: str, fragments: Sequence[str] = ("f1",), **extra: Any) -> RawNode:
    return RawNode(
        raw_id=raw_id,
        name=name,
        label=label,
        unit_id=extra.pop("unit_id", "u1"),
        call_id="c1",
        window=(0, 1),
        fragment_ids=tuple(fragments),
        **extra,
    )


def canonical(cid: str, label: str, members: Sequence[str]) -> CanonicalNode:
    return CanonicalNode(cid=cid, identity_key=cid, name=cid, label=label, member_ids=tuple(members))


def text_fragment(fid: str, text: str) -> FragmentRec:
    return fragment(fid).model_copy(update={"text": text})


def test_hub_name_spread_counts_surfaces_and_raw_names_per_hub_and_label() -> None:
    nodes = NodesArtifact(
        raw_nodes=(
            raw("a", "Orders service", MOD),
            raw("b", "orders  Service", MOD),  # same surface, same folded raw name
            raw("c", "The order services", MOD),  # same surface (article, plural), other raw name
            raw("d", "Billing", MOD),
            raw("e", "Ledger", MOD),  # merged into Billing by consolidation: a second surface
            raw("f", "Search", FEAT),
            raw("g", "Users must log in", FR),  # not a hub
        )
    )
    resolved = ResolveArtifact(
        canonical=(
            canonical("m1", MOD, ("a", "b", "c")),
            canonical("m2", MOD, ("d", "e")),
            canonical("f1", FEAT, ("f",)),
            canonical("r1", FR, ("g",)),
        ),
        counters=ResolveCounters(consolidation=ConsolidationCounters(merges=1)),
    )
    measured = naming_metrics(nodes, resolved, ONTOLOGY)
    assert measured["hub_surfaces_per_hub"] == {
        "all": {"hubs": 3, "mean": 1.333, "max": 2, "share_over_1": 0.333},
        FEAT: {"hubs": 1, "mean": 1.0, "max": 1, "share_over_1": 0.0},
        MOD: {"hubs": 2, "mean": 1.5, "max": 2, "share_over_1": 0.5},
    }
    assert measured["hub_raw_names_per_hub"]["all"] == {"hubs": 3, "mean": 1.667, "max": 2, "share_over_1": 0.667}
    assert measured["hub_consolidation_merges"] == 1

    assert all(measured[name] is None for name in CATALOG_METRICS)
    assert all(
        measured[name] is None
        for name in ("known_terms_mentioned", "known_terms_nearby", "nearby_term_uses", "alias_name_uses")
    )
    # without the artifacts every naming metric is n/a
    assert naming_metrics(None, None, ONTOLOGY) == dict.fromkeys(NAMING_METRICS)


def test_catalog_counts_and_known_terms_shown_come_from_the_extract_nodes_artifact() -> None:
    nodes = NodesArtifact.model_validate_json(
        NodesArtifact(
            counters=NodeCounters(catalog_batches=3, catalog_calls=4, catalog_hubs=7, catalog_aliases=2),
            known_terms={
                "u1": KnownTermsShown(mentioned=("A", "B"), nearby=("C",)),
                "u2": KnownTermsShown(mentioned=(), nearby=("C", "D", "E")),
            },
        ).model_dump_json()
    )
    measured = naming_metrics(nodes, None, ONTOLOGY)
    # counters the build did not measure stay n/a after the JSON round trip
    assert {name: measured[name] for name in CATALOG_METRICS} == {
        "catalog_batches": 3,
        "catalog_calls": 4,
        "catalog_failed": None,
        "catalog_reused": None,
        "catalog_truncation_splits": None,
        "catalog_hubs": 7,
        "catalog_aliases": 2,
        "catalog_links_refused": None,
        "catalog_aliases_dropped": None,
        "catalog_ambiguous": None,
    }

    plain = NodesArtifact.model_validate_json(NodesArtifact().model_dump_json())
    assert all(value is None for value in naming_metrics(plain, None, ONTOLOGY).values())

    empty = naming_metrics(NodesArtifact(known_terms={}), None, ONTOLOGY)
    assert empty["known_terms_nearby"] == {"units": 0, "mean": 0.0, "max": 0}
    assert empty["nearby_term_uses"] == 0
    assert measured["known_terms_mentioned"] == {"units": 2, "mean": 1.0, "max": 2}
    assert measured["known_terms_nearby"] == {"units": 2, "mean": 2.0, "max": 3}
    assert measured["hub_surfaces_per_hub"] is None  # no resolve artifact
    assert (measured["nearby_term_uses"], measured["nearby_term_uses_unsupported"], measured["alias_name_uses"]) == (
        0,
        None,  # no plan
        None,  # the hub pass was shown no known terms
    )
    assert (measured["nearby_term_uses_hubs"], measured["nearby_term_uses_statements"]) == (None, 0)


def test_hub_pass_nearby_term_uses_their_support_and_alias_uses() -> None:
    """Hub nodes are measured against the terms the hub pass showed their unit (``hub_known_terms``)."""

    nodes = NodesArtifact(
        raw_nodes=(
            raw("a", "Orders service", MOD, ("f1",)),  # nearby, named in f1: supported
            raw("b", "orders services", MOD, ("f2",)),  # nearby, f2 names only its alias OMS: supported
            raw("c", "Orders service", MOD, ("f3",), unit_id="u2"),  # nearby, not named: unsupported
            raw("d", "OMS", MOD, ("f2",)),  # an alias of a shown term: alias use
            raw("e", "Billing", MOD, ("f1",)),  # a mentioned term: neither
            raw("g", "Orders service", MOD, ("f3",), unit_id="u2", reused=True),  # reused: left out
            raw("h", "Orders service", MOD, ("f3",), unit_id="u9"),  # unit without known terms
        ),
        hub_catalog=(HubName(name="Orders service", label=MOD, aliases=("OMS",)),),
        hub_known_terms={
            "u1": KnownTermsShown(mentioned=("Billing",), nearby=("Orders service",)),
            "u2": KnownTermsShown(nearby=("Orders service",)),
        },
        # the statement pass's terms never count for a hub node
        known_terms={"u9": KnownTermsShown(nearby=("Orders service",))},
    )
    plan_ = plan(
        [
            text_fragment("f1", "The orders service books Billing runs."),
            text_fragment("f2", "OMS keeps the order."),
            text_fragment("f3", "Nothing named here."),
        ],
        [],
    )
    uses = term_uses(nodes, ONTOLOGY, plan_)
    assert [(use.raw_id, use.kind, use.node_pass, use.term, use.supported) for use in uses] == [
        ("a", NEARBY_USE, HUBS_LIST, "Orders service", True),
        ("b", NEARBY_USE, HUBS_LIST, "Orders service", True),
        ("c", NEARBY_USE, HUBS_LIST, "Orders service", False),
        ("d", ALIAS_USE, HUBS_LIST, "Orders service", None),
    ]
    measured = naming_metrics(nodes, None, ONTOLOGY, plan_)
    assert {name: measured[name] for name in TERM_USE_METRICS} == {
        "nearby_term_uses": 3,
        "nearby_term_uses_unsupported": 1,
        "nearby_term_uses_hubs": 3,
        "nearby_term_uses_hubs_unsupported": 1,
        "nearby_term_uses_statements": 0,
        "nearby_term_uses_statements_unsupported": 0,
        "alias_name_uses": 1,
    }
    without_plan = naming_metrics(nodes, None, ONTOLOGY)
    assert without_plan["nearby_term_uses_unsupported"] is None
    assert without_plan["nearby_term_uses_hubs_unsupported"] is None
    assert without_plan["nearby_term_uses"] == 3

    first = naming_metrics(nodes.model_copy(update={"hub_known_terms": None}), None, ONTOLOGY, plan_)
    assert (first["nearby_term_uses_hubs"], first["alias_name_uses"], first["nearby_term_uses"]) == (None, None, 0)


def test_statement_pass_nearby_term_uses_count_statements_whose_name_contains_a_nearby_term() -> None:
    """Statements are measured against the statement pass's terms (``known_terms``), by containment of a nearby term's
    name or other name; one statement with several nearby terms counts once.
    """

    nodes = NodesArtifact(
        raw_nodes=(
            raw("s1", "Orders service must keep orders for 7 years", FR, ("f1",)),  # named in f1: supported
            raw("s2", "OMS must log every change", FR, ("f2",), unit_id="u2"),  # alias; f2 names neither
            raw("s3", "Billing must send invoices", FR, ("f1",)),  # a mentioned term only
            raw("s4", "Orders service API must be versioned", FR, ("f1",)),  # the longer mentioned term wins
            raw("s5", "Ledger and Orders service share one clock", FR, ("f3",), unit_id="u2"),  # two terms
            raw("s6", "Orders service must answer fast", FR, ("f3",), unit_id="u2", reused=True),  # reused
            raw("s7", "Orders service must answer fast", FR, ("f3",), unit_id="u9"),  # unit without terms
            raw("m1", "Orders service", MOD, ("f3",), unit_id="u2"),  # a hub: the hub pass's terms only
        ),
        hub_catalog=(HubName(name="Orders service", label=MOD, aliases=("OMS",)),),
        known_terms={
            "u1": KnownTermsShown(mentioned=("Billing", "Orders service API"), nearby=("Orders service",)),
            "u2": KnownTermsShown(nearby=("Orders service", "Ledger")),
        },
    )
    plan_ = plan(
        [
            text_fragment("f1", "The orders service keeps orders; Billing sends invoices."),
            text_fragment("f2", "Every change is logged."),
            text_fragment("f3", "The Ledger shares a clock."),
        ],
        [],
    )
    uses = term_uses(nodes, ONTOLOGY, plan_)
    assert [(use.raw_id, use.node_pass, use.term, use.supported) for use in uses] == [
        ("s1", STATEMENTS_LIST, "Orders service", True),
        ("s2", STATEMENTS_LIST, "Orders service", False),
        ("s5", STATEMENTS_LIST, "Orders service", False),
        ("s5", STATEMENTS_LIST, "Ledger", True),
    ]
    measured = naming_metrics(nodes, None, ONTOLOGY, plan_)
    assert {name: measured[name] for name in TERM_USE_METRICS} == {
        "nearby_term_uses": 3,
        "nearby_term_uses_unsupported": 2,
        "nearby_term_uses_hubs": None,  # the hub pass was shown none
        "nearby_term_uses_hubs_unsupported": None,
        "nearby_term_uses_statements": 3,
        "nearby_term_uses_statements_unsupported": 2,
        "alias_name_uses": None,
    }


def test_consolidated_metrics_carry_the_naming_metrics() -> None:
    nodes = NodesArtifact(raw_nodes=(raw("a", "Orders service", MOD),))
    resolved = ResolveArtifact(canonical=(canonical("m1", MOD, ("a",)),))
    metrics = consolidated_metrics(
        TargetGraph(quarantine=()), None, ONTOLOGY, steps=StepArtifacts(resolved=resolved, nodes=nodes)
    )
    assert metrics.hub_surfaces_per_hub == {
        "all": {"hubs": 1, "mean": 1.0, "max": 1, "share_over_1": 0.0},
        MOD: {"hubs": 1, "mean": 1.0, "max": 1, "share_over_1": 0.0},
    }
    assert metrics.hub_consolidation_merges is None  # counters written without consolidation
    assert metrics.nearby_term_uses is None
    assert set(NAMING_METRICS) <= set(VerificationMetrics.model_fields)
    assert ConsolidatedMetrics.model_validate_json(metrics.model_dump_json()) == metrics


def test_persisted_and_resolve_aliases_count_as_support_and_persisted_aliases_as_alias_uses() -> None:
    nodes = NodesArtifact(
        raw_nodes=(
            raw("a", "Orders service", MOD, ("f1",)),  # f1 names only the persisted alias "oms"
            raw("b", "Orders service", MOD, ("f2",)),  # f2 names only a name Resolve folded in
            raw("c", "OMS", MOD, ("f1",)),  # the persisted alias value: an alias use
        ),
        persisted_hubs=(HubName(name="Orders service", label=MOD, aliases=("oms",)),),
        hub_known_terms={"u1": KnownTermsShown(nearby=("Orders service",))},
    )
    resolved = ResolveArtifact(
        canonical=(
            CanonicalNode(
                cid="m1",
                identity_key="m|surface|orders service",
                identity_aliases=("m|surface|order desk",),
                name="Orders service",
                label=MOD,
                member_ids=("a", "b"),
            ),
        )
    )
    plan_ = plan([text_fragment("f1", "OMS books it."), text_fragment("f2", "The order desk books it.")], [])
    uses = term_uses(nodes, ONTOLOGY, plan_, resolved)
    assert [(use.raw_id, use.kind, use.supported) for use in uses] == [
        ("a", NEARBY_USE, True),
        ("b", NEARBY_USE, True),
        ("c", ALIAS_USE, None),
    ]
    # without the resolve artifact the folded-in name is not known
    assert [use.supported for use in term_uses(nodes, ONTOLOGY, plan_)][:2] == [True, False]


def test_judge_calls_and_failures_come_from_the_result_and_read_n_a_without_it() -> None:
    counted = BuildMetrics(judge_calls={"hub_consolidation": 29}, judge_failed_calls={"hub_consolidation": 28})
    metrics = consolidated_metrics(shop_graph(), result(metrics=counted), ONTOLOGY)
    assert (metrics.judge_calls, metrics.judge_failed_calls) == ({"hub_consolidation": 29}, {"hub_consolidation": 28})
    older = consolidated_metrics(shop_graph(), result(metrics=BuildMetrics()), ONTOLOGY)
    assert (older.judge_calls, older.judge_failed_calls) == (None, None)
    assert consolidated_metrics(shop_graph(), None, ONTOLOGY).judge_failed_calls is None


# Edges and cross-document links


def doc_fragment(fid: str, document: str) -> FragmentRec:
    return fragment(fid).model_copy(update={"document_id": document})


def xdoc_build() -> tuple[TargetGraph, PlanArtifact, dict[str, str]]:
    """Requirements in documents a.md (fragment fa) and b.md (fb): r1 stated owner, r2 an implied owner
    in the other document, r3 ownerless with a cross-document link, r4 ownerless until inference, out
    excluded by the spine; the spec_id of each key."""

    nodes = (
        tnode("r1", FR, ("fa",)),
        tnode("r2", FR, ("fb",)),
        tnode("r3", FR, ("fb",)),
        tnode("r4", FR, ("fa",)),
        tnode("out", FR, ("fa",), attributes={"in_scope": "no"}),
        tnode("fa_hub", FEAT, ("fa",)),
        tnode("fb_hub", FEAT, ("fb",)),
        tnode("ma", MOD, ("fa",)),
    )
    edges = (
        tedge("PART_OF", "r1", "fa_hub"),
        tedge("PART_OF", "r2", "fa_hub", "implied"),
        tedge("RELATES_TO", "r3", "ma"),
    )
    graph = TargetGraph(nodes=nodes, edges=edges, quarantine=())
    fragments = (doc_fragment("fa", "a.md"), doc_fragment("fb", "b.md"))
    return graph, plan(fragments, dirty=()), {node.identity_key: node.identity_key.upper() for node in nodes}


def test_edge_metrics_are_fields_of_the_consolidated_metrics_and_na_on_older_artifacts() -> None:
    assert set(EDGE_METRICS) <= set(ConsolidatedMetrics.model_fields)
    metrics = consolidated_metrics(shop_graph(), result(), ONTOLOGY)
    # the frozen v1.3 ontology has no owned spine source; no artifact records windows, paths or answers

    assert all(getattr(metrics, name) is None for name in EDGE_METRICS)
    assert metrics.metric_sources["cross_document_edges"] == "n/a"


def test_requirement_edges_owners_and_cross_document_edges_on_the_artifacts() -> None:
    graph, stored_plan, spec_ids = xdoc_build()
    inferred = result(spec_changes=SpecChanges(inferred_added=("R4|PART_OF|FB_HUB", "R9|PART_OF|FA_HUB")))
    vectors = {"r3": [0.0, 1.0], "fa_hub": [0.6, 0.8], "fb_hub": [0.1, 0.9], "ma": [0.0, 1.0], "r4": [1.0, 0.0]}
    metrics = consolidated_metrics(
        graph, inferred, SHIPPED, plan=stored_plan, steps=StepArtifacts(spec_ids=spec_ids, vectors=vectors)
    )
    # 4 requirements (out is excluded by the spine, as for owners_by_source); r1, r2 and r3 touch one
    # target edge each (inferred edges are not target edges)
    assert (metrics.edges_per_requirement, metrics.requirements_without_edge) == (0.75, 0.25)
    # out is excluded; r4's owner is the inferred edge this build added (R9 names no key: left out)
    assert metrics.owners_by_source == {"stated": 1, "implied": 1, "inferred": 1, "none": 1}
    assert metrics.cross_document_edges == {"PART_OF": {"implied": 1, "inferred": 1}, "RELATES_TO": {"stated": 1}}
    assert metrics.cross_document_share == 0.75  # 3 of the 4 edges between documented ends
    # r3's closest candidate owner is the module ma (cosine 1.0), grounded in the other document
    assert metrics.orphans_best_elsewhere == {"orphans": 1, "measured": 1, "elsewhere": 1}
    assert {metrics.metric_sources[name] for name in ("owners_by_source", "cross_document_edges")} == {"artifacts"}

    before = consolidated_metrics(graph, inferred, SHIPPED, plan=stored_plan)
    assert before.owners_by_source == {"stated": 1, "implied": 1, "inferred": 0, "none": 2}
    assert before.metric_sources["owners_by_source"] == "conform artifact (before inference)"
    assert before.orphans_best_elsewhere is None  # no vectors


@pytest.mark.parametrize(
    ("mode", "diff", "source"),
    [
        pytest.param("update", DiffArtifact(edges_by_relation={"PART_OF": 2, "RELATES_TO": 1}), "artifacts", id="none"),
        # the diff kept an inferred PART_OF of an earlier build: the added ones alone would miss it
        pytest.param(
            "update", DiffArtifact(edges_by_relation={"PART_OF": 3, "RELATES_TO": 1}), "conform", id="carried-over"
        ),
        pytest.param("rebuild", None, "conform", id="no-diff"),
        pytest.param("update", DiffArtifact(), "conform", id="diff-without-counts"),
    ],
)
def test_the_artifact_path_needs_a_build_that_carried_no_edge_over(
    mode: str, diff: DiffArtifact | None, source: str
) -> None:
    graph, stored_plan, spec_ids = xdoc_build()
    later = result(mode=mode, spec_changes=SpecChanges(inferred_added=("R4|PART_OF|FB_HUB",)))
    steps = StepArtifacts(spec_ids=spec_ids, diff=diff)
    metrics = consolidated_metrics(graph, later, SHIPPED, plan=stored_plan, steps=steps)
    expected = {"artifacts": "artifacts", "conform": "conform artifact (before inference)"}[source]
    assert metrics.metric_sources["owners_by_source"] == metrics.metric_sources["cross_document_edges"] == expected
    assert (metrics.owners_by_source or {})["inferred"] == (1 if source == "artifacts" else 0)


def test_without_a_result_the_edges_are_measured_before_inference() -> None:
    graph, stored_plan, spec_ids = xdoc_build()
    metrics = consolidated_metrics(graph, None, SHIPPED, plan=stored_plan, steps=StepArtifacts(spec_ids=spec_ids))
    assert metrics.metric_sources["owners_by_source"] == "conform artifact (before inference)"
    assert metrics.metric_sources["cross_document_edges"] == "conform artifact (before inference)"


def test_orphans_best_elsewhere_ranks_legal_documented_owners_only() -> None:
    """Candidates failing the pair's ``where`` (external module), excluded by the spine (``in_scope: no``) or without a
    document are skipped, so the same-document feature is best; an orphan without a document is not measured.
    """

    nodes = (
        tnode("r", FR, ("fa",)),
        tnode("r_nodoc", FR, ()),
        tnode("m_ext", MOD, ("fb",), attributes={"external": "yes"}),
        tnode("m_out", MOD, ("fb",), attributes={"in_scope": "no"}),
        tnode("m_nodoc", MOD, ()),
        tnode("feat", FEAT, ("fa",)),
    )
    graph = TargetGraph(nodes=nodes, edges=(), quarantine=())
    fragments = (doc_fragment("fa", "a.md"), doc_fragment("fb", "b.md"))
    vectors = {key: [1.0, 0.0] for key in ("r", "r_nodoc", "m_ext", "m_out", "m_nodoc")} | {"feat": [0.6, 0.8]}
    metrics = consolidated_metrics(
        graph, None, SHIPPED, plan=plan(fragments, dirty=()), steps=StepArtifacts(vectors=vectors)
    )
    assert metrics.orphans_best_elsewhere == {"orphans": 2, "measured": 1, "elsewhere": 0}
    # with the nearest module legal, it wins and is grounded elsewhere
    legal = graph.model_copy(update={"nodes": (*nodes[:2], tnode("m_ext", MOD, ("fb",)), *nodes[3:])})
    metrics = consolidated_metrics(
        legal, None, SHIPPED, plan=plan(fragments, dirty=()), steps=StepArtifacts(vectors=vectors)
    )
    assert metrics.orphans_best_elsewhere == {"orphans": 2, "measured": 1, "elsewhere": 1}


def test_stored_verify_values_win_for_owners_and_cross_document_edges() -> None:
    graph, stored_plan, _ = xdoc_build()
    stored = VerificationMetrics(owners_by_source={"stated": 4, "none": 0}, cross_document_edges={"X": {"stated": 2}})
    verified = result(verification=VerificationSummary(ok=True, metrics=stored))
    metrics = consolidated_metrics(graph, verified, SHIPPED, plan=stored_plan)
    assert metrics.owners_by_source == {"none": 0, "stated": 4}
    assert metrics.cross_document_edges == {"X": {"stated": 2}}
    assert {metrics.metric_sources[name] for name in ("owners_by_source", "cross_document_edges")} == {"verify"}
    forced = consolidated_metrics(graph, verified, SHIPPED, plan=stored_plan, prefer_verification=False)
    assert forced.owners_by_source == {"stated": 1, "implied": 1, "inferred": 0, "none": 2}


def test_cross_document_edges_leave_out_ends_without_documents() -> None:
    documents = {"a": frozenset({"x.md"}), "b": frozenset({"y.md", "x.md"}), "c": frozenset({"y.md"}), "d": frozenset()}
    edges = [
        ("R", "a", "b", "stated"),
        ("R", "a", "c", "implied"),
        ("R", "a", "d", "stated"),
        ("R", "a", "z", "stated"),
    ]
    assert cross_document_edges(edges, documents) == ({"R": {"implied": 1}}, 0.5)
    assert cross_document_edges([], documents) == (None, None)

    assert cross_document_edges([("R", "a", "d", "stated")], documents) == (None, None)


def test_statements_give_documents_without_a_plan() -> None:
    stated = StatementRec(fragment_id="fb", document_id="b.md", revision_id="r", version=1, text="t", at="2026")
    graph = TargetGraph(
        nodes=(
            tnode("r1", FR, statements=(stated,)),
            tnode("m", MOD, statements=(stated.model_copy(update={"document_id": "a.md"}),)),
        ),
        edges=(tedge("RELATES_TO", "r1", "m"),),
        quarantine=(),
    )
    assert consolidated_metrics(graph, None, SHIPPED).cross_document_edges == {"RELATES_TO": {"stated": 1}}


def test_window_path_answer_rule_item_and_modified_cause_counts_come_from_the_artifacts() -> None:
    counters = TargetCounters(
        derive_suppressed_by_edge=2,
        foreign_edges={"near": {"kept": 3, "dropped": 1}, "heading": {"kept": 1}},
        oq_answer_judged=5,
        oq_partly_kept=1,
        oq_answered_dropped=2,
    )
    graph = TargetGraph(counters=counters, quarantine=())
    edges = EdgesArtifact(
        counters=EdgeCounters(windows_called=3, windows_reused=4, windows_fallback=0, warm_ups=1),
        called_windows=(
            EdgeWindowCall(unit_id="u1", window=(0, 2), cause="dirty"),
            EdgeWindowCall(unit_id="u1", window=(2, 3), cause="catalog_changed"),
            EdgeWindowCall(unit_id="u2", window=(0, 1)),
        ),
    )
    report = InferenceReport(rules=(RuleReport(rule="X"), RuleReport(rule="Y", items_by_outcome={"b": 1, "a": 2})))
    steps = StepArtifacts(edges=edges, inference=report, diff=DiffArtifact(modified_causes={"links": 2}))
    metrics = consolidated_metrics(graph, None, SHIPPED, steps=steps)
    assert metrics.foreign_edges == {"heading": {"kept": 1}, "near": {"dropped": 1, "kept": 3}}
    assert (metrics.derive_suppressed_by_edge, metrics.oq_answer_judged, metrics.oq_partly_kept) == (2, 5, 1)
    assert metrics.oq_answered_dropped == 2
    assert (metrics.windows_called, metrics.windows_reused, metrics.windows_fallback, metrics.warm_ups) == (3, 4, 0, 1)
    assert metrics.windows_called_by_cause == {"catalog_changed": 1, "dirty": 1, "unrecorded": 1}
    assert metrics.items_by_outcome == {"Y": {"a": 2, "b": 1}}
    assert metrics.modified_causes == {"links": 2}
    assert ConsolidatedMetrics.model_validate_json(metrics.model_dump_json()) == metrics


def test_inference_metrics_are_fields_of_the_consolidated_metrics_and_na_on_older_artifacts() -> None:
    assert set(INFERENCE_METRICS) <= set(ConsolidatedMetrics.model_fields)
    # a -shaped report: counts and cross_document items only, no bound field, no queue
    older = InferenceReport(
        rules=(RuleReport(rule="INF-CONT-01", subjects=3, judged=2, deferred=1),),
        judged_items=(JudgedItem(rule="X", kind="link", relation="R", outcome="accepted", confidence=0.9),),
    )
    metrics = consolidated_metrics(TargetGraph(quarantine=()), None, SHIPPED, steps=StepArtifacts(inference=older))
    found = {name: getattr(metrics, name) for name in INFERENCE_METRICS}
    assert found.pop("inference_items_by_band") == {"X": {"[80,100]": {"accepted": 1}}}
    assert all(value is None for value in found.values())


def test_inference_metrics_read_the_queue_bounds_bands_shares_settlement_and_change_report() -> None:
    def item(rule: str, outcome: str, confidence: float | None) -> JudgedItem:
        return JudgedItem(rule=rule, kind="owner", relation="PART_OF", outcome=outcome, confidence=confidence)

    report = InferenceReport(
        rules=(
            RuleReport(rule="INF-CONT-01", limit="budget", budget_tokens=300, backlog=2),
            RuleReport(rule="INF-CAP-01", budget_tokens=100, backlog=0, read_edges=4),
            RuleReport(rule="INF-INT-01"),
        ),
        judged_items=(
            item("INF-CONT-01", "accepted", 0.85),
            item("INF-CONT-01", "accepted", 0.8),
            item("INF-CONT-01", "below_floor", 0.65),
            item("INF-CONT-01", "declined", None),
            item("INF-CONT-01", "below_floor", 0.3),
            item("INF-CAP-01", "accepted", 0.7),
        ),
        backlog={"INF-CONT-01": BacklogQueue(spec_ids=("FR::1", "FR::2"), since_build="b0", age_builds=2)},
        spine_unreachable_after=7,
    )
    llm = {"extract_nodes": UsageCounts(input_tokens=600), "infer": UsageCounts(input_tokens=400)}
    built = result(
        metrics=BuildMetrics(llm=llm),
        settlement=Settlement(
            state="not_settled",
            reasons=("inference_backlog", "documents_incomplete"),
            documents=("a.md",),
            inference_backlog={"INF-CONT-01": 2},
            follow_up_builds=("b2", "b3"),
        ),
        change_report=ChangeReport(counts={"changed_links": 1, "added": 3}, edge_counts={"removed": 1}),
    )
    metrics = inference_metrics(report, built)
    assert metrics["inference_backlog"] == {"INF-CONT-01": {"subjects": 2, "age_builds": 2}}
    assert metrics["inference_limit_hits"] == {"INF-CONT-01": {"budget": 1}}
    assert metrics["inference_budget_tokens"] == {"INF-CAP-01": 100, "INF-CONT-01": 300}
    assert metrics["inference_read_edges"] == {"INF-CAP-01": 4}
    assert metrics["inference_items_by_band"] == {
        "INF-CAP-01": {"[70,80)": {"accepted": 1}},
        "INF-CONT-01": {
            "[0,60)": {"below_floor": 1},
            "[60,70)": {"below_floor": 1},
            "[80,100]": {"accepted": 2},
            "none": {"declined": 1},
        },
    }
    assert metrics["spine_unreachable_planned"] == 7
    assert metrics["inference_input_share"] == {"INF-CAP-01": 0.1, "INF-CONT-01": 0.3, "all": 0.4}
    assert metrics["settlement"] == {
        "settled": 0,
        "documents": 1,
        "follow_up_builds": 2,
        "inference_backlog": 2,
        "reasons": {"documents_incomplete": 1, "inference_backlog": 1},
    }
    assert metrics["change_report_nodes"] == {"added": 3, "changed_links": 1}
    assert metrics["change_report_edges"] == {"removed": 1}
    # an older result still gives the infer step's share
    assert inference_metrics(None, result(metrics=BuildMetrics(llm=llm)))["inference_input_share"] == {"all": 0.4}
    assert [confidence_band(value) for value in (None, 0.0, 0.59, 0.6, 0.79, 0.8, 1.0)] == [
        "none",
        "[0,60)",
        "[0,60)",
        "[60,70)",
        "[70,80)",
        "[80,100]",
        "[80,100]",
    ]
    stored = consolidated_metrics(TargetGraph(quarantine=()), built, SHIPPED, steps=StepArtifacts(inference=report))
    assert ConsolidatedMetrics.model_validate_json(stored.model_dump_json()) == stored
