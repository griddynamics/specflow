"""Tier 1: the host's conversion of build results to the gap-side models (``api.gap.changes``)."""

from __future__ import annotations

from datetime import UTC, datetime

from api.gap.changes import build_facts, gap_change_reports
from api.graph_build.reports import ChainLink
from graph.build import BuildResult
from graph.build.models import ChangedEdge, ChangedNode, ChangeReport, DedupRefusal, QuarantinedEdge
from graph.gap import BuildFacts, CutEdge, GapChangeReport, RefusedPair

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def node(spec_id: str, *classes: str, previous: tuple[str, ...] = ()) -> ChangedNode:
    return ChangedNode(spec_id=spec_id, label="Requirement", classes=classes, previous_spec_ids=previous)


def test_each_link_becomes_one_report_with_its_node_classes_in_build_order():
    first = ChainLink(
        build_id="build-2",
        previous_build_id="build-1",
        report=ChangeReport(
            nodes=(
                node("REQ-1", "added"),
                node("REQ-2", "removed"),
                node("REQ-3", "merged", "changed_meaning", previous=("REQ-3", "REQ-30", "REQ-31")),
                node("REQ-4", "rekeyed", "changed_links", previous=("REQ-40",)),
                node("REQ-5", "changed_evidence"),
                node("REQ-6", "carried_over"),
                node("REQ-7", "changed_links", "changed_evidence"),
            ),
            edges=(ChangedEdge(ref="REQ-4|DEPENDS_ON|REQ-7", change="added", basis="stated"),),
        ),
    )
    second = ChainLink(build_id="build-3", previous_build_id="build-2", report=ChangeReport())

    reports = gap_change_reports([first, second])

    assert reports == (
        GapChangeReport(
            build_id="build-2",
            added=("REQ-1",),
            removed=("REQ-2",),
            merged={"REQ-30": "REQ-3", "REQ-31": "REQ-3"},
            rekeyed={"REQ-40": "REQ-4"},
            changed_meaning=("REQ-3",),
            changed_links=("REQ-4", "REQ-7"),
            changed_evidence=("REQ-5", "REQ-7"),
        ),
        GapChangeReport(build_id="build-3"),
    )
    assert reports[0].fragments_changed == {} and reports[0].documents == {}
    assert gap_change_reports([]) == ()


def result(**values) -> BuildResult:
    return BuildResult(
        project_id="p", build_id="build-1", mode="update", status="succeeded", started_at=T0, finished_at=T0, **values
    )


def refusal(first: str | None, second: str | None, *, collapsed: bool = False) -> DedupRefusal:
    return DedupRefusal(
        first_spec_id=first,
        second_spec_id=second,
        label="Requirement",
        guard="negation",
        name_cosine=0.95,
        collapsed=collapsed,
    )


def quarantined(reason: str, source: str | None, target: str | None) -> QuarantinedEdge:
    return QuarantinedEdge(
        reason=reason,
        relation="DEPENDS_ON",
        source_label="Requirement",
        target_label="Requirement",
        basis="stated",
        source_spec_id=source,
        target_spec_id=target,
        fragment_ids=("f-1", "f-2"),
    )


def test_build_facts_keep_refused_pairs_and_cycle_cuts_between_two_persisted_nodes():
    facts = build_facts(
        result(
            dedup_refused=(
                refusal("REQ-1", "REQ-2"),
                refusal(None, "REQ-3"),
                refusal("REQ-4", "REQ-4", collapsed=True),
            ),
            quarantine=(
                quarantined("cycle_broken", "REQ-1", "REQ-2"),
                quarantined("cycle_broken", "REQ-1", None),
                quarantined("illegal_pair", "REQ-5", "REQ-6"),
            ),
        )
    )

    assert facts == BuildFacts(
        build_id="build-1",
        refused_pairs=(
            RefusedPair(first_spec_id="REQ-1", second_spec_id="REQ-2", label="Requirement", guard="negation"),
        ),
        cut_edges=(
            CutEdge(relation="DEPENDS_ON", source_spec_id="REQ-1", target_spec_id="REQ-2", fragment_ids=("f-1", "f-2")),
        ),
    )
    assert build_facts(result()) == BuildFacts(build_id="build-1")
