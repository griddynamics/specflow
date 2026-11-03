"""The graph report tool offline (``examples/graph_report.py``)."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import graph_report as report_tool
from graph.build import open_file_stores
from graph.build.metrics import GROUNDING_METRICS, ConsolidatedMetrics
from graph.build.models import (
    AnsweredElsewhere,
    BuildResult,
    CanonicalNode,
    CatalogParts,
    ChangedNode,
    ChangeReport,
    ChangeSet,
    DiffArtifact,
    DocumentRec,
    EdgeDelete,
    EdgesArtifact,
    EvidenceLines,
    FragmentRec,
    HubMerge,
    HubName,
    InferenceReport,
    JudgedItem,
    KnownTermsShown,
    NodeCreate,
    NodeDelete,
    NodesArtifact,
    PairCandidate,
    PlanArtifact,
    RawEdge,
    RawNode,
    RefusedDedupPair,
    ResolveArtifact,
    SpecChanges,
    TargetCounters,
    TargetEdge,
    TargetGraph,
    TargetNode,
    VerificationMetrics,
    VerificationSummary,
)
from graph.build.options import BuildOptions
from graph.build.ports import BuildRecord
from graph.build.vectors import VECTOR_KIND, encode_vector, text_hash
from graph.core.identity import SpecIdRow
from graph_report import (
    Reference,
    StoredBuild,
    build_report,
    compare,
    flatten,
    hub_subsets,
    match_nodes,
    metric_rows,
    noise_band,
    parse_build_spec,
    precision_recall,
    reference_matches,
    render_markdown,
    review_counts,
    review_sample,
    verdict,
    write_report,
)
from graph_support import frozen_ontology, shipped_ontology

# Mechanics tests: pinned to the frozen v1.3 ontology (graph_support.FROZEN_ONTOLOGY_FILE), not the shipped one.
ONTOLOGY = frozen_ontology()
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FR, FEAT, MOD, CAP, IFC = "FunctionalRequirement", "Feature", "Module", "Capability", "Interface"


def tnode(key: str, label: str, name: str, fragments: Sequence[str]) -> TargetNode:
    return TargetNode(
        identity_key=key,
        label=label,
        name=name,
        evidence=tuple(EvidenceLines(fragment_id=fid, line_start=1, line_end=2) for fid in fragments),
    )


def tedge(relation: str, source: str, target: str, fragments: Sequence[str] = ("f1",)) -> TargetEdge:
    return TargetEdge(
        edge_key=f"{source}|{relation}|{target}",
        relation=relation,
        source_key=source,
        target_key=target,
        basis="stated",
        fragment_ids=tuple(fragments),
    )


def stored(role: str, target: TargetGraph, **extra: Any) -> StoredBuild:
    return StoredBuild(role=role, project="p", build_id=f"{role}-1", target=target, **extra)


BASELINE = TargetGraph(
    nodes=(
        tnode("b:r1", FR, "Login with email", ["f1"]),
        tnode("b:r2", FR, "Reset password", ["f2"]),
        tnode("b:r3", FR, "Old thing", ["f9"]),  # gone
        tnode("b:r4", FR, "Billing API", ["f8"]),  # relabelled into a new type
        tnode("b:feat", FEAT, "Accounts", ["f1", "f2"]),
        tnode("b:m1", MOD, "Auth service", ["f3"]),  # merged with m2
        tnode("b:m2", MOD, "Auth svc", ["f4"]),
        tnode("b:pay", MOD, "Payments", ["f5", "f6"]),  # split in two
        tnode("b:cap", CAP, "Checkout", ["f7"]),  # relabelled to a known type
    ),
    edges=(
        tedge("PART_OF", "b:r1", "b:feat"),  # unchanged
        tedge("PART_OF", "b:r2", "b:m1", ["f2"]),  # relation changed
        tedge("PART_OF", "b:r3", "b:feat", ["f9"]),  # gone with its end
        tedge("PART_OF", "b:feat", "b:m2"),  # unchanged through the merge
        tedge("RELATES_TO", "b:r1", "b:m1"),  # gone: quarantined
    ),
)
CANDIDATE = TargetGraph(
    nodes=(
        tnode("c:r1", FR, "Login with e-mail", ["f1"]),
        tnode("c:r2", FR, "Reset password", ["f2"]),
        tnode("c:r4", IFC, "Billing API", ["f8"]),
        tnode("c:r5", FR, "New requirement", ["f10"]),  # new
        tnode("c:q", "OpenQuestion", "Which payment provider?", ["f11"]),  # new type
        tnode("c:feat", FEAT, "Accounts", ["f1", "f2"]),
        tnode("c:auth", MOD, "Auth service", ["f3", "f4"]),
        tnode("c:pay-api", MOD, "Payments API", ["f5"]),
        tnode("c:pay-db", MOD, "Payments DB", ["f6"]),
        tnode("c:cap", FEAT, "Checkout", ["f7"]),
    ),
    edges=(
        tedge("PART_OF", "c:r1", "c:feat"),
        tedge("RELATES_TO", "c:auth", "c:r2", ["f2"]),
        tedge("PART_OF", "c:feat", "c:auth"),
        tedge("PART_OF", "c:r5", "c:feat", ["f10"]),  # new, with a new end
    ),
)
QUARANTINE = (
    {
        "reason": "external_owner",
        "relation": "RELATES_TO",
        "source_label": FR,
        "target_label": MOD,
        "source_spec_id": "FR::LOGIN",
        "target_spec_id": "MOD::AUTH",
        "basis": "stated",
        "fragment_ids": ["f1"],
    },
)


def comparison() -> report_tool.Comparison:
    return compare(stored("baseline", BASELINE), stored("candidate", CANDIDATE, quarantine=QUARANTINE), ONTOLOGY)


def changes(kind: str) -> list[tuple[tuple[str, ...], tuple[str, ...], str]]:
    return [
        (tuple(ref.key for ref in change.before), tuple(ref.key for ref in change.after), change.explanation)
        for change in comparison().nodes
        if change.kind == kind
    ]


# Node matching


def test_nodes_match_one_to_one_by_fragments_then_label_and_name() -> None:
    matching = match_nodes(BASELINE, CANDIDATE)
    pairs, merged, split = matching.pairs, matching.merged, matching.split
    assert pairs["b:r1"] == "c:r1"  # same fragments, same label, name differs
    assert pairs["b:m1"] == "c:auth"  # Jaccard 0.5 tie with m2: the same name wins
    assert merged == {"b:m2": "c:auth"}
    assert pairs["b:pay"] == "c:pay-db"  # the closer name ("payments db")
    assert split == {"c:pay-api": "b:pay"}
    assert "b:r3" not in pairs


def test_little_overlap_and_another_name_is_no_match() -> None:
    before = TargetGraph(nodes=(tnode("b", FR, "x", ["f1", "f2", "f3"]),))
    after = TargetGraph(nodes=(tnode("c", FR, "y", ["f1", "f4", "f5"]),))
    assert match_nodes(before, after) == report_tool.NodeMatching({}, {}, {})


def test_same_label_and_name_with_a_shared_fragment_match_when_fragments_drift() -> None:
    hub = [f"f{index}" for index in range(10)]
    before = TargetGraph(nodes=(tnode("b", MOD, "Checkout", hub),))
    after = TargetGraph(nodes=(tnode("c", MOD, "checkout", ["f0", *(f"g{index}" for index in range(6))]),))
    assert match_nodes(before, after).pairs == {"b": "c"}  # Jaccard 1/16, no containment
    renamed = TargetGraph(nodes=(tnode("c", MOD, "Cart", ["f0", *(f"g{index}" for index in range(6))]),))
    assert match_nodes(before, renamed).pairs == {}


def test_a_three_way_merge_is_found_by_containment_and_explained_only_with_evidence() -> None:
    before = TargetGraph(
        nodes=(
            tnode("a", FR, "Card payment", ["f1"]),
            tnode("b", FR, "Card payment flow", ["f2"]),
            tnode("c", FR, "Card payment form", ["f3"]),
            tnode("m", MOD, "Payments", ["f9"]),
        ),
        edges=(tedge("PART_OF", "a", "m", ["f1"]),),
    )
    after = TargetGraph(
        nodes=(tnode("x", FR, "Card payments", ["f1", "f2", "f3"]), tnode("n", MOD, "Payments", ["f9"])),
        edges=(tedge("PART_OF", "x", "n", ["f1"]),),
    )
    found = compare(stored("baseline", before), stored("candidate", after), ONTOLOGY)
    kinds = [
        (change.kind, [r.key for r in change.before], [r.key for r in change.after], change.explanation)
        for change in found.nodes
    ]
    assert kinds == [("merged", ["a", "b", "c"], ["x"], "merged (cause unknown)")]  # Jaccard 1/3 each: no pair
    assert found.edges == () and len(found.unchanged_edges) == 1
    # The candidate's Resolve put raw nodes of those names (or their keys) into x: a Resolve merge.
    resolved = {"x": frozenset({"card payment", "card payment flow", "c"})}
    shown = compare(stored("baseline", before), stored("candidate", after, resolved=resolved), ONTOLOGY)
    assert [change.explanation for change in shown.nodes] == ["merge (Resolve)"]
    partial = {"x": frozenset({"card payment"})}
    unknown = compare(stored("baseline", before), stored("candidate", after, resolved=partial), ONTOLOGY)
    assert [change.explanation for change in unknown.nodes] == ["merged (cause unknown)"]


def test_a_three_way_split_is_found_by_containment() -> None:
    before = TargetGraph(
        nodes=(tnode("b", MOD, "Platform", ["f1", "f2", "f3"]), tnode("r", FR, "Run it", ["f9"])),
        edges=(tedge("PART_OF", "r", "b", ["f9"]),),
    )
    after = TargetGraph(
        nodes=(
            tnode("x", MOD, "Platform web", ["f1"]),
            tnode("y", MOD, "Platform api", ["f2"]),
            tnode("z", MOD, "Platform db", ["f3"]),
            tnode("s", FR, "Run it", ["f9"]),
        ),
        edges=(tedge("PART_OF", "s", "x", ["f9"]), tedge("RELATES_TO", "s", "z", ["f9"])),
    )
    found = compare(stored("baseline", before), stored("candidate", after), ONTOLOGY)
    kinds = [(change.kind, [r.key for r in change.before], [r.key for r in change.after]) for change in found.nodes]
    assert kinds == [("split", ["b"], ["x", "y", "z"])]
    assert len(found.unchanged_edges) == 1  # r->b and s->x: x stands for the split group
    assert [(change.kind, change.explanation) for change in found.edges] == [
        ("new", "end node split: split (cause unknown)")
    ]
    resolved = {"b": frozenset({"platform web", "platform api", "platform db"})}
    shown = compare(stored("baseline", before, resolved=resolved), stored("candidate", after), ONTOLOGY)
    assert [change.explanation for change in shown.nodes] == ["split: the baseline's Resolve had merged them"]
    renamed = TargetGraph(nodes=(tnode("x", MOD, "Web", ["f1"]), tnode("y", MOD, "Api", ["f2"])))
    unrelated = compare(stored("baseline", before), stored("candidate", renamed), ONTOLOGY)
    assert sorted(change.kind for change in unrelated.nodes) == ["gone", "gone", "new", "new"]  # names too far


def test_many_nodes_on_one_fragment_pair_only_with_similar_names() -> None:
    before = TargetGraph(
        nodes=(
            tnode("b1", CAP, "Authentication", ["f1"]),
            tnode("b2", FR, "Login with email", ["f1"]),
            tnode("b3", MOD, "Product Catalog Service (PCH)", ["f1"]),
        )
    )
    after = TargetGraph(
        nodes=(
            tnode("c1", MOD, "Product catalog service (PCH)", ["f1"]),
            tnode("c2", FR, "Login with e-mail", ["f1"]),
            tnode("c3", FEAT, "Image upload", ["f1"]),
        )
    )
    found = compare(stored("baseline", before), stored("candidate", after), ONTOLOGY)
    assert found.pairs == {"b2": "c2", "b3": "c1"}
    assert sorted((change.kind, (change.before or change.after)[0].key) for change in found.nodes) == [
        ("gone", "b1"),
        ("new", "c3"),
    ]


def test_fragments_many_nodes_share_weigh_less() -> None:
    common = [tnode(f"n{index}", FR, f"Filler {index}", ["c1", "c2"]) for index in range(9)]
    before = TargetGraph(nodes=(tnode("b", MOD, "Search", ["r1", "c1", "c2"]), *common))
    after = TargetGraph(nodes=(tnode("x", MOD, "Search service", ["r1"]),))
    # Plain Jaccard 1/3; the common fragments weigh 1/10 each: (1) / (1 + 0.2) = 0.83.
    assert report_tool.match_nodes(before, after).pairs == {"b": "x"}
    weights = report_tool.fragment_weights(before, after)
    assert (weights["r1"], weights["c1"]) == (1.0, 0.1)


def test_name_similarity() -> None:
    similarity = report_tool.name_similarity
    assert similarity("Login with email", "login with email.") == 1.0
    assert similarity("Auth svc", "Auth service") >= report_tool.NAME_SIMILARITY
    assert similarity("Authentication", "Product Catalog Service (PCH)") < report_tool.NAME_SIMILARITY
    assert similarity("Web app api", "api web app") == 1.0  # word sets


def test_only_the_merged_away_end_explains_a_gone_edge() -> None:
    before = TargetGraph(
        nodes=(
            tnode("a", MOD, "Auth service", ["f1", "f2"]),
            tnode("b", MOD, "Auth svc", ["f3"]),
            tnode("r", FR, "Login", ["f5"]),
            tnode("s", FR, "Logout", ["f6"]),
        ),
        edges=(tedge("PART_OF", "r", "a", ["f5"]), tedge("PART_OF", "s", "b", ["f6"])),
    )
    after = TargetGraph(
        nodes=(
            tnode("x", MOD, "Auth service", ["f1", "f2", "f3"]),
            tnode("r2", FR, "Login", ["f5"]),
            tnode("s2", FR, "Logout", ["f6"]),
        ),
    )
    found = compare(stored("baseline", before), stored("candidate", after), ONTOLOGY)
    assert [n.kind for n in found.nodes] == ["merged"]
    explained = {change.before.source.key: change.explanation for change in found.edges if change.before}
    assert explained == {"r": "unexplained", "s": "end node merged: merged (cause unknown)"}


def test_a_reversed_edge_is_a_direction_change_but_not_on_a_symmetric_relation() -> None:
    nodes = (tnode("m1", MOD, "Web", ["f1"]), tnode("m2", MOD, "Api", ["f2"]), tnode("r", FR, "Login", ["f3"]))
    before = TargetGraph(nodes=nodes, edges=(tedge("PART_OF", "m1", "m2"), tedge("RELATES_TO", "r", "m1")))
    after = TargetGraph(nodes=nodes, edges=(tedge("PART_OF", "m2", "m1"), tedge("RELATES_TO", "m1", "r")))
    found = compare(stored("baseline", before), stored("candidate", after), ONTOLOGY)
    assert [(change.kind, change.before.relation if change.before else None) for change in found.edges] == [
        ("direction_changed", "PART_OF")
    ]
    assert [pair[0].relation for pair in found.unchanged_edges] == ["RELATES_TO"]  # stored either way round
    after_other = TargetGraph(nodes=nodes, edges=(tedge("INTEGRATES_WITH", "m1", "m2"), tedge("RELATES_TO", "r", "m1")))
    other = compare(stored("baseline", before), stored("candidate", after_other), ONTOLOGY)
    assert [change.kind for change in other.edges] == ["relation_changed"]
    reversed_other = TargetGraph(
        nodes=nodes, edges=(tedge("INTEGRATES_WITH", "m2", "m1"), tedge("RELATES_TO", "r", "m1"))
    )
    flipped = compare(stored("baseline", before), stored("candidate", reversed_other), ONTOLOGY)
    assert sorted(change.kind for change in flipped.edges) == ["gone", "new"]  # ordered ends differ


def test_node_changes_are_classified_and_explained() -> None:
    assert changes("merged") == [(("b:m1", "b:m2"), ("c:auth",), "merged (cause unknown)")]
    assert changes("split") == [(("b:pay",), ("c:pay-api", "c:pay-db"), "split (cause unknown)")]
    assert changes("gone") == [(("b:r3",), (), "unexplained")]
    assert sorted(changes("relabelled")) == [
        (("b:cap",), ("c:cap",), "unexplained"),  # Feature is a known type
        (("b:r4",), ("c:r4",), "relabel into new type Interface (T5/T13/T17)"),
    ]
    assert sorted(changes("new")) == [
        ((), ("c:q",), "new type OpenQuestion (T5/T13/T17)"),
        ((), ("c:r5",), "unexplained"),
    ]


def test_known_labels_come_from_the_baseline_ontology_when_given() -> None:
    baseline = stored("baseline", BASELINE, labels=frozenset({FR, FEAT, MOD, CAP, IFC}))
    found = compare(baseline, stored("candidate", CANDIDATE), ONTOLOGY)
    relabel = next(change for change in found.nodes if change.after and change.after[0].key == "c:r4")
    assert relabel.explanation == "unexplained"


# Edge matching


def test_edge_changes_and_explanations() -> None:
    edges = [
        (change.kind, change.before.relation if change.before else None, change.explanation)
        for change in comparison().edges
    ]
    assert sorted(edges, key=str) == sorted(
        [
            ("gone", "PART_OF", "end node gone: unexplained"),
            ("gone", "RELATES_TO", "quarantine: external_owner (T15)"),
            ("new", None, "end node new: unexplained"),
            ("relation_changed", "PART_OF", "unexplained"),
        ],
        key=str,
    )
    assert len(comparison().unchanged_edges) == 2  # r1->feat, and feat->m2 through the merge


def test_comparison_is_deterministic() -> None:
    assert comparison() == comparison()
    shuffled = TargetGraph(nodes=tuple(reversed(CANDIDATE.nodes)), edges=tuple(reversed(CANDIDATE.edges)))
    other = compare(stored("baseline", BASELINE), stored("candidate", shuffled, quarantine=QUARANTINE), ONTOLOGY)
    assert other.nodes == comparison().nodes
    assert other.edges == comparison().edges


# Metrics, deltas and noise


def test_flatten_gives_numeric_leaves_and_none_for_absent_fields() -> None:
    flat = flatten(ConsolidatedMetrics(nodes_by_label={FR: 2}, edges_by_relation_basis={"PART_OF": {"stated": 3}}))
    assert flat["nodes_by_label." + FR] == 2
    assert flat["edges_by_relation_basis.PART_OF.stated"] == 3
    assert flat["quarantine_by_reason"] is None
    assert flatten(None) == {}
    judged = flatten(
        ConsolidatedMetrics(judge_calls={"hub_consolidation": 29}, judge_failed_calls={"hub_consolidation": 28})
    )
    assert (judged["judge_calls.hub_consolidation"], judged["judge_failed_calls.hub_consolidation"]) == (29, 28)


def test_noise_band_and_verdict() -> None:
    assert noise_band(10, 12) == 2
    assert noise_band(10, 15, 12) == 5  # the range over every run
    assert noise_band(10, None) is None and noise_band(10) is None
    assert verdict(1, 2) == "no difference (|delta| <= noise)"
    assert verdict(-2, 2) == "no difference (|delta| <= noise)"
    assert verdict(3, 2) == "higher"
    assert verdict(-1, None) == "lower"
    assert verdict(0, None) == "no difference"
    assert verdict(None, 1) == "n/a"
    # Within the runs' range is preferred; outside it the band still applies to |delta|.
    assert verdict(2, 5, candidate=12, runs=(10, 15, 12)) == "no difference (within the baseline range)"
    assert verdict(4, 5, candidate=14, runs=(10, 13)) == "no difference (|delta| <= noise)"
    assert verdict(7, 5, candidate=17, runs=(10, 15)) == "higher"


def test_metric_rows_compare_candidate_with_the_first_baseline_within_the_noise() -> None:
    rows = {
        row.name: row
        for row in metric_rows(
            {
                "original": ConsolidatedMetrics(nodes_by_label={FR: 9}, spine_unreachable_before=7),
                "baseline": ConsolidatedMetrics(nodes_by_label={FR: 10}, spine_unreachable_before=5),
                "baseline2": ConsolidatedMetrics(nodes_by_label={FR: 12}, spine_unreachable_before=5),
                "candidate": ConsolidatedMetrics(
                    nodes_by_label={FR: 11, IFC: 2}, spine_unreachable_before=8, quarantine_by_reason={"depth_cut": 2}
                ),
            }
        )
    }
    fr = rows["nodes_by_label." + FR]
    assert (fr.delta, fr.noise, fr.verdict, fr.delta_original) == (
        1,
        2,
        "no difference (within the baseline range)",
        2,
    )
    ifc = rows["nodes_by_label." + IFC]
    assert ifc.values == {"original": 0, "baseline": 0, "baseline2": 0, "candidate": 2}
    assert ifc.verdict == "higher"
    spine = rows["spine_unreachable_before"]
    assert (spine.delta, spine.noise, spine.verdict) == (3, 0, "higher")
    quarantine = rows["quarantine_by_reason.depth_cut"]
    assert quarantine.values["baseline"] is None and quarantine.verdict == "n/a"
    assert "quarantine_by_reason" not in rows  # its leaves carry the n/a
    assert rows["owner_conflicts_judged"].verdict == "n/a"
    assert list(rows) == sorted(rows)


def test_without_a_second_baseline_there_is_no_noise_band() -> None:
    rows = metric_rows({"baseline": ConsolidatedMetrics(exclusive_conflicts=1), "candidate": ConsolidatedMetrics()})
    row = next(row for row in rows if row.name == "exclusive_conflicts")
    assert (row.delta, row.noise, row.verdict) == (-1, None, "lower")


# Precision and recall


def test_precision_and_recall_per_type() -> None:
    target = TargetGraph(
        nodes=(
            tnode("login", FR, "Login with email", ["f1"]),
            tnode("reset", FR, "Change avatar", ["f2"]),  # another item on the reference's fragment
            tnode("extra", FR, "Extra", ["f3"]),  # outside the reference's fragments
            tnode("auth", MOD, "Auth Service", ["f1"]),
        ),
        edges=(tedge("PART_OF", "login", "auth"), tedge("PART_OF", "extra", "auth")),
    )
    reference = Reference(
        nodes=(
            {"label": FR, "name": "Login with email", "fragment": "f1"},
            {"label": FR, "name": "Reset password", "fragment": "f2"},
            {"label": MOD, "name": "auth service", "fragment": "f1"},
        ),
        edges=(
            {"relation": "PART_OF", "source": "Login with email", "target": "Auth service"},
            {"relation": "PART_OF", "source": "Reset password", "target": "Auth service"},
        ),
    )
    scores = precision_recall(target, reference, ONTOLOGY)
    assert scores["nodes"][FR] == {
        "reference": 2,
        "found": 1,
        "recall": 0.5,
        "in_scope": 2,
        "correct": 1,
        "precision": 0.5,
    }
    assert scores["nodes"][MOD]["precision"] == scores["nodes"][MOD]["recall"] == 1.0
    assert scores["edges"]["PART_OF"] == {
        "reference": 2,
        "found": 1,
        "recall": 0.5,
        "in_scope": 1,
        "correct": 1,
        "precision": 1.0,
    }


def test_a_reference_node_matches_by_fragment_and_a_close_name() -> None:
    target = TargetGraph(nodes=(tnode("a", FR, "Login with an email", ["f1"]),))
    reference = Reference(nodes=({"label": FR, "name": "Login with email", "fragment": "f1"},))
    assert precision_recall(target, reference)["nodes"][FR]["found"] == 1


# Reviewed sample


def test_review_sample_puts_changed_items_first_and_is_bounded() -> None:
    rows = review_sample(stored("baseline", BASELINE), stored("candidate", CANDIDATE), comparison(), nodes=6, edges=3)
    nodes = [row for row in rows if row["kind"] == "node"]
    edges = [row for row in rows if row["kind"] == "edge"]
    assert len(nodes) == 6 and len(edges) == 3
    assert all(row["change"] != "unchanged" for row in nodes)
    assert [row["row_id"] for row in rows] == [f"{'N' if i <= 6 else 'E'}{i:03d}" for i in range(1, 10)]
    assert all(row["judgement"] == "" for row in rows)
    everything = review_sample(stored("baseline", BASELINE), stored("candidate", CANDIDATE), comparison())
    assert {row["change"] for row in everything} >= {"unchanged", "merged", "gone", "relation_changed"}
    assert everything == review_sample(stored("baseline", BASELINE), stored("candidate", CANDIDATE), comparison())


def test_review_counts_per_kind_and_side() -> None:
    rows = [
        {"kind": "node", "side": "before", "judgement": "correct"},
        {"kind": "node", "side": "before", "judgement": " Wrong "},
        {"kind": "node", "side": "after", "judgement": "unsure"},
        {"kind": "edge", "side": "after", "judgement": ""},
    ]
    assert review_counts(rows) == {
        "edge": {"after": {"correct": 0, "wrong": 0, "unsure": 0, "blank": 1}},
        "node": {
            "after": {"correct": 0, "wrong": 0, "unsure": 1, "blank": 0},
            "before": {"correct": 1, "wrong": 1, "unsure": 0, "blank": 0},
        },
    }


# The report


def test_report_has_twelve_sections_with_n_a_for_absent_data(tmp_path: Path) -> None:
    builds = {"baseline": stored("baseline", BASELINE), "candidate": stored("candidate", CANDIDATE)}
    ontologies = dict.fromkeys(builds, ONTOLOGY)
    report, sample = build_report(builds, ontologies, sample_path="review-sample.csv")
    markdown = render_markdown(report)
    titles = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert titles == [
        "## 1. Summary",
        "## 2. Time and cost",
        "## 3. Graph size",
        "## 4. Metrics",
        "## 5. Graph comparison",
        "## 6. Quarantine",
        "## 7. Reviewed sample",
        "## 8. Precision and recall",
        "## 9. Hub names and known terms",
        "## 10. Identity samples",
        "## 11. Edge samples",
        "## 12. Inference and change report",
    ]
    edge_samples = report["11_edge_samples"]
    assert [edge_samples[name]["rows"] for name in ("near_edges", "xdoc_items", "answered_drops")] == [None] * 3
    assert "- near edges: n/a (not recorded by the candidate's artifacts)" in markdown
    assert [item["rows"] for item in report["12_inference"]["samples"].values()] == [None] * 2
    assert "- change report nodes: n/a (not recorded by the candidate's artifacts)" in markdown
    assert "n/a (no eval file)" in markdown
    identity = report["10_identity_samples"]
    names = ("merged_pairs", "near_unmerged_pairs", "refused_pairs", "derive_fallback_edges")
    assert [identity[name]["rows"] for name in names] == [None] * 4
    assert "- merged pairs: n/a (not recorded by the candidate's artifacts)" in markdown
    assert report["9_hub_names"]["body_only_hubs"] is None
    assert report["9_hub_names"]["nearby_unsupported"]["rows"] is None
    assert report["2_time_and_cost"] is None and report["8_precision_recall"] is None
    assert report["6_quarantine"]["candidate"]["quarantine_by_reason"] is None
    assert report["1_summary"]["node_changes"] == {"gone": 1, "merged": 1, "new": 2, "relabelled": 2, "split": 1}
    assert "One baseline run only" in markdown
    assert "Judgements: n/a." in markdown
    assert render_markdown(report) == markdown
    write_report(report, sample, tmp_path / "out")
    assert json.loads((tmp_path / "out" / "report.json").read_text()) == json.loads(json.dumps(report, default=str))
    assert (tmp_path / "out" / "report.md").read_text() == markdown
    csv_lines = (tmp_path / "out" / "review-sample.csv").read_text().splitlines()
    assert csv_lines[0].startswith("row_id,kind,side,change") and len(csv_lines) == len(sample) + 1


def test_report_with_noise_eval_reference_and_review() -> None:
    builds = {
        "original": stored("original", BASELINE),
        "baseline": stored("baseline", BASELINE),
        "baseline2": stored("baseline2", CANDIDATE),
        "candidate": stored("candidate", CANDIDATE),
    }
    evals = {
        "candidate": {
            "wall_s": 12.5,
            "steps": {"plan": {"wall_s": 0.5}, "conform": {"wall_s": 3.0}},
            "total": {"by_class": {"judge": {"calls": 4, "input_tokens": 100, "output_tokens": 20, "requests": 4}}},
            "estimate_vs_actual": {"node_extraction": {"estimate": [3, 900], "actual": [4, 1000]}},
        },
        "baseline": {
            "wall_s": 11.0,
            "steps": {"plan": {"wall_s": 0.5}, "conform": {"wall_s": 3.0}},
            "total": {
                "by_class": {
                    "extraction": {"calls": 2, "input_tokens": 400, "output_tokens": 50, "cached_input_tokens": 100},
                    "judge": {"calls": 1, "input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 0},
                }
            },
        },
    }
    reference = Reference(nodes=({"label": FR, "name": "Reset password", "fragment": "f2"},))
    review = [{"kind": "node", "side": "after", "judgement": "correct"}]
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY), evals=evals, reference=reference, review=review)
    markdown = render_markdown(report)
    assert report["1_summary"]["noise_band"] is True
    # The second baseline equals the candidate: every delta is within the noise.
    assert report["1_summary"]["key_deltas"] == []

    assert report["2_time_and_cost"]["candidate"]["by_class"] == {
        "judge": {"calls": 4, "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}
    }
    assert "| extraction | 2 / 400 / 50 / 100 | n/a |" in markdown
    assert "| total | 3 / 500 / 60 / 100 | 4 / 100 / 20 / 0 |" in markdown
    assert "Cached input share: baseline 20.0%, candidate 0.0%." in markdown
    assert "| conform | 3 |" in markdown and "| node_extraction | 3 / 900 | 4 / 1000 |" in markdown
    # every baseline run gets its precision and recall, in role order
    assert list(report["8_precision_recall"]) == ["original", "baseline", "baseline2", "candidate"]
    assert "| type | original | baseline | baseline2 | candidate |" in markdown
    assert report["7_reviewed_sample"]["judgements"]["node"]["after"]["correct"] == 1
    assert "delta vs original" in markdown


# Inputs


def test_build_specs_and_eval_files(tmp_path: Path) -> None:
    build = "0123456789abcdef0123456789abcdef"
    assert parse_build_spec("state:proj") == report_tool.BuildSpec(Path("state"), "proj", None)
    assert parse_build_spec(f"state:proj:{build}") == report_tool.BuildSpec(Path("state"), "proj", build)
    assert parse_build_spec(f"c:/x:y/state:proj:{build}") == report_tool.BuildSpec(Path("c:/x:y/state"), "proj", build)
    assert parse_build_spec("a:b/state:proj") == report_tool.BuildSpec(Path("a:b/state"), "proj", None)
    for bad in ("state", "state:", f"proj:{build}", ":proj"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_build_spec(bad)
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"builds": {"B1": {"wall_s": 1}, "B2": {"wall_s": 2}}}))
    assert report_tool.load_eval(f"candidate={path}") == ("candidate", {"wall_s": 2})
    assert report_tool.load_eval(f"baseline={path}#B1") == ("baseline", {"wall_s": 1})
    assert report_tool.load_eval(f"baseline3={path}#B1") == ("baseline3", {"wall_s": 1})
    for bad in ("other", "baseline1", "baselinex"):
        with pytest.raises(argparse.ArgumentTypeError):
            report_tool.load_eval(f"{bad}={path}")


ARTIFACT_QUARANTINE = (
    # As Conform stores it: ends by identity key; the module's key has no spec_id in the registry.
    {key: value for key, value in QUARANTINE[0].items() if not key.endswith("_spec_id")}
    | {"source_key": "c:r1", "target_key": "c:auth"},
)


async def test_a_build_is_read_back_from_the_file_stores(tmp_path: Path) -> None:
    payload = CANDIDATE.model_dump(mode="json") | {"quarantine": list(ARTIFACT_QUARANTINE)}
    build_id = "c" * 32
    stores = open_file_stores(tmp_path)
    await stores.builds.put_artifact("p", build_id, "conform", payload)
    await stores.registry.record(
        "p", build_id, [SpecIdRow(spec_id="FR::LOGIN", identity_key="c:r1", uuid="u", state="active")]
    )
    build = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", build_id), "candidate", ONTOLOGY)
    assert build.target.nodes == CANDIDATE.nodes and build.target.edges == CANDIDATE.edges
    assert len(build.target.quarantine) == 1
    # the report's entries name the ends by spec_id (None: unknown), never by identity key
    assert build.quarantine == ({**QUARANTINE[0], "target_spec_id": None},)
    builds = {"baseline": stored("baseline", BASELINE), "candidate": build}
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    assert report["6_quarantine"]["candidate"]["quarantine_by_reason"] == {"external_owner": 1}  # from the metrics
    assert (build.result, build.plan) == (None, None)
    assert build.labels == frozenset(ONTOLOGY.labels)
    with pytest.raises(ValueError, match="no finished build"):
        await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p"), "candidate", ONTOLOGY)


def test_quarantine_section_counts_the_artifact_entries_while_the_metrics_are_n_a() -> None:
    assert report_tool.quarantine_counts(QUARANTINE * 2) == {
        "quarantine_by_reason": {"external_owner": 2},
        "quarantine_by_pair": {f"RELATES_TO|{FR}|{MOD}": 2},
    }
    builds = {
        "baseline": stored("baseline", BASELINE),
        "candidate": stored("candidate", CANDIDATE, quarantine=QUARANTINE),
    }
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    assert report["6_quarantine"]["baseline"]["quarantine_by_reason"] is None
    assert report["6_quarantine"]["candidate"]["quarantine_by_reason"] == {"external_owner": 1}
    assert "| external_owner | n/a | 1 | T15 |" in render_markdown(report)


def test_quarantine_needs_a_shared_fragment_and_matching_spec_ids() -> None:
    def explanation(entry: dict[str, object], spec_ids: dict[str, str] | None = None) -> str:
        baseline = stored("baseline", BASELINE, spec_ids=spec_ids or {})
        found = compare(baseline, stored("candidate", CANDIDATE, quarantine=(entry,)), ONTOLOGY)
        return next(c.explanation for c in found.edges if c.before and c.before.relation == "RELATES_TO")

    entry = QUARANTINE[0]
    assert explanation(entry) == "quarantine: external_owner (T15)"  # no spec ids known: end labels
    assert explanation(entry | {"fragment_ids": ["f99"]}) == "unexplained"
    assert explanation(entry | {"fragment_ids": []}) == "unexplained"
    ids = {"b:r1": "FR::LOGIN", "b:m1": "MOD::AUTH"}
    assert explanation(entry, ids) == "quarantine: external_owner (T15)"
    assert explanation(entry | {"source_spec_id": "FR::OTHER"}, ids) == "unexplained"
    assert explanation(entry | {"source_label": MOD}) == "unexplained"


def test_precision_and_recall_are_n_a_for_a_type_without_reference_items() -> None:
    target = TargetGraph(nodes=(tnode("a", FR, "Login", ["f1"]), tnode("m", MOD, "Auth", ["f1"])))
    reference = Reference(nodes=({"label": FR, "name": "Login", "fragment": "f1"},))
    scores = precision_recall(target, reference)
    assert (scores["nodes"][MOD]["precision"], scores["nodes"][MOD]["recall"]) == (None, None)
    assert scores["nodes"][MOD]["in_scope"] == 1
    assert scores["nodes"][FR]["precision"] == scores["nodes"][FR]["recall"] == 1.0


def test_reference_edge_ends_resolve_by_label_and_name() -> None:
    target = TargetGraph(
        nodes=(
            tnode("f", FEAT, "Accounts", ["f1"]),
            tnode("m", MOD, "Accounts", ["f1"]),
            tnode("r", FR, "Login", ["f1"]),
        ),
        edges=(tedge("PART_OF", "r", "m"),),
    )
    reference = Reference(
        nodes=(
            {"label": FEAT, "name": "Accounts"},
            {"label": MOD, "name": "Accounts"},
            {"label": FR, "name": "Login"},
        ),
        edges=({"relation": "PART_OF", "source": "Login", "target": "Accounts", "target_label": MOD},),
    )
    assert precision_recall(target, reference)["edges"]["PART_OF"]["found"] == 1
    by_name = Reference(
        nodes=reference.nodes, edges=({"relation": "PART_OF", "source": "Login", "target": "Accounts"},)
    )
    assert precision_recall(target, by_name)["edges"]["PART_OF"]["found"] == 0  # the first "Accounts": the feature


async def test_a_review_rerun_keeps_the_judged_sample_under_the_same_out(tmp_path: Path) -> None:
    state, out = tmp_path / "state", tmp_path / "out"
    builds = open_file_stores(state).builds
    first, second = "a" * 32, "b" * 32
    await builds.put_artifact("p", first, "conform", BASELINE.model_dump(mode="json"))
    await builds.put_artifact("p", second, "conform", CANDIDATE.model_dump(mode="json"))
    argv = ["--baseline", f"{state}:p:{first}", "--candidate", f"{state}:p:{second}", "--out", str(out)]
    await report_tool.main(argv)
    sample = out / "review-sample.csv"
    rows = report_tool.read_review(sample)
    rows[0]["judgement"] = "correct"
    report_tool.write_review_sample(rows, sample)
    judged = sample.read_text()
    await report_tool.main([*argv, "--review", str(sample)])
    assert sample.read_text() == judged
    report = json.loads((out / "report.json").read_text())
    counts = report["7_reviewed_sample"]["judgements"][rows[0]["kind"]][rows[0]["side"]]
    assert counts["correct"] == 1
    assert report["7_reviewed_sample"]["sample"] == str(sample)


def test_recomputed_verify_metrics_show_next_to_the_stored_ones_where_they_differ() -> None:
    stored_metrics = VerificationMetrics(spine_unreachable_before=1, spine_unreachable=1, exclusive_conflicts=5)
    result = BuildResult(
        project_id="p",
        build_id="b",
        mode="first",
        status="succeeded",
        verification=VerificationSummary(ok=True, metrics=stored_metrics),
        started_at=T0,
        finished_at=T0,
    )
    builds = {"baseline": stored("baseline", BASELINE, result=result), "candidate": stored("candidate", CANDIDATE)}
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY), recompute=True)
    rows = {row["name"]: row for row in report["4_metrics"]}
    assert rows["exclusive_conflicts"]["values"]["baseline"] == 5
    assert rows["exclusive_conflicts (artifacts)"]["values"]["baseline"] == 0
    assert rows["exclusive_conflicts"]["source"] == "verify / artifacts"
    assert rows["spine_unreachable"]["source"] == "verify / n/a"
    assert "label_disagreement (artifacts)" not in rows  # equal: not repeated
    assert report["recomputed"] == sum(1 for name in rows if name.endswith("(artifacts)"))
    markdown = render_markdown(report)
    assert "rows differ (section 4)" in markdown and "source (baseline / candidate)" in markdown
    plain, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    assert plain["recomputed"] is None
    assert not any(row["name"].endswith("(artifacts)") for row in plain["4_metrics"])


def test_the_ontologies_are_named_and_a_baseline_one_needs_a_candidate_one(tmp_path: Path) -> None:
    base = ["--baseline", "s:p", "--candidate", "s:q", "--out", str(tmp_path)]
    with pytest.raises(SystemExit):
        report_tool.parse_args([*base, "--baseline-ontology", "v13.yaml"])
    args = report_tool.parse_args([*base, "--baseline-ontology", "v13.yaml", "--candidate-ontology", "v20.yaml"])
    assert (args.baseline_ontology, args.candidate_ontology) == (Path("v13.yaml"), Path("v20.yaml"))
    assert report_tool.describe_ontology(ONTOLOGY, None) == f"{ONTOLOGY.name} {ONTOLOGY.version} (shipped)"
    builds = {
        "baseline": stored("baseline", BASELINE, ontology="spec-ontology 1.3 (v13.yaml)"),
        "candidate": stored("candidate", CANDIDATE, ontology="spec-ontology 2.0 (shipped)"),
    }
    markdown = render_markdown(build_report(builds, dict.fromkeys(builds, ONTOLOGY))[0])
    assert "spec-ontology 1.3 (v13.yaml)" in markdown and "spec-ontology 2.0 (shipped)" in markdown


def test_resolved_members_come_from_the_raw_resolve_and_nodes_artifacts() -> None:
    resolve = {
        "canonical": [
            {"identity_key": "k1", "identity_aliases": ["k0"], "member_ids": ["r1", "r2"]},
            {"identity_key": "k2", "member_ids": ["r3", "missing"]},
        ]
    }
    nodes = {"raw_nodes": [{"raw_id": "r1", "name": "Auth svc"}, {"raw_id": "r2", "name": "Auth Service"}]}
    assert report_tool.resolved_members(resolve, nodes) == {
        "k0": frozenset({"auth svc", "auth service", "k0"}),
        "k1": frozenset({"auth svc", "auth service", "k0"}),
        "k2": frozenset(),
    }
    assert report_tool.resolved_members(None, nodes) == {}


def test_three_baseline_runs_give_a_range_and_a_column_each() -> None:
    def run(count: int, conflicts: int) -> ConsolidatedMetrics:
        return ConsolidatedMetrics(nodes_by_label={FR: count}, exclusive_conflicts=conflicts)

    metrics = {
        "baseline": run(10, 1),
        "baseline3": run(16, 1),
        "baseline2": run(13, 1),
        "candidate": run(18, 4),
    }
    rows = {row.name: row for row in metric_rows(metrics)}
    fr = rows["nodes_by_label." + FR]
    assert list(fr.values) == ["baseline", "baseline2", "baseline3", "candidate"]
    assert (fr.delta, fr.noise, fr.verdict) == (8, 6, "higher")  # 18 is outside 10..16 and 8 > 6
    conflicts = rows["exclusive_conflicts"]
    assert (conflicts.delta, conflicts.noise, conflicts.verdict) == (3, 0, "higher")
    within = metric_rows({**metrics, "candidate": run(15, 1)})
    assert next(row for row in within if row.name == "nodes_by_label." + FR).verdict == (
        "no difference (within the baseline range)"
    )
    builds = {
        "baseline": stored("baseline", BASELINE),
        "baseline2": stored("baseline2", CANDIDATE),
        "baseline3": stored("baseline3", BASELINE),
        "candidate": stored("candidate", CANDIDATE),
    }
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    assert report["1_summary"]["baseline_runs"] == 3 and report["1_summary"]["noise_band"] is True
    assert report["1_summary"]["key_deltas"] == []  # the candidate equals a baseline run everywhere
    assert report["5_graph_comparison"]["matched_nodes"] == len(
        compare(builds["baseline"], builds["candidate"], ONTOLOGY).pairs
    )
    markdown = render_markdown(report)
    assert "| metric | baseline | baseline2 | baseline3 | candidate | delta | noise | verdict |" in markdown
    assert "range (max - min) over 3 baseline runs" in markdown
    assert report_tool.ordered_roles(["candidate", "baseline10", "baseline2", "original", "baseline", "x"]) == [
        "original",
        "baseline",
        "baseline2",
        "baseline10",
        "candidate",
    ]
    args = report_tool.parse_args(
        ["--baseline", "s:a", "--baseline", "s:b", "--baseline", "s:c", "--candidate", "s:d", "--out", "o"]
    )
    assert [spec.project for spec in args.baseline] == ["a", "b", "c"]


async def test_grounding_metrics_of_an_older_build_are_measured_on_its_stored_artifacts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fragment = {
        "fragment_id": "a.md#s0",
        "document_id": "a.md",
        "heading_path": [],
        "ordinal": 0,
        "line_start": 1,
        "line_end": 1,
        "text": "Users can export orders.",
        "content_hash": "h",
        "tokens": 4,
    }
    plan = {"mode": "first", "change_set": {"new": ["a.md"]}, "documents": [], "fragments": [fragment]}
    canonical = {
        "cid": "c1",
        "identity_key": "requirement|span|users can export orders",
        "provisional": True,
        "name": "Users can export orders",
        "label": FR,
        "member_ids": ["raw-1"],
        "fragment_ids": ["a.md#s0"],
    }
    build_id = "d" * 32
    stores = open_file_stores(tmp_path)
    artifacts = {
        "conform": CANDIDATE.model_dump(mode="json", exclude={"counters": set(GROUNDING_METRICS)}) | {"quarantine": []},
        "plan": plan,
        "resolve": {"canonical": [canonical]},
        "extract_edges": {"attributes": {"c1": {"quote": "Admins can purge exports"}}},
        "extract_nodes": {"counters": {"label_collapses": 2}},
    }
    for step, payload in artifacts.items():
        await stores.builds.put_artifact("p", build_id, step, payload)
    build = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", build_id), "candidate", ONTOLOGY)
    assert build.resolve_artifact is not None and build.edges_artifact is not None
    builds = {"baseline": stored("baseline", BASELINE), "candidate": build}
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    rows = {row["name"]: row["values"] for row in report["4_metrics"]}
    # the quote attribute snaps nowhere: whole-fragment evidence, provisional key kept, nothing dropped
    assert rows[f"unsnapped_quote_nodes.{FR}"] == {"baseline": None, "candidate": 1}
    assert rows["provisional_keys_left"] == {"baseline": None, "candidate": 1}
    assert rows["attributed_fragments_dropped"] == {"baseline": None, "candidate": 0}
    assert rows["unsnapped_source_quotes"] == {"baseline": None, "candidate": None}
    assert rows["label_collapses"] == {"baseline": None, "candidate": 2}
    # an artifact the current models do not read leaves the metrics n/a
    await stores.builds.put_artifact("p", build_id, "resolve", {"canonical": [canonical | {"unknown": 1}]})
    caplog.set_level(logging.WARNING)
    again = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", build_id), "candidate", ONTOLOGY)
    assert again.resolve_artifact is None
    assert any(record.getMessage() == "example.graph_report.artifact_unreadable" for record in caplog.records)


# Reference fallback for renamed items


def test_a_renamed_reference_item_matches_the_unique_best_node_on_its_fragment() -> None:
    oq = "OpenQuestion"
    target = TargetGraph(
        nodes=(
            tnode("q1", oq, "OI-001: What is the customer commercial model?", ["f2"]),
            tnode("q2", oq, "OI-005: What is the customer data residency?", ["f2"]),
            tnode("q3", oq, "OI-015: Should project budgets be blocking?", ["f9"]),  # not on the fragment
            tnode("r1", FR, "System can return the caller's active projects newest first with cursor paging", ["f3"]),
            tnode("w1", MOD, "Graph worker", ["f4"]),  # hub: same words in another order
            tnode("c1", MOD, "Claude", ["f4"]),  # hub: the shorter surface starts the longer one
        )
    )
    reference = Reference(
        nodes=(
            {"label": oq, "name": "Where must customer data reside?", "fragment": "f2"},
            {"label": oq, "name": "What is the customer commercial model?", "fragment": "f2"},  # q1 by name
            {"label": oq, "name": "Are project budgets blocking?", "fragment": "f2"},  # its node is elsewhere
            {
                "label": FR,
                "name": "FR-PRJ-002: System can list the caller's active projects, newest first",
                "fragment": "f3",
            },
            {"label": MOD, "name": "worker-graph", "fragment": "f4"},
            {"label": MOD, "name": "Claude clients", "fragment": "f4"},
        )
    )
    matched, fallbacks = report_tool.reference_matching(target, reference, ONTOLOGY.hub_labels)
    assert {key: hits for key, hits in matched.items() if hits} == {
        "q1": [1],
        "q2": [0],
        "r1": [3],
        "w1": [4],
        "c1": [5],
    }
    assert [(match.index, match.key) for match in fallbacks] == [(0, "q2"), (3, "r1"), (4, "w1"), (5, "c1")]
    scores = precision_recall(target, reference, ONTOLOGY)
    assert scores["fallback"] == {FR: 1, MOD: 2, oq: 1}
    assert [row["node_name"] for row in scores["fallback_matches"]][:1] == [
        "OI-005: What is the customer data residency?"
    ]


@pytest.mark.parametrize(
    ("label", "reference_name", "node_name"),
    [
        (MOD, "Web app", "Admin web app"),  # a leading qualifier: a narrower hub
        (MOD, "Graph store", "Graph service"),  # one shared word of two
        (FEAT, "Order export", "Order history"),
        (FR, "System can reject expired tokens", "System can reject oversized uploads"),  # sim 0.6, one word
    ],
)
def test_doubtful_renames_are_refused(label: str, reference_name: str, node_name: str) -> None:
    target = TargetGraph(nodes=(tnode("n", label, node_name, ["f1"]),))
    reference = Reference(nodes=({"label": label, "name": reference_name, "fragment": "f1"},))
    matched, fallbacks = report_tool.reference_matching(target, reference, ONTOLOGY.hub_labels)
    assert not any(matched.values()) and fallbacks == []


def test_the_fallback_needs_a_unique_best_and_never_takes_a_matched_node() -> None:
    target = TargetGraph(
        nodes=(
            tnode("a", FR, "Export orders as CSV files", ["f1"]),
            tnode("b", FR, "Export orders as PDF files", ["f1"]),
            tnode("c", FR, "Import users from LDAP", ["f2"]),
        )
    )
    tied = Reference(nodes=({"label": FR, "name": "Orders export to files", "fragment": "f1"},))
    assert not any(reference_matches(target, tied).values())  # two equal candidates: no match
    taken = Reference(
        nodes=(
            {"label": FR, "name": "Import users from LDAP", "fragment": "f2"},
            {"label": FR, "name": "Users import from LDAP", "fragment": "f2"},  # c already matches item 0
        )
    )
    assert reference_matches(target, taken)["c"] == [0]
    other_label = Reference(nodes=({"label": CAP, "name": "Export orders as CSV file", "fragment": "f1"},))
    assert not any(reference_matches(target, other_label).values())


# Hub names and known terms (section 9)


def hub_plan() -> PlanArtifact:
    def frag(fid: str, path: Sequence[str], text: str) -> FragmentRec:
        return FragmentRec(
            fragment_id=fid,
            document_id="a.md",
            heading_path=tuple(path),
            ordinal=0,
            line_start=1,
            line_end=1,
            text=text,
            content_hash="h",
            tokens=1,
        )

    return PlanArtifact(
        mode="first",
        change_set=ChangeSet(new=("a.md",)),
        documents=(DocumentRec(document_id="a.md", revision_id="r", version=1, path="a.md", title="Billing Service"),),
        fragments=(
            frag("f1", ["Architecture", "Orders services"], "The order service calls the ledger store."),
            frag("f2", ["Architecture"], "Payments go through the ledger store and the Orders service."),
        ),
    )


HUB_REFERENCE = Reference(
    nodes=(
        {"label": MOD, "name": "Billing service", "fragment": "f1"},  # the document title
        {"label": MOD, "name": "Order service", "fragment": "f1"},  # a heading (plural folded)
        {"label": MOD, "name": "Ledger store", "fragment": "f1"},  # body only
        {"label": FR, "name": "Payments go through the ledger", "fragment": "f2"},  # not a hub
    )
)


def test_body_only_hubs_name_no_title_or_heading() -> None:
    subsets = hub_subsets(HUB_REFERENCE, hub_plan(), ONTOLOGY.hub_labels)
    assert (subsets.body_only, subsets.heading, subsets.every) == ((2,), (0, 1), (0, 1, 2))
    assert hub_subsets(HUB_REFERENCE, hub_plan(), ONTOLOGY.hub_labels) == subsets  # deterministic


def raw_node(raw_id: str, name: str, fragments: Sequence[str], unit: str = "u1", label: str = MOD) -> RawNode:
    return RawNode(
        raw_id=raw_id,
        name=name,
        label=label,
        unit_id=unit,
        call_id="c",
        window=(0, 1),
        fragment_ids=tuple(fragments),
    )


def test_hub_name_section_recall_catalog_recall_multi_matches_and_unsupported_uses(tmp_path: Path) -> None:
    candidate_graph = TargetGraph(
        nodes=(
            tnode("l1", MOD, "Ledger store", ["f1"]),
            tnode("l2", MOD, "ledger store", ["f2"]),  # a second node for the same reference hub
            tnode("o1", MOD, "Order service", ["f1"]),
        )
    )
    nodes = NodesArtifact(
        raw_nodes=(
            raw_node("r1", "Order service", ["f1"]),  # nearby, named in f1: supported
            raw_node("r2", "Billing service", ["f2"]),  # nearby, f2 does not name it: unsupported
            raw_node("s1", "Billing service must send invoices", ["f1"], label=FR),  # statement, unsupported
            raw_node("s2", "Orders service must keep payments", ["f2"], label=FR),  # statement, supported
        ),
        hub_catalog=(
            HubName(name="Ledger", label=MOD, aliases=("Ledger store",)),
            HubName(name="Orders service", label=MOD),
        ),
        hub_known_terms={"u1": KnownTermsShown(nearby=("Orders service", "Billing service"))},
        known_terms={"u1": KnownTermsShown(nearby=("Orders service", "Billing service"))},
    )
    builds = {
        "baseline": stored("baseline", BASELINE, plan=hub_plan()),
        "candidate": stored("candidate", candidate_graph, plan=hub_plan(), nodes_artifact=nodes),
    }
    report, sample = build_report(builds, dict.fromkeys(builds, ONTOLOGY), reference=HUB_REFERENCE)
    section = report["9_hub_names"]
    assert section["body_only_hubs"] == [{"label": MOD, "name": "Ledger store"}]
    assert section["heading_hubs"] == 2
    candidate = section["recall"]["candidate"]
    assert candidate["body_only"] == {"reference": 1, "found": 1, "recall": 1.0}
    assert candidate["heading"] == {"reference": 2, "found": 1, "recall": 0.5}
    assert (candidate["multi_matched"], candidate["multi_matched_hubs"]) == (1, [f"{MOD}: Ledger store (2)"])
    assert section["recall"]["baseline"]["all"]["found"] == 0
    # catalog recall: by name or alias; n/a for a build without a catalog
    assert section["catalog_recall"]["baseline"] is None
    assert section["catalog_recall"]["candidate"]["body_only"]["found"] == 1  # the alias
    assert section["catalog_recall"]["candidate"]["heading"]["found"] == 1  # Orders service ~ Order service
    rows = section["nearby_unsupported"]["rows"]
    assert sorted((row["pass"], row["name"], row["term"], row["fragments"]) for row in rows) == [
        ("hubs", "Billing service", "Billing service", "f2"),
        ("statements", "Billing service must send invoices", "Billing service", "f1"),
    ]
    assert [row["row_id"] for row in rows] == ["1", "2"]
    markdown = render_markdown(report)
    assert "Body-only subset: 1 of 3 reference hubs" in markdown
    assert "| extraction body_only | 0 (0/1) | 1 (1/1) |" in markdown
    assert "| catalog body_only | n/a | 1 (1/1) |" in markdown
    assert "2 rows in `nearby-unsupported.csv`" in markdown
    write_report(report, sample, tmp_path)
    assert (tmp_path / "body-only-hubs.txt").read_text().splitlines()[2:] == [f"{MOD}\tLedger store"]
    csv_lines = (tmp_path / "nearby-unsupported.csv").read_text().splitlines()
    assert csv_lines[0] == "row_id,pass,unit_id,label,name,term,fragments,judgement,note"
    assert len(csv_lines) == 3


async def test_naming_metrics_of_an_older_build_give_before_values_and_n_a_for_the_catalog(tmp_path: Path) -> None:
    raw = {"unit_id": "u1", "call_id": "c1", "window": [0, 1], "fragment_ids": ["f3"]}
    artifacts = {
        "conform": CANDIDATE.model_dump(mode="json") | {"quarantine": []},
        # no hub_catalog, known_terms or catalog counters
        "extract_nodes": {
            "raw_nodes": [
                raw | {"raw_id": "r1", "name": "Auth service", "label": MOD},
                raw | {"raw_id": "r2", "name": "Auth svc", "label": MOD},
            ],
            "known_terms_source": "hub_catalog",
            "counters": {"label_collapses": 0, "known_terms": 2},
        },
        "resolve": {
            "canonical": [
                {"cid": "c1", "identity_key": "k", "name": "Auth service", "label": MOD, "member_ids": ["r1", "r2"]}
            ],
            "counters": {"consolidation": {"merges": 1}},
        },
    }
    build_id = "e" * 32
    stores = open_file_stores(tmp_path)
    for step, payload in artifacts.items():
        await stores.builds.put_artifact("p", build_id, step, payload)
    build = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", build_id), "candidate", ONTOLOGY)
    builds = {"baseline": stored("baseline", BASELINE), "candidate": build}
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY), recompute=True)
    rows = {row["name"]: row["values"] for row in report["4_metrics"]}
    assert rows["hub_surfaces_per_hub.all.mean"] == {"baseline": None, "candidate": 2.0}
    assert rows[f"hub_raw_names_per_hub.{MOD}.share_over_1"] == {"baseline": None, "candidate": 1.0}
    assert rows["hub_consolidation_merges"] == {"baseline": None, "candidate": 1}
    for name in (
        "catalog_hubs",
        "catalog_calls",
        "known_terms_mentioned",
        "nearby_term_uses",
        "nearby_term_uses_hubs",
        "nearby_term_uses_statements",
        "alias_name_uses",
    ):
        assert rows[name] == {"baseline": None, "candidate": None}


# Section 10: identity samples


def test_identity_samples_list_merges_near_pairs_dedup_merges_and_refused_pairs(tmp_path: Path) -> None:
    hub = CanonicalNode(
        cid="c1",
        identity_key="Module|surface|orders",
        identity_aliases=("Module|surface|order service",),
        name="Orders",
        label=MOD,
        member_ids=("r1", "r2"),
        fragment_ids=("f1", "f2"),
    )
    lone = CanonicalNode(cid="c2", identity_key="Module|surface|billing", name="Billing", label=MOD, member_ids=("r3",))
    pay = CanonicalNode(
        cid="c3", identity_key="Module|surface|payments", name="Payments", label=MOD, member_ids=("r4",)
    )
    candidates = [
        PairCandidate(
            key_a="Module|surface|orders",
            key_b="Module|surface|order service",
            part="",
            name_cos=0.9,
            source="vector",
            outcome="same",
        ),
        *(
            PairCandidate(key_a=a, key_b=b, part="", name_cos=cos, rich_cos=rich, source="vector", outcome="different")
            for a, b, cos, rich in (
                ("Module|surface|billing", "Module|surface|orders", 0.7, 0.8),
                ("Module|surface|billing", "Module|surface|payments", 0.75, None),
                ("Module|surface|orders", "Module|surface|payments", 0.6, 0.65),
            )
        ),
    ]
    resolved = ResolveArtifact.model_validate_json(
        ResolveArtifact(
            canonical=(hub, lone, pay),
            merges=(
                HubMerge(
                    anchor_key="Module|surface|orders",
                    member_key="Module|surface|order service",
                    source="block",
                    confidence="high",
                ),
            ),
            pair_candidates=tuple(candidates),
        ).model_dump_json()
    )
    survivor = tnode("FunctionalRequirement|span|export csv", FR, "Export CSV", ["f5"]).model_copy(
        update={"identity_aliases": ("FunctionalRequirement|span|download csv",)}
    )
    other = tnode("FunctionalRequirement|span|export in 5 s", FR, "Export in 5 s", ["f6"])
    implied = TargetEdge(
        edge_key=f"{survivor.identity_key}|PART_OF|Module|surface|orders",
        relation="PART_OF",
        source_key=survivor.identity_key,
        target_key="Module|surface|orders",
        basis="implied",
        fragment_ids=("f5",),
    )
    target = TargetGraph.model_validate_json(
        TargetGraph(
            nodes=(
                survivor,
                other,
                tnode("Module|surface|orders", MOD, "Orders", ["f1"]).model_copy(
                    update={"identity_aliases": ("Module|surface|order service",)}
                ),
            ),
            edges=(implied,),
            derive_fallback_edges=(implied.edge_key,),
            dedup_refused=(
                RefusedDedupPair(
                    first_key=other.identity_key,
                    second_key=survivor.identity_key,
                    label=FR,
                    guard="numbers",
                    name_cosine=0.88,
                ),
            ),
        ).model_dump_json()
    )
    samples = report_tool.identity_samples(stored("candidate", target, resolve_artifact=resolved), ONTOLOGY)
    (merged,) = samples["merged_pairs"]["rows"]
    assert (merged["row_id"], merged["source"], merged["confidence"], merged["node"]) == (
        "M001",
        "block",
        "high",
        "Orders",
    )
    assert merged["member"] == "Module|surface|order service" and merged["fragments"] == "f1 f2"
    near = samples["near_unmerged_pairs"]["rows"]
    # the merged pair is left out; the closest by candidate (else name) cosine first
    assert [(row["first_node"], row["second_node"], row["sample"]) for row in near] == [
        ("Module: Billing", "Module: Orders", "top"),
        ("Module: Billing", "Module: Payments", "top"),
        ("Module: Orders", "Module: Payments", "top"),
    ]
    assert (near[0]["rich_cos"], near[1]["rich_cos"]) == ("0.800", "")
    assert len(report_tool.near_unmerged_rows(resolved, top=1, spread=1) or []) == 2
    (dedup,) = samples["dedup_merges"]["rows"]  # the hub alias is no dedup merge
    assert (dedup["node"], dedup["merged"]) == ("Export CSV", "FunctionalRequirement|span|download csv")
    (refused,) = samples["refused_pairs"]["rows"]
    assert (refused["guard"], refused["first"], refused["second"]) == ("numbers", "Export in 5 s", "Export CSV")
    (fallback,) = samples["derive_fallback_edges"]["rows"]
    assert (fallback["row_id"], fallback["relation"], fallback["source"], fallback["target"]) == (
        "F001",
        "PART_OF",
        "Export CSV",
        "Orders",
    )
    assert (fallback["target_label"], fallback["fragments"]) == (MOD, "f5")

    builds = {
        "baseline": stored("baseline", BASELINE),
        "candidate": stored("candidate", target, resolve_artifact=resolved),
    }
    report, _ = build_report(builds, dict.fromkeys(builds, ONTOLOGY))
    assert report["10_identity_samples"] == samples
    write_report(report, None, tmp_path)
    assert "- near unmerged pairs: 3 rows in `near-unmerged-pairs.csv`" in render_markdown(report)
    header = (tmp_path / "near-unmerged-pairs.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("row_id,sample,part,source,outcome") and header.endswith(",judgement,note")
    assert len((tmp_path / "refused-pairs.csv").read_text(encoding="utf-8").splitlines()) == 2
    assert len((tmp_path / "derive-fallback-edges.csv").read_text(encoding="utf-8").splitlines()) == 2


# Section 11: edge samples

SHIPPED = shipped_ontology()


def doc_fragment(fid: str, document: str) -> FragmentRec:
    return FragmentRec(
        fragment_id=fid,
        document_id=document,
        heading_path=(),
        ordinal=0,
        line_start=1,
        line_end=2,
        text="x",
        content_hash=fid,
        tokens=1,
    )


def edge_sample_build() -> StoredBuild:
    """A candidate over the shipped ontology with documents a.md (fa) and b.md (fb): a requirement owned
    across documents, an interface provided in its own, a near raw edge, a judged cross-document item,
    an inferred owner edge and an open question dropped as answered elsewhere."""

    nodes = (
        tnode("r1", FR, "Export CSV", ["fa"]),
        tnode("r2", FR, "Import CSV", ["fb"]),
        tnode("feat", FEAT, "Orders", ["fb"]),
        tnode("api", IFC, "Orders API", ["fa"]),
        tnode("mod", MOD, "Order service", ["fa"]),
    )
    edges = (
        tedge("PART_OF", "r1", "feat", ["fa"]),
        tedge("PROVIDED_BY", "api", "mod", ["fa"]),
        tedge("RELATES_TO", "r1", "mod", ["fa"]),
    )
    target = TargetGraph(
        nodes=nodes,
        edges=edges,
        quarantine=(),
        counters=TargetCounters(oq_answer_judged=2, oq_partly_kept=1, oq_answered_dropped=1),
        answered_elsewhere=(AnsweredElsewhere(key="q1", fragment="fb", quote="Exports are CSV."),),
    )
    canonical = tuple(
        CanonicalNode(
            cid=f"c{index}", identity_key=key, name=name, label=label, member_ids=(f"m{index}",), fragment_ids=(fid,)
        )
        for index, (key, name, label, fid) in enumerate(
            (
                ("r1", "Export CSV", FR, "fa"),
                ("mod", "Order service", MOD, "fa"),
                ("q1", "Which format?", "OpenQuestion", "fb"),
            )
        )
    )
    raw_edges = (
        RawEdge(
            relation="RELATES_TO", source_cid="c0", target_cid="c1", fact="uses", fragment_ids=("fa",), unit_id="u1"
        ),
        RawEdge(relation="PART_OF", source_cid="c0", target_cid="c1", fragment_ids=("fa",), unit_id="u1"),
        RawEdge(relation="RELATES_TO", source_cid="c0", target_cid="c1", fragment_ids=("fa",), unit_id="u2"),
    )
    extracted = EdgesArtifact(
        raw_edges=raw_edges,
        catalog_parts={"u1": CatalogParts(near=("c1",)), "u2": CatalogParts(mentioned=("c1",))},
    )
    item = JudgedItem(
        rule="INF-XDOC-01",
        kind="link",
        subject="MOD::ORDERS",
        source_spec_id="FR::EXPORT",
        relation="RELATES_TO",
        target_spec_id="FR::IMPORT",
        outcome="accepted",
        confidence=0.9,
        fragment_ids=("fa", "fb"),
    )
    plan_artifact = PlanArtifact(
        mode="first",
        change_set=ChangeSet(),
        documents=(),
        fragments=(doc_fragment("fa", "a.md"), doc_fragment("fb", "b.md")),
    )
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    result = BuildResult(
        project_id="p",
        build_id="candidate-1",
        mode="first",
        status="succeeded",
        started_at=stamp,
        finished_at=stamp,
        spec_changes=SpecChanges(inferred_added=("FR::IMPORT|PART_OF|FEAT::ORDERS",)),
    )
    spec_ids = {"r1": "FR::EXPORT", "r2": "FR::IMPORT", "feat": "FEAT::ORDERS", "mod": "MOD::ORDERS", "api": "IFC::API"}
    return stored(
        "candidate",
        target,
        result=result,
        plan=plan_artifact,
        spec_ids=spec_ids,
        resolve_artifact=ResolveArtifact(canonical=canonical),
        edges_artifact=extracted,
        inference_artifact=InferenceReport(judged_items=(item,)),
    )


def test_edge_samples_list_near_edges_judged_items_answered_drops_and_owner_and_provider_edges(
    tmp_path: Path,
) -> None:
    build = edge_sample_build()
    samples = report_tool.edge_samples(build, SHIPPED)
    near = samples["near_edges"]["rows"]
    # only unit u1 took the module by nearness; the RELATES_TO edge is kept, the PART_OF one is not
    assert sorted((row["relation"], row["near_end"], row["kept"]) for row in near) == [
        ("PART_OF", "target", "no"),
        ("RELATES_TO", "target", "yes"),
    ]
    (item,) = samples["xdoc_items"]["rows"]
    assert (item["row_id"], item["source"], item["target"], item["subject"]) == (
        "X001",
        "Export CSV",
        "Import CSV",
        "Order service",
    )
    assert (item["outcome"], item["confidence"], item["fragments"]) == ("accepted", "0.90", "fa fb")
    (answered,) = samples["answered_drops"]["rows"]
    assert (answered["question"], answered["fragment"], answered["quote"]) == (
        "Which format?",
        "fb",
        "Exports are CSV.",
    )
    owners = samples["owner_provider_edges"]["rows"]
    assert [(row["kind"], row["relation"], row["basis"], row["source"], row["cross_document"]) for row in owners] == [
        ("owner", "PART_OF", "inferred", "Import CSV", "no"),
        ("owner", "PART_OF", "stated", "Export CSV", "yes"),
        ("provider", "PROVIDED_BY", "stated", "Orders API", "no"),
    ]
    assert len(report_tool.owner_provider_rows(build, SHIPPED, per_group=0)) == 0

    builds = {"baseline": stored("baseline", BASELINE), "candidate": build}
    ontologies = {"baseline": ONTOLOGY, "candidate": SHIPPED}
    report, _ = build_report(builds, ontologies)
    assert report["11_edge_samples"] == samples
    rows = {row["name"]: row["values"]["candidate"] for row in report["4_metrics"]}
    assert rows["owners_by_source.inferred"] == 1 and rows["owners_by_source.stated"] == 1
    assert rows["cross_document_edges.PART_OF.stated"] == 1
    assert (rows["oq_answer_judged"], rows["oq_answered_dropped"]) == (2, 1)
    write_report(report, None, tmp_path)
    markdown = render_markdown(report)
    assert "- xdoc items: 1 rows in `xdoc-items.csv`" in markdown
    header = (tmp_path / "near-edges.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("row_id,unit,relation,near_end") and header.endswith(",kept,judgement,note")
    assert len((tmp_path / "owner-provider-edges.csv").read_text(encoding="utf-8").splitlines()) == 4


def test_xdoc_items_sample_the_accepted_and_downgraded_items_at_most_forty() -> None:
    build = edge_sample_build()
    (item,) = build.inference_artifact.judged_items if build.inference_artifact else ()
    items = [
        item.model_copy(update={"target_spec_id": f"FR::{index:03d}", "outcome": outcome})
        for index, outcome in enumerate(["accepted"] * 30 + ["downgraded"] * 15 + ["below_floor", "invalid"] * 5)
    ]
    sampled = replace(build, inference_artifact=InferenceReport(judged_items=tuple(items)))
    rows = report_tool.xdoc_item_rows(sampled) or []
    assert len(rows) == report_tool.XDOC_ITEM_SAMPLE == 40
    assert {row["outcome"] for row in rows} == {"accepted", "downgraded"}
    assert len(report_tool.xdoc_item_rows(sampled, limit=100) or []) == 45
    orphan = item.model_copy(update={"rule": "INF-CONT-01", "kind": "owner"})
    mixed = replace(build, inference_artifact=InferenceReport(judged_items=(item, orphan)))
    assert len(report_tool.xdoc_item_rows(mixed) or []) == 2  # without the ontology: every rule's
    assert [row["rule"] for row in report_tool.xdoc_item_rows(mixed, ontology=SHIPPED) or []] == ["INF-XDOC-01"]


def test_inference_samples_give_accepted_items_per_rule_and_band_and_change_report_nodes_per_class(
    tmp_path: Path,
) -> None:
    build = edge_sample_build()
    (item,) = build.inference_artifact.judged_items if build.inference_artifact else ()
    items = [
        item.model_copy(update={"target_spec_id": f"FR::{index:03d}", "confidence": confidence, "outcome": outcome})
        for index, (confidence, outcome) in enumerate(
            [(0.9, "accepted")] * 45 + [(0.75, "downgraded")] * 3 + [(0.65, "below_floor")] * 4 + [(None, "declined")]
        )
    ]
    nodes = (
        ChangedNode(spec_id="FR::EXPORT", label=FR, classes=("changed_meaning", "changed_links"), causes=("name",)),
        ChangedNode(spec_id="FR::GONE", label=FR, classes=("removed",)),
        *(ChangedNode(spec_id=f"FR::N{index:02d}", label=FR, classes=("added",)) for index in range(35)),
    )
    assert build.result is not None
    result = build.result.model_copy(
        update={"change_report": ChangeReport(nodes=nodes, counts={"added": 35, "removed": 1})}
    )
    sampled = replace(build, inference_artifact=InferenceReport(judged_items=tuple(items)), result=result)
    rows = report_tool.inferred_edge_rows(sampled) or []
    bands = [(row["rule"], row["band"]) for row in rows]
    # 40 of 45 in the top band, 3 downgraded, none of the refused; the added edge no item names: n/a
    assert bands == [("INF-XDOC-01", "[70,80)")] * 3 + [("INF-XDOC-01", "[80,100]")] * 40 + [("n/a", "n/a")]
    assert rows[-1]["relation"] == "PART_OF" and rows[-1]["source"] == "Import CSV"
    assert [row["row_id"] for row in rows[:2]] == ["I001", "I002"]
    changed = report_tool.change_report_rows(sampled) or []
    assert [row["class"] for row in changed] == ["added"] * 30 + ["removed", "changed_meaning"]
    assert changed[-1]["name"] == "Export CSV" and changed[-1]["classes"] == "changed_meaning changed_links"
    assert changed[-2]["name"] == ""
    assert report_tool.change_report_rows(build) is None

    builds = {"baseline": stored("baseline", BASELINE), "candidate": sampled}
    evals = {
        "baseline": {
            "estimate_vs_actual": {"not_estimated:judge_attach_orphan": {"estimate": None, "actual": [5, 50]}}
        },
        "candidate": {
            "estimate_vs_actual": {
                "inference": {"estimate": [4, 100], "actual": [5, 120]},
                "cross_document_inference": {"estimate": [2, 50], "actual": [0, 0]},
            }
        },
    }
    report, _ = build_report(builds, {"baseline": SHIPPED, "candidate": SHIPPED}, evals=evals)
    versus = report["12_inference"]["estimate_vs_actual"]
    assert versus["baseline"]["inference"] == {"estimate": None, "actual": [5, 50], "ratio": None}
    assert versus["baseline"]["cross_document_inference"]["actual"] is None
    assert versus["candidate"]["inference"] == {"estimate": [4, 100], "actual": [5, 120], "ratio": 1.2}
    assert versus["candidate"]["cross_document_inference"]["ratio"] == 0
    write_report(report, None, tmp_path)
    header = (tmp_path / "inferred-edges.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("row_id,rule,band,kind") and header.endswith(",fragments,judgement,note")
    assert len((tmp_path / "change-report-nodes.csv").read_text(encoding="utf-8").splitlines()) == 33
    markdown = render_markdown(report)
    assert "- inferred edges: 44 rows in `inferred-edges.csv`" in markdown
    assert "| candidate | inference | 4 / 100 | 5 / 120 | 1.2 |" in markdown


async def test_a_build_reads_its_diff_inference_and_embedding_vectors_offline(tmp_path: Path) -> None:
    model = "test/embedding"
    first, second = (
        tnode("r1", FR, "Export CSV", ["f1"]).model_copy(
            update={"embedding_text": "Export CSV", "name_embedding_text": "FunctionalRequirement: Export CSV"}
        ),
        tnode("m1", MOD, "Exports", ["f2"]).model_copy(update={"embedding_text": "Exports"}),
    )
    target = TargetGraph(nodes=(first, second, tnode("x", MOD, "No vector", ["f3"])), quarantine=())
    build_id = "d" * 32
    stores = open_file_stores(tmp_path)
    await stores.builds.put_artifact("p", build_id, "conform", target.model_dump(mode="json"))
    create = NodeCreate(uuid="u", spec_id="FR::X", label=FR, properties={"embedding_model": model})
    await stores.builds.put_artifact("p", build_id, "diff", DiffArtifact(operations=(create,)).model_dump(mode="json"))
    await stores.builds.put_artifact("p", build_id, "infer", InferenceReport().model_dump(mode="json"))
    await stores.extraction.put(VECTOR_KIND, f"p|{model}|{text_hash('Export CSV')}", model, encode_vector(np.ones(3)))
    build = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", build_id), "candidate", SHIPPED)
    assert build.diff_artifact is not None and build.inference_artifact == InferenceReport()
    # the search embedding, not the name embedding: m1's vector is not stored, x has no text
    assert list(build.vectors or {}) == ["r1"]
    assert report_tool.embedding_model(build.diff_artifact) == model
    assert report_tool.embedding_model(None) is None


async def test_a_result_without_a_change_report_is_classified_offline(tmp_path: Path) -> None:
    # An older update: its diff has no edge_changes; the ends of its edge delete come from the registry.
    rows = [
        SpecIdRow(spec_id="REQ::FR::EXPORT", identity_key="k:fr", uuid="u-fr", state="active"),
        SpecIdRow(spec_id="FEAT::GONE", identity_key="k:feat", uuid="u-feat", state="retired"),
    ]
    operations = (
        EdgeDelete(relation="PART_OF", edge_key="k:fr|PART_OF|k:feat", uuid="e1"),
        NodeDelete(uuid="u-feat", spec_id="FEAT::GONE"),
    )
    older = DiffArtifact(operations=operations, spec_changes=SpecChanges(removed=("FEAT::GONE",)))
    assert older.edge_changes is None
    result = BuildResult(
        project_id="p", build_id="b" * 32, mode="update", status="succeeded", started_at=T0, finished_at=T0
    )
    registry = {row.spec_id: row for row in rows}

    classified = report_tool.offline_change_report(result, older, None, None, ontology=SHIPPED, rows=registry)
    assert classified is not None and classified.change_report is not None
    assert [(node.spec_id, node.label, node.classes) for node in classified.change_report.nodes] == [
        ("FEAT::GONE", "Feature", ("removed",)),
        ("REQ::FR::EXPORT", "FunctionalRequirement", ("changed_meaning",)),  # lost its owner Feature
    ]
    assert [edge.ref for edge in classified.change_report.edges] == ["REQ::FR::EXPORT|PART_OF|FEAT::GONE"]
    # A recorded report, a failed build or no diff: the result as stored.
    recorded = classified.model_copy(update={"change_report": ChangeReport()})
    assert report_tool.offline_change_report(recorded, older, None, None, ontology=SHIPPED, rows=registry) is recorded
    failed = result.model_copy(update={"status": "failed"})
    assert report_tool.offline_change_report(failed, older, None, None, ontology=SHIPPED, rows=registry) is failed
    assert report_tool.offline_change_report(result, None, None, None, ontology=SHIPPED, rows=registry) is result

    stores = open_file_stores(tmp_path)
    await stores.builds.put_artifact("p", result.build_id, "conform", TargetGraph().model_dump(mode="json"))
    await stores.builds.put_artifact("p", result.build_id, "diff", older.model_dump(mode="json"))
    await stores.registry.record("p", result.build_id, rows)
    record = BuildRecord(
        build_id=result.build_id,
        project_id="p",
        mode="update",
        options=BuildOptions(),
        ontology_fingerprint="f",
        label_set=SHIPPED.labels,
        embedding_model="m",
        started_at=T0,
    )
    await stores.builds.create_build(record)
    await stores.builds.finish_build(result)
    build = await report_tool.load_build(report_tool.BuildSpec(tmp_path, "p", result.build_id), "candidate", SHIPPED)
    assert build.change_report_offline and build.result == classified
    builds = {"baseline": stored("baseline", BASELINE), "candidate": build}
    report, _ = build_report(builds, {"baseline": SHIPPED, "candidate": SHIPPED})
    assert report["12_inference"]["change_report_offline"] is True
    assert "classified offline" in render_markdown(report)
    assert [row["class"] for row in report_tool.change_report_rows(build) or []] == ["removed", "changed_meaning"]
