"""Change report offline ("Change report", Diff ``edge_changes``)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from graph.build import BuildContext, FileStores, open_file_stores
from graph.build import run as runner
from graph.build.change_report import classify
from graph.build.diff import diff
from graph.build.models import (
    ChangedEdge,
    ChangedNode,
    ChangeReport,
    DiffArtifact,
    DocumentRec,
    InferenceReport,
    JudgedItem,
    PersistCursor,
    Repair,
    VerificationReport,
)
from graph.core.identity import SpecIdRow
from graph_support import OfflineExecutor, ScriptedGateway, frozen_ontology, shipped_ontology
from test_graph_diff import (
    A1,
    A2,
    B1,
    DOC_A,
    DOCUMENTS,
    E_R1_H1,
    E_R2_M1,
    H1,
    M1,
    OLDER,
    ONT,
    R1,
    R2,
    UPDATE_PLAN,
    Persisted,
    edge,
    frag,
    given,
    graph,
    inferred,
    node,
    node_h1,
    node_m1,
    node_r1,
    node_r2,
    persisted,
    plan_of,
    unchanged,
)

pytest_plugins = ["graph_support"]

SHIPPED = shipped_ontology()


def classified(
    before: Persisted,
    artifact: DiffArtifact,
    inference: InferenceReport | None = None,
    verification: VerificationReport | None = None,
) -> ChangeReport:
    """The change report of ``artifact``, the registry as the build leaves it."""

    return classify(artifact, inference, verification, SHIPPED, registry=registry_after(before, artifact))


def registry_after(before: Persisted, artifact: DiffArtifact) -> dict[str, SpecIdRow]:
    return {**before.taken, **{row.spec_id: row for row in artifact.registry_rows}}


def by_id(report: ChangeReport) -> dict[str, ChangedNode]:
    return {node.spec_id: node for node in report.nodes}


def classes(report: ChangeReport) -> dict[str, tuple[str, ...]]:
    return {node.spec_id: node.classes for node in report.nodes}


def ref(before: Persisted, source: str, relation: str, target: str) -> str:
    return f"{before.node(source).spec_id}|{relation}|{before.node(target).spec_id}"


def update(before: Persisted, *items: Any, plan: Any = UPDATE_PLAN) -> DiffArtifact:
    return diff(plan, graph(*items), ONT, given(before.current, before.taken))


UNCHANGED = (node_r1(), node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1)


# One test per class


def test_a_first_build_lists_every_node_and_edge_as_added() -> None:
    before = persisted()
    report = classify(before.artifact, None, None, SHIPPED, registry=before.taken)

    assert set(classes(report).values()) == {("added",)}
    assert len(report.nodes) == 4 and report.counts == {"added": 4}
    assert {(change.ref, change.change, change.basis) for change in report.edges} == {
        (ref(before, R1, "PART_OF", H1), "added", "stated"),
        (ref(before, R2, "PART_OF", M1), "added", "stated"),
    }
    assert report.edge_counts == {"added": 2}
    assert before.artifact.edge_changes == report.edges  # the diff's own list, ordered by ref


def test_an_unchanged_update_reports_nothing() -> None:
    before = persisted()
    artifact = update(before, *UNCHANGED)
    assert artifact.edge_changes == ()
    assert classified(before, artifact) == ChangeReport()


def test_a_deleted_node_is_removed_and_its_edge_ends_lose_a_link() -> None:
    before = persisted()
    artifact = update(before, node_r1(), node_r2(), node_h1(), E_R1_H1)  # M1 gone, and its edge

    report = classified(before, artifact)
    m1, r2 = before.node(M1).spec_id, before.node(R2).spec_id
    assert by_id(report)[m1] == ChangedNode(spec_id=m1, label="Module", classes=("removed",))
    assert by_id(report)[r2].classes == ("changed_meaning",)  # QualityRequirement PART_OF Module: owner pair
    assert report.edges == (ChangedEdge(ref=ref(before, R2, "PART_OF", M1), change="removed", basis="stated"),)


def test_a_merge_survivor_names_the_merged_ids_and_moved_edges_change_under_their_new_ends() -> None:
    m2_key = "Module|surface|order api"
    m2 = node(m2_key, "Order API", "Module", [(A1, 2, 2)])
    before = persisted(target=graph(*UNCHANGED, m2))
    before.replace_node(m2_key, created_at=OLDER)  # older: it survives
    survivor, loser = before.node(m2_key), before.node(M1)
    artifact = update(before, node_r1(), node_r2(), node_h1(), node_m1(aliases=(m2_key,)), E_R1_H1, E_R2_M1)

    report = classified(before, artifact)
    merged = by_id(report)[survivor.spec_id]
    assert merged.classes[:1] == ("merged",) and merged.previous_spec_ids == (loser.spec_id,)
    assert "merged" in merged.causes and loser.spec_id not in by_id(report)
    moved = f"{before.node(R2).spec_id}|PART_OF|{survivor.spec_id}"
    assert report.edges == (ChangedEdge(ref=moved, change="changed", basis="stated", causes=("ends",)),)


def test_a_merge_survivor_whose_own_properties_did_not_change_is_listed_as_merged() -> None:
    m2_key = "Module|surface|order api"
    m2 = node(m2_key, "Order API", "Module", [(A1, 2, 2)])
    before = persisted(target=graph(*UNCHANGED, m2))
    before.replace_node(m2_key, created_at=OLDER)  # older: it survives
    survivor, loser = before.node(m2_key), before.node(M1)
    artifact = update(before, node_r1(), node_r2(), node_h1(), node_m1(aliases=(m2_key,)), E_R1_H1, E_R2_M1)
    survivor_ops = [op for op in artifact.operations if getattr(op, "spec_id", None) == survivor.spec_id]
    # the same diff without the survivor's own update: its properties did not change
    bare = artifact.model_copy(update={"operations": tuple(op for op in artifact.operations if op not in survivor_ops)})

    assert survivor_ops  # the update the survivor gets in this diff (its merge cause)

    merged = by_id(classified(before, bare))[survivor.spec_id]
    # merged, and its links changed by the moved edge; no property class
    assert (merged.classes, merged.previous_spec_ids) == (("merged", "changed_links"), (loser.spec_id,))
    assert merged.causes == ("merged",)


def test_a_renamed_node_is_carried_over_with_a_changed_meaning_and_its_rekeyed_edges_do_not_change() -> None:
    before = persisted()
    new_key = "Feature|surface|order export job"
    renamed = node_h1(key=new_key, name="Order export job")
    artifact = update(
        before, node_r1(), node_r2(), renamed, node_m1(), edge(R1, "PART_OF", new_key, [A1], fact=E_R1_H1.fact), E_R2_M1
    )

    report = classified(before, artifact)
    h1 = before.node(H1).spec_id
    assert classes(report) == {h1: ("carried_over", "changed_meaning", "changed_evidence")}
    assert set(by_id(report)[h1].causes) >= {"name", "identity_key", "vectors"}
    assert report.edges == ()  # an update of the edge key only is no change


def test_a_node_matched_by_an_alias_keeps_its_spec_id_rekeyed() -> None:
    before = persisted()
    new_key = "Module|surface|orders svc"
    rekeyed = node(new_key, "Orders service", "Module", [(A1, 2, 2)], aliases=(M1,))
    moved = edge(R2, "PART_OF", new_key, [A2], fact=E_R2_M1.fact)
    artifact = update(before, node_r1(), node_r2(), node_h1(), rekeyed, E_R1_H1, moved)

    report = classified(before, artifact)
    m1 = before.node(M1).spec_id
    assert artifact.spec_changes.rekeyed == (m1,)
    assert classes(report)[m1][0] == "rekeyed" and "identity_key" in by_id(report)[m1].causes
    assert "changed_links" not in classes(report)[m1] and report.edges == ()


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ({"name": "Staff export orders as CSV"}, ("changed_meaning", "changed_evidence")),  # vectors follow the name
        ({"stmt": "Staff can export orders as XLSX."}, ("changed_evidence",)),  # same quote: a paraphrase
        (
            {"stmt": "Staff can export orders as XLSX.", "quote": "FR-1 Staff can export orders as XLSX."},
            ("changed_meaning", "changed_evidence"),
        ),
        ({"attributes": {"acceptance_criteria": "The file opens anywhere."}}, ("changed_meaning",)),
        ({"quote": "FR-1: staff export orders."}, ("changed_evidence",)),
    ],
)
def test_a_meaning_property_changes_the_meaning_and_the_quote_only_the_evidence(
    changed: Mapping[str, Any], expected: tuple[str, ...]
) -> None:
    before = persisted()
    artifact = update(before, node_r1(**changed), *UNCHANGED[1:])

    report = classified(before, artifact)
    assert classes(report) == {before.node(R1).spec_id: expected}


def test_another_new_edge_changes_the_links_of_both_ends() -> None:
    before = persisted()
    artifact = update(before, *UNCHANGED, edge(R1, "RELATES_TO", R2, [A1, A2]))

    report = classified(before, artifact)
    r1, r2 = before.node(R1).spec_id, before.node(R2).spec_id
    assert classes(report) == {r1: ("changed_links",), r2: ("changed_links",)}
    assert report.edges == (ChangedEdge(ref=f"{r1}|RELATES_TO|{r2}", change="added", basis="stated"),)


def test_an_owner_pair_edge_changes_the_meaning_of_its_source_and_the_links_of_its_owner() -> None:
    before = persisted()
    artifact = update(before, *UNCHANGED, edge(R1, "PART_OF", M1, [A1]))  # FunctionalRequirement PART_OF Module

    report = classified(before, artifact)
    assert classes(report) == {
        before.node(R1).spec_id: ("changed_meaning",),
        before.node(M1).spec_id: ("changed_links",),
    }


def test_lines_that_only_move_change_the_evidence_never_the_meaning() -> None:
    before = persisted()
    shifted = frag("a.md#intro#1", ["# A", "", *A1.text.split("\n")[1:]], 1).model_copy(update={"change": "changed"})
    plan = plan_of([shifted, unchanged(A2), unchanged(B1)])
    artifact = update(
        before,
        node_r1(evidence=[(shifted, 4, 4)]),
        node_r2(),
        node_h1(),
        node_m1(evidence=[(shifted, 3, 3)]),
        E_R1_H1,
        E_R2_M1,
        plan=plan,
    )

    report = classified(before, artifact)
    r1, m1 = before.node(R1).spec_id, before.node(M1).spec_id
    assert classes(report) == {r1: ("changed_evidence",), m1: ("changed_evidence",)}
    assert {node.causes for node in report.nodes} == {("links",)}
    assert report.edges == ()


def test_an_edge_whose_evidence_only_changed_changes_the_evidence_of_its_ends() -> None:
    before = persisted()
    artifact = update(before, *UNCHANGED[:5], E_R2_M1.model_copy(update={"fact": "Exports finish fast."}))

    report = classified(before, artifact)
    assert classes(report) == {
        before.node(R2).spec_id: ("changed_evidence",),
        before.node(M1).spec_id: ("changed_evidence",),
    }
    assert report.edges == (
        ChangedEdge(ref=ref(before, R2, "PART_OF", M1), change="changed", basis="stated", causes=("fact",)),
    )


def test_an_inferred_edge_turned_stated_is_a_changed_link() -> None:
    before = persisted()
    before.add_edge(R1, "RELATES_TO", R2, **inferred())
    artifact = update(before, *UNCHANGED, edge(R1, "RELATES_TO", R2, [A1], fact="FR-1 relates."))

    report = classified(before, artifact)
    [change] = report.edges
    assert (change.change, change.basis, change.rule) == ("changed", "stated", None)
    assert set(change.causes) == {"basis", "fact", "source_fragment_ids"}
    assert set(classes(report).values()) == {("changed_links",)}


def test_inferred_edges_come_from_the_inference_report_and_the_verification_repairs() -> None:
    before = persisted()
    artifact = update(before, *UNCHANGED)
    r1, r2, m1, h1 = (before.node(key).spec_id for key in (R1, R2, M1, H1))
    attached, unlinked, repaired = f"{r1}|PART_OF|{m1}", f"{r2}|RELATES_TO|{h1}", f"{r2}|PART_OF|{h1}"
    item = JudgedItem(
        rule="INF-CONT-02",
        kind="owner",
        subject=r1,
        source_spec_id=r1,
        relation="PART_OF",
        target_spec_id=m1,
        outcome="accepted",
    )
    inference = InferenceReport(inferred_added=(attached, repaired), inferred_removed=(unlinked,), judged_items=(item,))
    verification = VerificationReport(
        removed_spec_ids=(h1,),
        removed_inferred_edges=(repaired,),
        repairs=(Repair(round=1, check="node_label", action="set_label", subject=r2, value="FunctionalRequirement"),),
    )

    report = classified(before, artifact, inference, verification)
    assert report.edges == (
        ChangedEdge(ref=attached, change="added", basis="inferred", rule="INF-CONT-02"),
        ChangedEdge(ref=unlinked, change="removed", basis="inferred"),
    )  # added and repaired away in the same build: not listed
    assert classes(report) == {
        r1: ("changed_meaning",),  # gained an owner (FunctionalRequirement PART_OF Module)
        r2: ("changed_meaning", "changed_links"),  # its label set by a repair, and a link lost
        m1: ("changed_links",),  # a new member
        h1: ("removed",),
    }
    assert by_id(report)[r2].label == "FunctionalRequirement" and by_id(report)[h1].label == "Feature"


# Update scenarios of a shop-like corpus


def test_a_new_document_adds_its_nodes_and_links_the_existing_ones() -> None:
    before = persisted()
    c1 = frag("c.md#intro#4", ["# C", "FR-2 Staff can schedule exports."], 1)
    doc_c = DocumentRec(document_id="c.md", revision_id="r1", version=1, path="c.md", title="C")
    plan = plan_of([*(unchanged(f) for f in (A1, A2, B1)), c1], documents=(*DOCUMENTS, doc_c))
    fr2 = node(
        "requirement|id|FR-2",
        "Staff can schedule exports",
        "FunctionalRequirement",
        [(c1, 2, 2)],
        revision={"c.md": ("r1", 1)},
    )
    artifact = update(before, *UNCHANGED, fr2, edge(fr2.identity_key, "PART_OF", H1, [c1]), plan=plan)

    report = classified(before, artifact)
    [added] = [node for node in report.nodes if node.classes == ("added",)]
    assert added.label == "FunctionalRequirement"
    assert classes(report) == {added.spec_id: ("added",), before.node(H1).spec_id: ("changed_links",)}
    assert report.edges == (
        ChangedEdge(ref=f"{added.spec_id}|PART_OF|{before.node(H1).spec_id}", change="added", basis="stated"),
    )


def test_a_changed_section_changes_the_meaning_of_its_rewritten_node_only() -> None:
    before = persisted()
    rewritten = frag("a.md#export#2", ["## Export", "Exports finish within 5 s.", "Exports are signed."], 4, 1)
    plan = plan_of([unchanged(A1), rewritten.model_copy(update={"change": "changed"}), unchanged(B1)])
    r2 = node_r2(evidence=[(rewritten, 5, 5)], stmt="Exports finish within 5 s, signed.", quote="Exports are signed.")
    artifact = update(before, node_r1(), r2, node_h1(), node_m1(), E_R1_H1, E_R2_M1, plan=plan)

    report = classified(before, artifact)
    assert classes(report) == {before.node(R2).spec_id: ("changed_meaning", "changed_evidence")}
    assert "stmt" in by_id(report)[before.node(R2).spec_id].causes


def test_a_deleted_document_removes_its_nodes_and_their_owner_edges() -> None:
    before = persisted()
    plan = plan_of([unchanged(A1), unchanged(A2)], documents=(DOC_A,), retired=(B1.fragment_id,))
    artifact = update(before, node_r1(), node_r2(), node_m1(), E_R2_M1, plan=plan)

    report = classified(before, artifact)
    h1, r1 = before.node(H1).spec_id, before.node(R1).spec_id
    assert classes(report) == {h1: ("removed",), r1: ("changed_meaning",)}  # R1 lost its owner Feature
    assert report.edges == (ChangedEdge(ref=ref(before, R1, "PART_OF", H1), change="removed", basis="stated"),)
    assert report.counts == {"changed_meaning": 1, "removed": 1} and report.edge_counts == {"removed": 1}


# Older artifacts and the build result


@pytest.mark.parametrize("scenario", ["merge", "deleted_document", "new_edges"])
def test_an_artifact_without_edge_changes_is_classified_through_the_registry_alike(scenario: str) -> None:
    before, artifact = older_scenario(scenario)
    assert artifact.edge_changes  # the scenario changes edges
    older = artifact.model_copy(update={"edge_changes": None})

    # An older artifact does not name the rule of an inferred edge it deleted.
    assert without_rules(classified(before, older)) == without_rules(classified(before, artifact))


def without_rules(report: ChangeReport) -> ChangeReport:
    return report.model_copy(update={"edges": tuple(edge.model_copy(update={"rule": None}) for edge in report.edges)})


def older_scenario(scenario: str) -> tuple[Persisted, DiffArtifact]:
    if scenario == "merge":
        m2_key = "Module|surface|order api"
        before = persisted(target=graph(*UNCHANGED, node(m2_key, "Order API", "Module", [(A1, 2, 2)])))
        before.replace_node(m2_key, created_at=OLDER)
        before.add_edge(R1, "PART_OF", M1, **inferred())
        merged = node_m1(aliases=(m2_key,))
        return before, update(before, node_r1(), node_r2(), node_h1(), merged, E_R1_H1, E_R2_M1)
    before = persisted()
    if scenario == "deleted_document":
        before.add_edge(R1, "RELATES_TO", H1, **inferred())
        plan = plan_of([unchanged(A1), unchanged(A2)], documents=(DOC_A,), retired=(B1.fragment_id,))
        return before, update(before, node_r1(), node_r2(), node_m1(), E_R2_M1, plan=plan)
    restated = E_R1_H1.model_copy(update={"fact": "Order export: FR-1."})
    return before, update(before, *UNCHANGED[:4], restated, edge(R1, "RELATES_TO", R2, [A1]))


def test_the_report_names_nodes_and_edges_by_spec_id_only() -> None:
    before = persisted()
    artifact = update(before, node_r1(name="Staff export orders"), *UNCHANGED[1:], edge(R1, "RELATES_TO", R2, [A1]))
    dumped = classified(before, artifact).model_dump_json()
    assert not any(text in dumped for text in (R1, R2, H1, M1, "orders", A1.fragment_id))


@pytest.fixture
def stores(tmp_path: Path) -> FileStores:
    return open_file_stores(tmp_path / "stores", corpus_folder=tmp_path / "corpus")


def run_context(stores: FileStores) -> BuildContext:
    return BuildContext(
        project_id="pytest-change-report",
        ontology=frozen_ontology(),
        driver=None,
        gateway=ScriptedGateway(),
        registry=stores.registry,
        corpus=stores.corpus,
        extraction=stores.extraction,
        builds=stores.builds,
        executor=OfflineExecutor(None),
    )


async def test_the_build_result_carries_the_change_report_of_a_persisted_build(stores: FileStores) -> None:
    before = persisted()
    ctx = run_context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    await runner.store_artifact(ctx, "diff", before.artifact)
    unpersisted = await runner._finish(ctx, runner._RunStats())
    assert unpersisted.change_report is None  # persist did not finish

    await runner.store_artifact(ctx, "persist", PersistCursor(done=True))
    result = await runner._finish(ctx, runner._RunStats())
    assert result.change_report is not None and result.change_report.counts == {"added": 4}
    stored = await stores.builds.get_result(ctx.project_id, ctx.build_id or "")
    assert stored is not None and stored.change_report == result.change_report

    failed = await runner._finish(ctx, runner._RunStats(), failed_step="verify", error=RuntimeError("boom"))
    assert failed.change_report is None
