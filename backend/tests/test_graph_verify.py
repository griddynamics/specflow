"""Verification offline: checks, problem identifiers, repair planning, the valve, the loop bound,
metrics, the shared spine walk and the example's retry options (plan 4.T2;
analysis.md "Verification"; m4-spec-edits G5-G10; FR-VER-01..04).

Graph states are hand-built ``VNode``/``VEdge``/``VFragment`` values (``graph.build.verify``) over the
shipped ontology; the loop runs on in-memory executors that answer the three verify reads, so no
test reaches Neo4j. The live tier is ``test_graph_verify_live.py``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import graph_build
from graph.build import open_file_stores
from graph.build import verify as verification
from graph.build.models import (
    DiffArtifact,
    EdgeCreate,
    EvidenceLines,
    NodeLinks,
    SpecChanges,
    TargetCounters,
    TargetEdge,
    TargetGraph,
    TargetNode,
    VerificationReport,
)
from graph.build.queries import VERIFY_DOMAIN_EDGES, VERIFY_FRAGMENTS, VERIFY_SPEC_NODES
from graph.build.run import _with_repairs
from graph.build.spine import SpineEdge, SpineGraph, SpineNode
from graph.build.verify import (
    MAX_REPAIR_ROUNDS,
    VALVE_FRACTION,
    VALVE_MIN,
    Evidence,
    GraphState,
    OwnedNames,
    RepairScope,
    RoundPlan,
    SpecIds,
    VEdge,
    VFragment,
    VNode,
    deleted_edges,
    dropped_by_relation,
    edge_subject,
    find_problems,
    measure,
    plan_round,
    valve,
    verify_graph,
)
from graph.core.identity import SpecIdRow
from graph_support import OfflineExecutor, shipped_ontology

ONTOLOGY = shipped_ontology()
NAMES = OwnedNames.of(ONTOLOGY)
PROJECT = "p-offline"

FR, FEAT, MOD, CAP = "FunctionalRequirement", "Feature", "Module", "Capability"
ACTIVE = VFragment("f1", "d1.md", "active", "h1", "h1", grounds=True)
RETIRED = VFragment("f-old", "d1.md", "retired", "h0", "h0", grounds=True)
SKIPPED = VFragment("f-skip", "d2.md", "skipped", "h2", None, grounds=True)


DEFAULT: Any = object()


def node(
    element: str,
    label: str | tuple[str, ...] | None,
    spec_id: Any = DEFAULT,
    *,
    evidence: tuple[str, ...] = ("f1",),
    uuid: Any = None,
    name: str | None = None,
    key: str | None = None,
    **extra: Any,
) -> VNode:
    labels = () if label is None else (label,) if isinstance(label, str) else tuple(sorted(label))
    if spec_id is DEFAULT:
        spec_id = f"{ONTOLOGY.id_prefix(label)}{element.upper()}" if isinstance(label, str) else None
    return VNode(
        id=element,
        uuid=uuid if uuid is not None else f"u-{element}",
        spec_id=spec_id,
        name=name or element,
        identity_key=key or f"{labels[0] if labels else 'X'}|name|{element}",
        labels=labels,
        evidence=evidence,
        **extra,
    )


def edge(
    element: str,
    relation: str,
    source: str,
    target: str,
    *,
    basis: Any = "stated",
    fragments: Any = ("f1",),
    **extra: Any,
) -> VEdge:
    return VEdge(
        id=element,
        relation=relation,
        source=source,
        target=target,
        uuid=f"u-{element}",
        edge_key=f"key|{element}",
        basis=basis,
        fragment_ids=list(fragments) if fragments is not None else None,
        **extra,
    )


def inferred(element: str, relation: str, source: str, target: str, **properties: Any) -> VEdge:
    values = {"inference_rule": "INF-CONT-01", "confidence": 0.9, "evidence": '[{"fragment_id": "f1", "quote": "q"}]'}
    values.update(properties)
    return edge(element, relation, source, target, basis="inferred", fragments=None, **values)


def state(
    nodes: Sequence[VNode], edges: Sequence[VEdge] = (), fragments: Sequence[VFragment] = (ACTIVE,)
) -> GraphState:
    return GraphState(nodes={item.id: item for item in nodes}, edges=tuple(edges), fragments=tuple(fragments))


def problems(graph: GraphState) -> list[tuple[str, str, str | None]]:
    return [(f.problem.check, f.problem.subject, f.problem.code) for f in find_problems(graph, ONTOLOGY, NAMES)]


def plan(graph: GraphState, evidence: Evidence | None = None, spec_ids: SpecIds | None = None) -> RoundPlan:
    findings = find_problems(graph, ONTOLOGY, NAMES)
    return plan_round(findings, graph, ONTOLOGY, NAMES, evidence or Evidence(), spec_ids=spec_ids, round_=1)


def repairs(round_plan: RoundPlan) -> list[tuple[str, str, str, str | None]]:
    return [(r.check, r.action, r.subject, r.value) for r in round_plan.repairs]


def nodes_of(count: int, label: str = FR) -> list[VNode]:
    return [node(f"n{index:03d}", label) for index in range(count)]


# Constants (G5: the valve is max(1, 5%); at most 2 rounds)


def test_valve_and_round_constants() -> None:
    assert (VALVE_FRACTION, VALVE_MIN, MAX_REPAIR_ROUNDS) == (0.05, 1, 2)


# Valve arithmetic (FR-VER-02, G5)


@pytest.mark.parametrize(
    ("total", "deletes", "blocked"),
    [
        (1, 1, False),  # a single bad item can always be repaired
        (10, 1, False),
        (10, 2, True),
        (15, 1, False),  # the "15-node project": max(1, 0.75) = 1
        (15, 2, True),
        (20, 1, False),  # 5% of 20 is exactly 1
        (20, 2, True),
        (39, 1, False),
        (39, 2, True),  # 1.95
        (40, 2, False),
        (40, 3, True),
        (100, 5, False),
        (100, 6, True),
    ],
)
def test_valve_limits_node_deletes_to_max_one_or_five_percent(total: int, deletes: int, *, blocked: bool) -> None:
    graph = state(nodes_of(total))
    round_plan = RoundPlan(delete_nodes={item.id: item for item in list(graph.nodes.values())[:deletes]})
    over = valve(round_plan, graph)
    if blocked:
        assert over == {"nodes": deletes, "edges": 0, "spec_nodes": total, "domain_edges": 0}
    else:
        assert over is None


@pytest.mark.parametrize(
    ("total", "deletes", "blocked"), [(11, 1, False), (11, 2, True), (60, 3, False), (60, 4, True)]
)
def test_valve_limits_edge_deletes_to_max_one_or_five_percent(total: int, deletes: int, *, blocked: bool) -> None:
    modules = [node("m", MOD)]
    requirements = nodes_of(total)
    edges = [edge(f"e{index:03d}", "PART_OF", item.id, "m") for index, item in enumerate(requirements)]
    graph = state([*modules, *requirements], edges)
    round_plan = RoundPlan(delete_edges={item.id: item for item in edges[:deletes]})
    over = valve(round_plan, graph)
    assert (over is not None) == blocked
    if blocked:
        assert over == {"nodes": 0, "edges": deletes, "spec_nodes": total + 1, "domain_edges": total}


def test_valve_counts_the_edges_deleted_with_their_node() -> None:
    # 30 nodes, 30 edges: one node may go (1 <= 1.5) but it takes 2 edges with it (2 > 1.5).
    requirements = nodes_of(29)
    hub = node("hub", MOD)
    edges = [edge(f"e{index:03d}", "PART_OF", item.id, "hub") for index, item in enumerate(requirements[:2])]
    edges += [
        edge(f"x{index:03d}", "RELATES_TO", item.id, requirements[0].id)
        for index, item in enumerate(requirements[2:30])
    ]
    graph = state([hub, *requirements], edges)
    assert (len(graph.nodes), len(graph.edges)) == (30, 29)
    round_plan = RoundPlan(delete_nodes={"hub": hub})
    assert set(deleted_edges(round_plan, graph)) == {"e000", "e001"}
    assert valve(round_plan, graph) == {"nodes": 1, "edges": 2, "spec_nodes": 30, "domain_edges": 29}
    # An edge deleted explicitly and with its node counts once.
    round_plan.delete_edges["e000"] = edges[0]
    assert len(deleted_edges(round_plan, graph)) == 2
    # One edge with the node: within the limit.
    alone = state([hub, *requirements], [edges[0], *edges[2:]])
    assert valve(RoundPlan(delete_nodes={"hub": hub}), alone) is None


# Problem identifiers and checks (analysis.md "Verification" table; G9, G10)


def test_edge_subject_is_spec_id_form_and_never_an_edge_key() -> None:
    graph = state(
        [
            node("a", FR, key="FunctionalRequirement|name|secret document words"),
            node("m", MOD),
            node("nospec", MOD, spec_id="", uuid="uuid-9"),
        ],
        [
            edge("e1", "PART_OF", "a", "m"),
            edge("e2", "PART_OF", "a", "elsewhere"),
            edge("e3", "PART_OF", "a", "nospec"),
        ],
    )
    subjects = [edge_subject(item, graph) for item in graph.edges]
    assert subjects == ["REQ::FR::A|PART_OF|MOD::M", "REQ::FR::A|PART_OF|elsewhere", "REQ::FR::A|PART_OF|uuid-9"]
    found = find_problems(graph, ONTOLOGY, NAMES)
    assert all("secret" not in item.problem.subject and "key|" not in item.problem.subject for item in found)


def test_node_checks_label_spec_id_and_grounding() -> None:
    graph = state(
        [
            node("ok", FR),
            node("nolabel", None, spec_id="REQ::FR::NOLABEL"),
            node("two", (FR, FEAT), spec_id="REQ::FR::TWO"),
            node("nolabel_bad", None, spec_id="ZZZ::STRAY"),
            node("missing", MOD, spec_id=None, uuid="uuid-missing"),
            node("wrongprefix", MOD, spec_id="FEAT::WRONG"),
            node("lower", MOD, spec_id="MOD::lower"),
            node("empty", MOD, spec_id="MOD::"),
            node("retired", FR, evidence=("f-old",)),
            node("skippedonly", FR, evidence=("f-skip",)),
            node("none", FR, evidence=()),
        ],
        fragments=(ACTIVE, RETIRED, SKIPPED),
    )
    # In subject order; a node without a label whose spec_id names one has no spec_id problem, a
    # skipped fragment grounds, a retired one does not.
    assert problems(graph) == [
        ("spec_id", "FEAT::WRONG", "malformed"),
        ("spec_id", "MOD::", "malformed"),
        ("spec_id", "MOD::lower", "malformed"),
        ("node_label", "REQ::FR::NOLABEL", "no_label"),
        ("ungrounded_node", "REQ::FR::NONE", None),
        ("ungrounded_node", "REQ::FR::RETIRED", None),
        ("node_label", "REQ::FR::TWO", "several_labels"),
        ("node_label", "ZZZ::STRAY", "no_label"),
        ("spec_id", "ZZZ::STRAY", "malformed"),
        ("spec_id", "uuid-missing", "missing"),
    ]


def test_edge_checks_illegal_ungrounded_and_inferred() -> None:
    nodes = [
        node("r", FR),
        node("r2", FR),
        node("m", MOD),
        node("m2", MOD),
        node("f", FEAT),
        node("c", CAP),
        node("two", (FR, FEAT), spec_id="REQ::FR::TWO"),
        node("stray", None, spec_id="ZZZ::STRAY"),
    ]
    edges = [
        edge("legal", "PART_OF", "r", "m"),
        edge("illegal_by_prefix", "PART_OF", "m", "two"),  # "two" has two labels; its spec_id names FR
        edge("unjudged", "PART_OF", "m", "stray"),  # an end whose spec_id names no label: not judged
        edge("illegal", "PART_OF", "m", "r"),
        edge("dangling", "PART_OF", "r", "not-a-spec-node"),
        edge("byprefix", "PART_OF", "two", "m"),  # judged by the label its spec_id names: legal
        edge("nobasis", "RELATES_TO", "r", "r2", basis=None, fragments=()),  # no basis: read as stated
        edge("nobasis_ok", "PART_OF", "f", "m", basis=None),
        edge("retiredonly", "GROUPED_BY", "f", "c", fragments=("f-old",)),
        edge("skippedonly", "RELATES_TO", "r", "two", basis="implied", fragments=("f-skip",)),
        edge("nofragments", "REALIZED_BY", "c", "m2", fragments=None),
        inferred("inf_ok", "PART_OF", "r", "f"),  # no source_fragment_ids needed
        inferred("inf_ok_list", "RELATES_TO", "m", "r", evidence=[{"fragment_id": "f1", "quote": "q"}]),
        inferred("no_rule", "RELATES_TO", "r2", "m", inference_rule=None),
        inferred("no_conf", "RELATES_TO", "r2", "r", confidence=None),
        inferred("bool_conf", "RELATES_TO", "two", "r2", confidence=True),
        inferred("str_conf", "RELATES_TO", "m", "two", confidence="0.9"),
        inferred("empty_ev", "REALIZED_BY", "c", "m", evidence="[]"),
        inferred("bad_json", "INTEGRATES_WITH", "m", "m2", evidence="not json"),
        inferred("empty_list", "INTEGRATES_WITH", "m2", "m", evidence=[]),
        inferred("bad_inferred", "PART_OF", "m", "f", inference_rule=""),  # illegal and incomplete
    ]
    found = problems(state(nodes, edges, (ACTIVE, RETIRED, SKIPPED)))
    node_checks = {"node_label", "spec_id", "ungrounded_node"}
    assert [item for item in found if item[0] not in node_checks] == [
        ("ungrounded_edge", "FEAT::F|GROUPED_BY|CAP::C", None),
        ("inferred_edge", "MOD::M2|INTEGRATES_WITH|MOD::M", "missing_evidence"),
        ("inferred_edge", "MOD::M|INTEGRATES_WITH|MOD::M2", "missing_evidence"),
        ("illegal_edge", "MOD::M|PART_OF|FEAT::F", "illegal_pair"),
        ("inferred_edge", "MOD::M|PART_OF|FEAT::F", "missing_rule"),
        ("illegal_edge", "MOD::M|PART_OF|REQ::FR::R", "illegal_pair"),
        ("illegal_edge", "MOD::M|PART_OF|REQ::FR::TWO", "illegal_pair"),
        ("illegal_edge", "REQ::FR::R|PART_OF|not-a-spec-node", "endpoint_not_spec_node"),
        ("inferred_edge", "CAP::C|REALIZED_BY|MOD::M", "missing_evidence"),
        ("ungrounded_edge", "CAP::C|REALIZED_BY|MOD::M2", None),
        ("inferred_edge", "MOD::M|RELATES_TO|REQ::FR::TWO", "missing_confidence"),
        ("inferred_edge", "REQ::FR::R2|RELATES_TO|MOD::M", "missing_rule"),
        ("inferred_edge", "REQ::FR::R2|RELATES_TO|REQ::FR::R", "missing_confidence"),
        ("ungrounded_edge", "REQ::FR::R|RELATES_TO|REQ::FR::R2", None),
        ("inferred_edge", "REQ::FR::TWO|RELATES_TO|REQ::FR::R2", "missing_confidence"),
    ]


def test_fragment_state_check() -> None:
    fragments = (
        ACTIVE,
        RETIRED,
        SKIPPED,  # skipped and never extracted: allowed
        VFragment("f-weird", "d3.md", "weird", "h", "h", grounds=True),
        VFragment("f-none", "d3.md", None, "h", "h", grounds=False),
        VFragment("f-never", "d4.md", "active", "h", None, grounds=False),
        VFragment("f-stale", "d4.md", "active", "new", "old", grounds=True),
    )
    assert problems(state([node("r", FR)], fragments=fragments)) == [
        ("fragment_state", "f-weird", "unknown_status"),
        ("fragment_state", "f-none", "unknown_status"),
        ("fragment_state", "f-never", "not_extracted"),
        ("fragment_state", "f-stale", "not_extracted"),
    ]


def test_problems_come_nodes_then_edges_then_fragments() -> None:
    graph = state(
        [node("r", FR, evidence=()), node("m", MOD)],
        [edge("bad", "PART_OF", "m", "r")],
        (ACTIVE, VFragment("f-never", "d4.md", "active", "h", None, grounds=True)),
    )
    assert [item[0] for item in problems(graph)] == ["ungrounded_node", "illegal_edge", "fragment_state"]


# Repair planning


def test_label_repair_keeps_the_prefix_label_and_sets_label_disagreement_as_diff_would() -> None:
    two = node("two", (FR, FEAT), spec_id="REQ::FR::TWO", key="k-two")
    bare = node("bare", None, spec_id="FEAT::BARE", key="k-bare", label_disagreement="FEAT")
    stray = node("stray", None, spec_id="ZZZ::STRAY")
    target = TargetGraph(
        nodes=(
            TargetNode(identity_key="k-two", label=FR, name="two", evidence=()),
            TargetNode(identity_key="k-other", identity_aliases=("k-bare",), label=FR, name="bare", evidence=()),
        )
    )
    graph = state([two, bare, stray], [edge("e", "PART_OF", "stray", "two")])
    round_plan = plan(graph, Evidence.of_build(target, None))
    assert round_plan.labels == [
        {"id": "bare", "label": FEAT, "remove": [], "label_disagreement": FR},  # target (by alias) says FR
        {"id": "two", "label": FR, "remove": [FEAT], "label_disagreement": None},
    ]
    # A node whose spec_id names no label is deleted; nothing else is done to it, its edge goes with it.
    assert set(round_plan.delete_nodes) == {"stray"} and round_plan.delete_edges == {}
    assert repairs(round_plan) == [
        ("node_label", "set_label", "FEAT::BARE", FEAT),
        ("node_label", "set_label", "REQ::FR::TWO", FR),
        ("node_label", "delete_node", "ZZZ::STRAY", None),
    ]


def test_label_disagreement_without_a_target_node_keeps_the_stored_value_unless_it_names_the_label() -> None:
    evidence = Evidence()
    assert evidence.label_disagreement(node("a", None, label_disagreement="Feature"), FR) == "Feature"
    assert evidence.label_disagreement(node("a", None, label_disagreement=FR), FR) is None
    assert evidence.label_disagreement(node("a", None, label_disagreement=""), FR) is None
    target = TargetGraph(
        nodes=(TargetNode(identity_key="X|name|a", label=FR, name="a", evidence=(), label_disagreement=MOD),)
    )
    assert Evidence.of_build(target, None).label_disagreement(node("a", None), FR) == MOD


def test_ungrounded_node_restores_evidence_from_diff_then_target_else_deletes() -> None:
    lines = {fid: EvidenceLines(fragment_id=fid, line_start=1, line_end=2) for fid in ("f1", "f-old", "f-skip")}
    diff = DiffArtifact(
        operations=(
            NodeLinks(uuid="u-bydiff", evidence=(lines["f-old"], lines["f1"])),
            NodeLinks(uuid="u-both", evidence=(lines["f1"],)),  # the diff's evidence wins over the target's
            NodeLinks(uuid="u-fallback", evidence=(lines["f-old"],)),  # nothing grounding left: the target's
        )
    )
    target = TargetGraph(
        nodes=(
            TargetNode(identity_key="k-both", label=FR, name="b", evidence=(lines["f-skip"],)),
            TargetNode(identity_key="k-fallback", label=FR, name="c", evidence=(lines["f-skip"],)),
            TargetNode(identity_key="k-key", label=FR, name="x", evidence=(lines["f-skip"],)),
            TargetNode(
                identity_key="k-main", identity_aliases=("k-alias",), label=FR, name="y", evidence=(lines["f1"],)
            ),
            TargetNode(identity_key="k-retired", label=FR, name="z", evidence=(lines["f-old"],)),
        )
    )
    graph = state(
        [
            node("bydiff", FR, evidence=()),
            node("both", FR, evidence=(), key="k-both"),
            node("fallback", FR, evidence=(), key="k-fallback"),
            node("bykey", FR, evidence=(), key="k-key"),
            node("byalias", FR, evidence=("f-old",), key="k-alias"),
            node("retired", FR, evidence=(), key="k-retired"),
            node("nothing", FR, evidence=()),
        ],
        fragments=(ACTIVE, RETIRED, SKIPPED),
    )
    round_plan = plan(graph, Evidence.of_build(target, diff))
    restored = {row["id"]: [line["fragment_id"] for line in row["evidence"]] for row in round_plan.node_evidence}
    # Only active and skipped fragments are restored; the retired line of the diff is left out.
    assert restored == {
        "byalias": ["f1"],
        "both": ["f1"],
        "bydiff": ["f1"],
        "bykey": ["f-skip"],
        "fallback": ["f-skip"],
    }
    assert set(round_plan.delete_nodes) == {"nothing", "retired"}


def test_edge_repairs_restore_or_delete() -> None:
    nodes = [node("r", FR), node("m", MOD), node("f", FEAT), node("gone", FR, evidence=())]
    edges = [
        edge("bywrite", "PART_OF", "r", "m", fragments=()),
        edge("bykey", "PART_OF", "f", "m", fragments=("f-old",)),
        edge("nothing", "RELATES_TO", "r", "m", basis=None, fragments=None),
        edge("illegal", "PART_OF", "m", "r"),
        inferred("inf", "RELATES_TO", "m", "r", confidence=None),
        edge("withnode", "RELATES_TO", "gone", "m"),  # goes with its deleted end, not listed itself
    ]
    diff = DiffArtifact(
        operations=(
            EdgeCreate(
                uuid="u-bywrite",
                relation="PART_OF",
                edge_key="key|bywrite",
                source_uuid="u-r",
                target_uuid="u-m",
                properties={"source_fragment_ids": ["f-skip"]},
            ),
        )
    )
    target = TargetGraph(
        edges=(
            TargetEdge(
                edge_key="key|bykey",
                relation="PART_OF",
                source_key="a",
                target_key="b",
                basis="stated",
                fragment_ids=("f-old", "f1", "f1"),
            ),
        )
    )
    evidence = Evidence.of_build(target, diff)
    round_plan = plan(state(nodes, edges, (ACTIVE, RETIRED, SKIPPED)), evidence)
    assert round_plan.edge_evidence == [
        {"id": "bykey", "fragment_ids": ["f1"]},
        {"id": "bywrite", "fragment_ids": ["f-skip"]},
    ]
    assert set(round_plan.delete_edges) == {"illegal", "inf", "nothing"}
    assert set(round_plan.delete_nodes) == {"gone"}
    assert set(deleted_edges(round_plan, state(nodes, edges))) == {"illegal", "inf", "nothing", "withnode"}
    actions = {(check, action) for check, action, _, _ in repairs(round_plan)}
    assert actions == {
        ("ungrounded_node", "delete_node"),
        ("illegal_edge", "delete_edge"),
        ("inferred_edge", "delete_edge"),
        ("ungrounded_edge", "restore_evidence"),
        ("ungrounded_edge", "delete_edge"),
    }
    assert all("key|" not in subject for _, _, subject, _ in repairs(round_plan))


def test_fragment_state_requests_a_follow_up_and_writes_nothing() -> None:
    graph = state([node("r", FR)], fragments=(ACTIVE, VFragment("f-never", "d4.md", "active", "h", None, grounds=True)))
    round_plan = plan(graph)
    assert round_plan.follow_ups == {"f-never": "d4.md"} and not round_plan.writes and round_plan.repairs == []


def test_spec_id_repair_reuses_the_issued_id_keeps_the_malformed_one_as_alias_else_allocates() -> None:
    victim = node("victim", FR, spec_id="bad id", uuid="u-v", key="k-v")
    missing = node("missing", MOD, spec_id=None, uuid="u-m", key="k-m", name="Payment Gateway")
    holder = node("holder", MOD, spec_id="MOD::PAYMENT_GATEWAY", uuid="u-h", key="k-h")
    taken = {
        "REQ::FR::VICTIM": SpecIdRow("REQ::FR::VICTIM", "k-v", "u-v", "active"),
        "MOD::PAYMENT_GATEWAY": SpecIdRow("MOD::PAYMENT_GATEWAY", "k-h", "u-h", "active"),
    }
    graph = state([victim, missing, holder])
    ids = SpecIds.of(taken, graph)
    round_plan = plan(graph, spec_ids=ids)
    assert round_plan.spec_ids == [
        {"id": "victim", "spec_id": "REQ::FR::VICTIM", "aliases": ["bad id"]},
        {"id": "missing", "spec_id": "MOD::PAYMENT_GATEWAY_2", "aliases": None},
    ]
    assert round_plan.registry_rows == [
        SpecIdRow("bad id", "k-v", "u-v", "alias"),
        SpecIdRow("REQ::FR::VICTIM", "k-v", "u-v", "active"),
        SpecIdRow("MOD::PAYMENT_GATEWAY_2", "k-m", "u-m", "active"),
    ]
    assert [(check, action, value) for check, action, _, value in repairs(round_plan)] == [
        ("spec_id", "assign_spec_id", "REQ::FR::VICTIM"),
        ("spec_id", "assign_spec_id", "MOD::PAYMENT_GATEWAY_2"),
    ]


def test_spec_id_repair_resumed_after_the_registry_write_keeps_the_alias() -> None:
    # The registry already holds the round's rows (the round crashed after ``record``).
    victim = node("victim", FR, spec_id="bad id", uuid="u-v", key="k-v")
    taken = {
        "REQ::FR::VICTIM": SpecIdRow("REQ::FR::VICTIM", "k-v", "u-v", "active"),
        "bad id": SpecIdRow("bad id", "k-v", "u-v", "alias"),
    }
    graph = state([victim])
    round_plan = plan(graph, spec_ids=SpecIds.of(taken, graph))
    assert round_plan.spec_ids == [{"id": "victim", "spec_id": "REQ::FR::VICTIM", "aliases": ["bad id"]}]
    # Another node's alias is never taken over.
    other = SpecIds.of({**taken, "bad id": SpecIdRow("bad id", "k-x", "u-x", "alias")}, graph)
    assert plan(graph, spec_ids=other).spec_ids == [{"id": "victim", "spec_id": "REQ::FR::VICTIM", "aliases": None}]


# The loop: read-only, valve, at most 2 rounds (FR-VER-01, 02, 04)


def node_row(item: VNode) -> dict[str, Any]:
    return {
        "id": item.id,
        "uuid": item.uuid,
        "spec_id": item.spec_id,
        "name": item.name,
        "identity_key": item.identity_key,
        "identity_aliases": list(item.identity_aliases),
        "spec_id_aliases": list(item.spec_id_aliases) or None,
        "label_disagreement": item.label_disagreement,
        "labels": list(item.labels),
        "evidence": list(item.evidence),
    }


def edge_row(item: VEdge) -> dict[str, Any]:
    return {
        "id": item.id,
        "relation": item.relation,
        "uuid": item.uuid,
        "edge_key": item.edge_key,
        "basis": item.basis,
        "fragment_ids": item.fragment_ids,
        "inference_rule": item.inference_rule,
        "confidence": item.confidence,
        "evidence": item.evidence,
        "source": item.source,
        "target": item.target,
    }


def fragment_row(item: VFragment) -> dict[str, Any]:
    return {
        "fragment_id": item.fragment_id,
        "document_id": item.document_id,
        "status": item.status,
        "content_hash": item.content_hash,
        "extracted_hash": item.extracted_hash,
        "grounds": item.grounds,
    }


def rows_of(graph: GraphState) -> dict[str, list[dict[str, Any]]]:
    return {
        VERIFY_SPEC_NODES.name: [node_row(item) for item in graph.nodes.values()],
        VERIFY_DOMAIN_EDGES.name: [edge_row(item) for item in graph.edges],
        VERIFY_FRAGMENTS.name: [fragment_row(item) for item in graph.fragments],
    }


class StuckExecutor(OfflineExecutor):
    """Reads always return the same rows (a repair that does not stick, or a fault that comes back);
    writes are recorded per transaction."""

    def __init__(self, rows: Mapping[str, list[dict[str, Any]]]) -> None:
        super().__init__(rows)
        self.transactions: list[list[tuple[str, dict[str, Any]]]] = []

    def tx(self, _project_id: str) -> Any:
        runs: list[tuple[str, dict[str, Any]]] = []
        self.transactions.append(runs)

        class Transaction:
            async def run(self, query: Any, **params: Any) -> list[dict[str, Any]]:
                runs.append((query.name, params))
                return []

        @asynccontextmanager
        async def opened() -> Any:
            yield Transaction()

        return opened()


def faulty_state() -> GraphState:
    nodes = [node(f"r{index}", FR) for index in range(20)] + [
        node("m", MOD),
        node("two", (FR, FEAT), spec_id="REQ::FR::TWO"),
    ]
    edges = [edge(f"e{index}", "PART_OF", f"r{index}", "m") for index in range(20)] + [
        edge("bad", "PART_OF", "m", "r0")
    ]
    fragments = (ACTIVE, VFragment("f-never", "d4.md", "active", "h", None, grounds=True))
    return state(nodes, edges, fragments)


async def test_verify_project_style_run_reports_and_never_writes(tmp_path: Path) -> None:
    executor = OfflineExecutor(rows_of(faulty_state()))  # any write or transaction fails the test
    report = await verify_graph(executor, PROJECT, ONTOLOGY, dropped={"RELATES_TO": 1}, repair=None)
    assert report.read_only and not report.ok and report.rounds == 0 and report.repairs == ()
    assert report.found == {"node_label": 1, "illegal_edge": 1, "fragment_state": 1}
    assert [(p.check, p.subject) for p in report.problems] == [
        ("node_label", "REQ::FR::TWO"),
        ("illegal_edge", "MOD::M|PART_OF|REQ::FR::R0"),
        ("fragment_state", "f-never"),
    ]
    assert report.follow_up_lineages == ()  # G6: verify_project lists problems only
    assert report.metrics.dropped_edges == {"RELATES_TO": 1}
    assert (report.spec_nodes, report.domain_edges) == (22, 21)


async def test_loop_stops_after_two_rounds_when_problems_persist(tmp_path: Path) -> None:
    executor = StuckExecutor(rows_of(faulty_state()))
    registry = open_file_stores(tmp_path).registry
    report = await verify_graph(executor, PROJECT, ONTOLOGY, dropped={}, repair=RepairScope("b1", Evidence(), registry))
    assert report.rounds == MAX_REPAIR_ROUNDS == len(executor.transactions) and not report.ok
    assert [(r.round, r.check, r.action) for r in report.repairs] == [
        (1, "fragment_state", "request_follow_up"),
        (1, "node_label", "set_label"),
        (1, "illegal_edge", "delete_edge"),
        (2, "node_label", "set_label"),
        (2, "illegal_edge", "delete_edge"),
    ]
    assert report.follow_up_lineages == ("d4.md",)
    assert {p.check for p in report.problems} == {"node_label", "illegal_edge", "fragment_state"}
    assert [name for name, _ in executor.transactions[0]] == ["build_repair_delete_edges", "build_repair_set_labels"]


async def test_loop_with_only_a_fragment_problem_opens_no_transaction(tmp_path: Path) -> None:
    graph = state([node("r", FR)], fragments=(ACTIVE, VFragment("f-weird", "d3.md", "weird", "h", "h", grounds=True)))
    registry = open_file_stores(tmp_path).registry
    report = await verify_graph(
        OfflineExecutor(rows_of(graph)), PROJECT, ONTOLOGY, dropped={}, repair=RepairScope("b1", Evidence(), registry)
    )
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 0, 0)
    assert report.follow_up_lineages == ("d3.md",)
    assert [(r.action, r.subject, r.value) for r in report.repairs] == [("request_follow_up", "f-weird", "d3.md")]
    assert [(p.check, p.code) for p in report.problems] == [("fragment_state", "unknown_status")]


async def test_blocked_round_is_not_applied(tmp_path: Path) -> None:
    # Half the nodes ungrounded with no evidence to restore: 10 deletes of 20 nodes.
    nodes = [node(f"r{index:02d}", FR, evidence=() if index % 2 else ("f1",)) for index in range(20)]
    registry = open_file_stores(tmp_path).registry
    report = await verify_graph(
        OfflineExecutor(rows_of(state(nodes))),
        PROJECT,
        ONTOLOGY,
        dropped={},
        repair=RepairScope("b1", Evidence(), registry),
    )
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 0, 1)
    assert report.valve == {"nodes": 10, "edges": 0, "spec_nodes": 20, "domain_edges": 0}
    assert report.repairs == () and report.removed_spec_ids == ()
    assert report.found == {"ungrounded_node": 10} and len(report.problems) == 10


class RepairingExecutor(StuckExecutor):
    """Reads ``before`` until the first repair transaction, then ``after`` (the repaired graph)."""

    def __init__(self, before: GraphState, after: GraphState) -> None:
        super().__init__(rows_of(before))
        self.after = rows_of(after)

    def tx(self, project_id: str) -> Any:
        self.rows = self.after
        return super().tx(project_id)


async def test_inferred_edges_deleted_with_their_node_are_reported_and_enter_spec_changes(tmp_path: Path) -> None:
    lonely = node("lonely", FR, evidence=())  # ungrounded, nothing to restore: deleted
    kept = [node(f"r{index:02d}", FR) for index in range(20)] + [node("m", MOD), node("f", FEAT)]
    stated = [edge(f"e{index:02d}", "PART_OF", f"r{index:02d}", "m") for index in range(20)]
    stated += [edge(f"x{index:02d}", "RELATES_TO", f"r{index:02d}", f"r{(index + 1) % 20:02d}") for index in range(20)]
    # 42 domain edges: the 2 that go with the node are within max(1, 5%).
    gone = [inferred("inf", "PART_OF", "lonely", "f"), edge("st", "RELATES_TO", "lonely", "r00")]
    before = state([*kept, lonely], [*stated, *gone])
    executor = RepairingExecutor(before, state(kept, stated))
    registry = open_file_stores(tmp_path).registry
    report = await verify_graph(executor, PROJECT, ONTOLOGY, dropped={}, repair=RepairScope("b1", Evidence(), registry))
    assert (report.ok, report.rounds) == (True, 1)
    assert [(r.check, r.action, r.subject) for r in report.repairs] == [
        ("ungrounded_node", "delete_node", "REQ::FR::LONELY")
    ]
    assert executor.transactions[0] == [("build_repair_delete_nodes", {"ids": ["lonely"]})]
    assert report.removed_spec_ids == ("REQ::FR::LONELY",)
    # Only the inferred edge that went with the node; the stated one is no spec change of its own.
    assert report.removed_inferred_edges == ("REQ::FR::LONELY|PART_OF|FEAT::F",)
    # The build result's spec changes (run.py): the node removed, the inferred edge no longer added.
    changes = SpecChanges(added=("REQ::FR::LONELY",), inferred_added=("REQ::FR::LONELY|PART_OF|FEAT::F", "X|PART_OF|Y"))
    assert _with_repairs(changes, report) == SpecChanges(
        removed=("REQ::FR::LONELY",),
        inferred_added=("X|PART_OF|Y",),
        inferred_removed=("REQ::FR::LONELY|PART_OF|FEAT::F",),
    )


def test_repair_deletions_enter_spec_changes() -> None:
    changes = SpecChanges(
        added=("REQ::FR::A", "MOD::X"), modified=("MOD::Y",), inferred_added=("A|PART_OF|B", "C|PART_OF|D")
    )
    report = VerificationReport(removed_spec_ids=("MOD::X", "MOD::Y"), removed_inferred_edges=("A|PART_OF|B",))
    assert _with_repairs(changes, report) == SpecChanges(
        added=("REQ::FR::A",),
        removed=("MOD::X", "MOD::Y"),
        inferred_added=("C|PART_OF|D",),
        inferred_removed=("A|PART_OF|B",),
    )
    assert _with_repairs(changes, VerificationReport()) == changes
    assert _with_repairs(changes, None) == changes


# Metrics (G7) and the spine walk


def test_metrics_count_exclusive_conflicts_disagreements_and_fragments_without_nodes() -> None:
    nodes = [
        node("r", FR, label_disagreement="Feature"),
        node("s", FR),
        node("f1", FEAT),
        node("f2", FEAT),
        node("f3", FEAT),
        node("m1", MOD),
        node("m2", MOD),
    ]
    edges = [
        edge("a", "PART_OF", "r", "f1"),
        edge("b", "PART_OF", "r", "f2"),
        inferred("c", "PART_OF", "r", "f3"),  # every extra target counts, inferred ones too
        edge("d", "PART_OF", "s", "f1"),
        edge("e", "PART_OF", "s", "m1"),  # another target label: no conflict
        edge("g", "RELATES_TO", "s", "m2"),  # not exclusive
        edge("h", "PART_OF", "f1", "m1"),
        edge("i", "PART_OF", "f1", "m2"),  # Feature -> Module is exclusive too
    ]
    fragments = (
        ACTIVE,
        VFragment("f-lonely", "d1.md", "active", "h", "h", grounds=False),
        VFragment("f-skip-lonely", "d1.md", "skipped", "h", None, grounds=False),
        VFragment("f-retired", "d1.md", "retired", "h", "h", grounds=False),
    )
    metrics = measure(state(nodes, edges, fragments), ONTOLOGY, {"RELATES_TO": 1})
    assert metrics.exclusive_conflicts == 3
    assert metrics.label_disagreement == 1
    assert metrics.fragments_without_nodes == 1
    assert metrics.dropped_edges == {"RELATES_TO": 1}


def test_dropped_edges_are_summed_per_relation_from_the_conform_counters() -> None:
    target = TargetGraph(
        counters=TargetCounters(dropped_edges={"RELATES_TO": {"illegal_pair": 1, "self_loop": 2}, "PART_OF": {"x": 0}})
    )
    assert dropped_by_relation(target) == {"RELATES_TO": 3}
    assert dropped_by_relation(None) == {}


def test_spine_unreachable_before_reads_stated_and_implied_edges_only() -> None:
    nodes = [node("r1", FR), node("r2", FR), node("r3", FR), node("m", MOD), node("f", FEAT)]
    edges = [
        edge("a", "PART_OF", "r1", "m"),
        edge("b", "PART_OF", "r2", "f", basis="implied"),
        edge("c", "PART_OF", "f", "m"),
        inferred("d", "PART_OF", "r3", "m"),
    ]
    metrics = measure(state(nodes, edges), ONTOLOGY, {})
    assert (metrics.spine_unreachable_before, metrics.spine_unreachable) == (1, 0)


def test_spine_unreachable_before_reads_an_edge_without_basis_as_stated() -> None:
    graph = state([node("r", FR), node("m", MOD)], [edge("a", "PART_OF", "r", "m", basis=None)])
    metrics = measure(graph, ONTOLOGY, {})
    assert (metrics.spine_unreachable_before, metrics.spine_unreachable) == (0, 0)


def spine(nodes: Sequence[tuple[str, str]], edges: Sequence[tuple[str, str, str, str | None]]) -> SpineGraph:
    return SpineGraph(
        nodes=tuple(SpineNode(key=key, spec_id=f"S::{key.upper()}", label=label) for key, label in nodes),
        edges=tuple(SpineEdge(relation, source, target, basis) for relation, source, target, basis in edges),
    )


def unreached(graph: SpineGraph, bases: Sequence[str] | None = None) -> list[str]:
    return [item.key for item in graph.unreached(ONTOLOGY, ONTOLOGY.spine.sources, bases)]


def test_spine_walk_paths() -> None:
    nodes = [
        ("m", MOD),
        ("f1", FEAT),
        ("f2", FEAT),
        ("f3", FEAT),
        ("c", CAP),
        ("one", FR),
        ("three", FR),
        ("four", FR),
        ("seq", FR),
        ("rev", FR),
        ("rel", FR),
        ("rel2", FR),
        ("lone", FR),
    ]
    edges = [
        ("PART_OF", "one", "m", "stated"),
        ("PART_OF", "three", "f1", "stated"),  # three -> f1 -> f2 -> m: 3 hops
        ("PART_OF", "f1", "f2", "stated"),
        ("PART_OF", "f2", "m", "stated"),
        ("PART_OF", "four", "f3", "stated"),  # four -> f3 -> f1 -> f2 -> m: 4 hops, over max_hops
        ("PART_OF", "f3", "f1", "stated"),
        ("GROUPED_BY", "seq", "c", "stated"),  # GROUPED_BY then REALIZED_BY
        ("REALIZED_BY", "c", "m", "stated"),
        ("REALIZED_BY", "rev", "c", "stated"),  # wrong order: no path
        ("RELATES_TO", "m", "rel", "stated"),  # symmetric, stored the other way round
        ("RELATES_TO", "rel2", "rel", "stated"),  # RELATES_TO is one hop only
        ("PART_OF", "lone", "ghost", "stated"),  # an end that is no node is ignored
    ]
    assert unreached(spine(nodes, edges)) == ["four", "lone", "rel2", "rev"]  # in spec_id order


def test_spine_walk_bases_and_label_problem_nodes() -> None:
    graph = spine([("m", MOD), ("r", FR)], [("PART_OF", "r", "m", "inferred")])
    assert unreached(graph) == []
    assert unreached(graph, ("stated", "implied")) == ["r"]
    # verify leaves a node with a label problem out of the walk, and so the edges through it.
    nodes = [node("r", FR), node("f", (FEAT, MOD), spec_id="FEAT::F"), node("m", MOD)]
    walk = verification.spine_graph(state(nodes, [edge("a", "PART_OF", "r", "f"), edge("b", "PART_OF", "f", "m")]))
    assert [item.key for item in walk.nodes] == ["r", "m"]
    assert [item.key for item in walk.unreached(ONTOLOGY, ONTOLOGY.spine.sources)] == ["r"]


# The example's retry options (examples/graph_build.py)


def parse(monkeypatch: pytest.MonkeyPatch, *extra: str) -> argparse.Namespace:

    monkeypatch.setattr(sys, "argv", ["graph_build.py", "--project", "p", "--corpus", "c", "--state", "s", *extra])
    return graph_build.parse_args()


def test_example_parses_retry_options(monkeypatch: pytest.MonkeyPatch) -> None:
    args = parse(monkeypatch)
    assert (args.retry_follow_ups, args.retry_lineage, args.project, args.state) == (False, [], "p", Path("s"))
    args = parse(monkeypatch, "--retry-follow-ups", "--retry-lineage", "a.md", "--retry-lineage", "b.md")
    assert args.retry_follow_ups and args.retry_lineage == ["a.md", "b.md"]


class _Builds:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def latest_build(self, _project_id: str, **options: Any) -> Any:
        assert options.get("finished_only")
        return SimpleNamespace(build_id="b1") if self.result is not None else None

    async def get_result(self, _project_id: str, _build_id: str) -> Any:
        return self.result


async def test_example_retry_lineages_merge_explicit_and_follow_ups(monkeypatch: pytest.MonkeyPatch) -> None:

    result = SimpleNamespace(verification=SimpleNamespace(follow_up_lineages=("orders.md", "a.md")))
    stores = SimpleNamespace(builds=_Builds(result))
    args = parse(monkeypatch, "--retry-follow-ups", "--retry-lineage", "a.md")
    assert await graph_build.retry_lineages(stores, "p", args) == ("a.md", "orders.md")
    args = parse(monkeypatch, "--retry-lineage", "a.md")
    assert await graph_build.retry_lineages(stores, "p", args) == ("a.md",)
    args = parse(monkeypatch, "--retry-follow-ups")
    assert await graph_build.retry_lineages(SimpleNamespace(builds=_Builds(None)), "p", args) == ()
    no_verification = SimpleNamespace(builds=_Builds(SimpleNamespace(verification=None)))
    assert await graph_build.retry_lineages(no_verification, "p", args) == ()
