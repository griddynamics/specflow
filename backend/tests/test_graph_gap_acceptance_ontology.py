"""Acceptance tests of graph.gap: Ontology, rules and ownership (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import Planted, pending
from gap_answer_support import MEMBER, answer_world, service_of
from gap_close_support import answer_and_apply, apply_build, update_run
from gap_run_support import corpus_world
from graph.gap import GapService, run_gap
from graph.gap.models import AnswerInput
from graph.gap.prompts import RELEVANCE_PROMPT

GAP_TYPES = {"Gap", "Clarification", "ABOUT", "IMPACTS", "CITES", "FOLLOWS_UP", "ANSWERS", "PLACED_IN", "GROUNDED_IN"}


@pending("01", "covered by test_graph_gap_ontology")
def test_fr_gont_01_load_ontology_merge_shipped_gap_ontology_file() -> None:
    """When called without arguments, then `Gap` and `Clarification` are owned by `gap`, the relations `ABOUT`,
    `IMPACTS`, `CITES`, `FOLLOWS_UP`, `ANSWERS`, `PLACED_IN`, `GROUNDED_IN`, `REPLACES` are owned by `gap`, and no
    build or plan type, relation or vocabulary differs from the files without gap.
    """


@pending("01", "covered by test_graph_gap_ontology")
def test_fr_gont_02_installing_changing_gap_ontology_file_change_extraction() -> None:
    """Given a project built without gap, when gap is installed and an update build runs on unchanged documents, then
    the build reports no change.
    """


@pending("01", "covered by test_graph_gap_rules")
def test_fr_gont_03_load_gap_rules_validate_rules_against_merged() -> None:
    """Given rules naming an unknown type, an unknown relation and an unknown gap kind, then the error names all
    three.
    """


@pending("01", "covered by test_graph_gap_rules; reviewed again in 04")
def test_fr_gont_04_graph_gap_reach_spec_types_relations_attributes() -> None:
    """Inspection: the structure test finds no build label, relation or attribute name as a literal under
    `graph.gap`.
    """


@pending("01", "covered by test_graph_gap_schema and test_graph_gap_schema_live")
def test_fr_gont_05_ensure_gap_schema_create_graph_gap_constraint() -> None:
    """When it runs twice, then the second run returns `()`."""


async def test_fr_gown_01_graph_gap_write_types_ontology_file_never() -> None:
    """Given a full gap run, answers, an apply build and a revert on the shop corpus, then every build-owned node,
    relation and property written by `graph.gap` is none (store audit).

    A full run and an update run (scope, record); then, on the recorded loop, a full run with an AI answer, its
    apply, a simulated apply build with the update run's close check, and the revert of the resolved AI answer
    (person answers: FR-GOWN-02, FR-GOWN-04): every write of ``graph.gap`` names a gap type or relation, and no spec
    node or fragment changes except through the simulated build.
    """

    world = await corpus_world()
    specs = {spec_id: dict(node) for spec_id, node in world.executor.specs.items()}
    fragments = dict(world.executor.fragments)

    await run_gap(world.context(), mode="full")
    await run_gap(world.context())

    writes = [name for name, _ in world.executor.calls if name.startswith(("upsert_", "delete_"))]
    assert writes
    written = {name.split("_", 2)[-1] for name in writes}
    assert written <= GAP_TYPES, written - GAP_TYPES
    assert world.executor.specs == specs and world.executor.fragments == fragments

    rig = await answer_world(ai_answers=True)
    rig.world.gateway.handle(RELEVANCE_PROMPT, lambda _request: SPEC_SECTION)
    specs = {spec_id: dict(node) for spec_id, node in rig.world.executor.specs.items()}
    (answer,) = (await rig.service.ai_answers(rig.project_id)).answers
    await rig.service.apply_now(rig.project_id, by="gap-apply")
    assert rig.world.executor.specs == specs
    criteria = {Planted.OWNERLESS_CSV_EXPORT: {"acceptance_criteria": answer.clarification.text}}
    await update_run(rig.world, await apply_build(rig.world, attributes=criteria))
    assert (await rig.service.get_gap(rig.project_id, answer.gap_id)).resolution == "ai_answered"
    await rig.service.revert_clarification(rig.project_id, answer.clarification.clr_id, by=MEMBER, reason="No.")

    writes = [name for name, _ in rig.world.executor.calls if name.startswith(("upsert_", "delete_"))]
    written = {name.split("_", 2)[-1] for name in writes}
    assert written <= GAP_TYPES, written - GAP_TYPES


SPEC_SECTION = {"sections": [{"section": "S1", "relevance": "spec", "signal": "states behaviour"}]}


async def test_fr_gown_02_gap_query_graph_relational_bind_filter_project() -> None:
    """Given two projects with the same corpus, when project A runs, is answered and reverted, then nothing of
    project B is read or changed.

    Project A runs (full, then update), is read through ``GapService``, answered, applied and its answer reverted.
    Every graph statement binds the project id (the in-memory executor refuses another project's statement and checks
    the binding of each one); the relational fakes and the run store are shared and keyed by project.
    """

    first = await corpus_world(project_id="project-a")
    second = await corpus_world(project_id="project-b")
    second.records, second.store = first.records, first.store
    await run_gap(second.context(), mode="full")
    rows = (
        list(first.records.seeds["project-b"]),
        list(first.records.sites["project-b"]),
        dict(first.records.verdicts["project-b"]),
        list(first.records.event_rows["project-b"]),
    )
    graph_b = (dict(second.executor.nodes), dict(second.executor.edges))
    calls_b = len(second.executor.calls)

    await run_gap(first.context(), mode="full")
    await run_gap(first.context())
    service = GapService(first.executor, first.reader, first.ontology, first.rules, first.records, first.amendments)
    page = await service.list_gaps("project-a", limit=200)
    answering, builds, _, _ = service_of(first)
    gap = await answering.get_gap("project-a", page.gaps[0].gap_id)
    answered = await answering.answer_gap(
        "project-a", AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER, updated_at=gap.updated_at)
    )
    await answering.apply_now("project-a", by=MEMBER)
    reverted = await answering.revert_clarification("project-a", answered.clr_id, by=MEMBER, reason="Not yet.")

    assert (reverted.gap_id, reverted.follow_up) == (gap.gap_id, False)
    assert (await answering.get_gap("project-a", gap.gap_id)).status == "open"
    assert [request[0] for request in builds.requests] == ["project-a", "project-a"]
    assert page.gaps and {view.gap_id for view in page.gaps} == set(first.gap_nodes())
    assert rows == (
        list(first.records.seeds["project-b"]),
        list(first.records.sites["project-b"]),
        dict(first.records.verdicts["project-b"]),
        list(first.records.event_rows["project-b"]),
    )
    assert (dict(second.executor.nodes), dict(second.executor.edges)) == graph_b
    assert len(second.executor.calls) == calls_b
    latest = await first.store.latest_run("project-b")
    assert latest is not None and latest.run_id != (await first.store.latest_run("project-a")).run_id


@pending("01", "covered by the import contracts in importlinter.toml")
def test_fr_gown_03_graph_gap_import_graph_core_graph_library() -> None:
    """Inspection: the import contract test passes."""


async def test_fr_gown_04_options_amendments_runs_seeds_sites_verdicts_section() -> None:
    """Given a full run with answers, then the graph holds only `Gap` and `Clarification` gap nodes.

    A full run with an AI answer and a person's answer, applied: options, amendments, runs, seeds, sites, verdicts,
    section relevance, notes and events are rows of the ports; the graph's gap nodes are gaps and clarifications.
    """

    rig = await answer_world(ai_answers=True)
    await answer_and_apply(rig, await rig.cancel_gap(), option_rank=1)

    labels = {label for label, _ in rig.world.executor.nodes}
    assert labels == {"Gap", "Clarification"}
    records, project = rig.world.records, rig.project_id
    assert records.option_rows[project] and records.runs[project] and records.seeds[project]
    assert records.sites[project] and records.verdicts[project] and records.relevance_rows[project]
    assert records.notes[project] and records.event_rows[project]
    statuses = {row.status for row in rig.world.amendments.rows[project].values()}
    assert statuses == {"active"} and len(rig.world.amendments.rows[project]) == 2
    assert Planted.OWNERLESS_CSV_EXPORT in {
        spec_id for gap in rig.world.gap_nodes() for spec_id in rig.world.about(gap)
    }
