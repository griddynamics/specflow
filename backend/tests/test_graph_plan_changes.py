"""Plan report-chain and change-set rules."""

from __future__ import annotations

import pytest

from graph.core.errors import ConfigurationError
from graph.plan.changes import ChangeSet, change_set, check_chain, meaning_revisions, resolve_spec_id
from graph.plan.models import BuildChangeReport, ChangedSpecEdge, ChangedSpecNode, MeaningRevisions


def report(
    build_id: str,
    previous_build_id: str | None = None,
    nodes: tuple[ChangedSpecNode, ...] = (),
    edges: tuple[ChangedSpecEdge, ...] = (),
) -> BuildChangeReport:
    return BuildChangeReport(
        build_id=build_id,
        previous_build_id=previous_build_id,
        nodes=nodes,
        edges=edges,
    )


def test_first_mode_accepts_empty_changes_and_refuses_reports() -> None:
    assert check_chain((), "build-2", "build-1", "first") == ()
    with pytest.raises(ConfigurationError, match="change_reports_missing") as raised:
        check_chain((report("build-1")), None, "build-1", "first")

    assert raised.value.code == "change_reports_missing"


def test_replan_accepts_no_reports() -> None:
    assert check_chain((), "build-2", "build-4", "replan") == ()


def test_update_requires_a_chain_from_the_last_consumed_build_to_the_current_build() -> None:
    reports = (
        report("build-3", "build-2"),
        report("build-4", "build-3"),
    )

    assert check_chain(reports, "build-2", "build-4", "update") == reports


def test_a_gap_names_the_missing_build_before_any_write() -> None:
    reports = (
        report("build-4", "build-3"),
        report("build-5", "build-4"),
    )

    with pytest.raises(ConfigurationError, match="change_reports_missing") as raised:
        check_chain(reports, "build-2", "build-5", "update")

    assert raised.value.code == "change_reports_missing"
    assert raised.value.fields["first_missing"] == "build-3"


def test_a_missing_trailing_report_names_the_target_build() -> None:
    reports = (report("build-3", "build-2"),)

    with pytest.raises(ConfigurationError, match="change_reports_missing") as raised:
        check_chain(reports, "build-2", "build-4", "update")

    assert raised.value.fields["first_missing"] == "build-4"


def test_change_set_holds_link_changes_and_ignores_evidence_changes() -> None:
    reports = (
        report(
            "build-2",
            "build-1",
            edges=(
                ChangedSpecEdge(
                    source="REQ::ORPHAN", relation="PART_OF", target="MOD::WEB_SHOP", change="added", basis="inferred"
                ),
            ),
        ),
        report(
            "build-3",
            "build-2",
            nodes=(ChangedSpecNode(spec_id="ENT::ORDER", classes=("changed_evidence",)),),
        ),
    )

    changes = change_set(reports)

    assert changes == ChangeSet(
        links=frozenset({"MOD::WEB_SHOP", "REQ::ORPHAN"}),
    )
    assert changes.empty is False


def test_an_evidence_only_change_set_is_empty() -> None:
    reports = (report("build-2", "build-1", nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("changed_evidence",)),)),)

    assert change_set(reports) == ChangeSet()


def test_change_set_takes_the_last_meaning_class_in_build_order() -> None:
    reports = (
        report("build-2", "build-1", nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("added",)),)),
        report("build-3", "build-2", nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("changed_meaning",)),)),
    )

    assert change_set(reports).meaning == (("REQ-1", "changed_meaning"),)


def test_changed_links_nodes_and_both_edge_ends_become_link_changes() -> None:
    reports = (
        report(
            "build-2",
            "build-1",
            nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("changed_links",)),),
            edges=(
                ChangedSpecEdge(source="REQ-2", relation="PART_OF", target="FEAT-1", change="changed", basis="stated"),
            ),
        ),
        report("build-3", "build-2"),
    )

    assert change_set(reports).links == frozenset({"FEAT-1", "REQ-1", "REQ-2"})


def test_a_meaning_change_in_build_3_sets_the_node_revision_to_build_3() -> None:
    reports = (
        report(
            "build-3",
            "build-2",
            nodes=(ChangedSpecNode(spec_id="REQ-1", classes=("changed_meaning",)),),
        ),
    )

    revisions = meaning_revisions(
        reports,
        node_build_ids={"REQ-1": "build-3"},
        stored=MeaningRevisions(nodes={"REQ-1": "build-2"}, consumed=("build-2",)),
        build_id="build-3",
        consumed=("build-2", "build-3"),
    )

    assert revisions == MeaningRevisions(
        nodes={"REQ-1": "build-3"},
        consumed=("build-2", "build-3"),
    )


def test_link_and_evidence_changes_keep_the_stored_meaning_revision() -> None:
    reports = (
        report(
            "build-3",
            "build-2",
            nodes=(
                ChangedSpecNode(spec_id="REQ-1", classes=("changed_links",)),
                ChangedSpecNode(spec_id="REQ-2", classes=("changed_evidence",)),
            ),
        ),
    )

    revisions = meaning_revisions(
        reports,
        node_build_ids={"REQ-1": "build-3", "REQ-2": "build-2"},
        stored={"REQ-1": "build-2", "REQ-2": "build-2"},
        build_id="build-3",
        consumed=("build-2", "build-3"),
    )

    assert revisions.nodes == {"REQ-1": "build-2", "REQ-2": "build-2"}


def test_a_node_without_a_stored_revision_gets_the_consumed_build() -> None:
    reports = (report("build-3", "build-2"),)

    revisions = meaning_revisions(
        reports,
        node_build_ids={"REQ-1": "build-2", "REQ-2": "build-3"},
        stored={"REQ-2": "build-2"},
        build_id="build-3",
        consumed=("build-1", "build-2", "build-3"),
    )

    assert revisions.nodes == {"REQ-1": "build-3", "REQ-2": "build-2"}


def test_a_merge_and_later_change_resolve_to_one_survivor_revision() -> None:
    reports = (
        report("build-2", "build-1", nodes=(ChangedSpecNode(spec_id="REQ-A", classes=("merged",)),)),
        report(
            "build-3",
            "build-2",
            nodes=(ChangedSpecNode(spec_id="REQ-B", classes=("changed_meaning",)),),
        ),
    )

    revisions = meaning_revisions(
        reports,
        node_build_ids={"REQ-B": "build-3"},
        stored=MeaningRevisions(nodes={"REQ-A": "build-1", "REQ-B": "build-1"}, consumed=("build-1",)),
        build_id="build-3",
        aliases={"REQ-A": "REQ-B"},
        consumed=("build-1", "build-2", "build-3"),
    )

    assert revisions.nodes == {"REQ-B": "build-3"}


def test_a_rekeyed_id_later_resolves_through_registry_aliases() -> None:
    assert resolve_spec_id("REQ-OLD", {"REQ-OLD": "REQ-MIDDLE", "REQ-MIDDLE": "REQ-NEW"}) == "REQ-NEW"
    assert resolve_spec_id("REQ-NEW", {"REQ-OLD": "REQ-NEW", "REQ-NEW": "REQ-NEW"}) == "REQ-NEW"


def test_replan_without_reports_sets_every_node_newer_than_the_consumed_chain() -> None:
    revisions = meaning_revisions(
        (),
        node_build_ids={"REQ-1": "build-3", "REQ-2": "build-2", "REQ-3": "build-4"},
        stored={"REQ-1": "build-2", "REQ-2": "build-2"},
        build_id="build-4",
        consumed=("build-2", "build-4"),
        replan_without_reports=True,
    )

    assert revisions == MeaningRevisions(
        nodes={"REQ-1": "build-4", "REQ-2": "build-2", "REQ-3": "build-4"},
        consumed=("build-2", "build-4"),
    )
