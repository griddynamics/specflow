"""Acceptance tests of graph.gap: Ontology, rules and ownership (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Final

import test_graph_gap_ontology as ontology_checks
import test_graph_gap_rules as rules_checks
import test_graph_gap_schema as schema_checks
from gap_acceptance_support import Planted
from gap_answer_support import MEMBER, answer_world, service_of
from gap_close_support import answer_and_apply, apply_build, update_run
from gap_run_support import corpus_world
from graph.core import load_ontology
from graph.gap import GapService, run_gap
from graph.gap.models import AnswerInput
from graph.gap.prompts import RELEVANCE_PROMPT

BACKEND: Final = Path(__file__).resolve().parents[1]
GAP_TYPES = {"Gap", "Clarification", "ABOUT", "IMPACTS", "CITES", "FOLLOWS_UP", "ANSWERS", "PLACED_IN", "GROUNDED_IN"}


def test_fr_gont_01_load_ontology_merge_shipped_gap_ontology_file() -> None:
    """When called without arguments, then `Gap` and `Clarification` are owned by `gap`, the relations `ABOUT`,
    `IMPACTS`, `CITES`, `FOLLOWS_UP`, `ANSWERS`, `PLACED_IN`, `GROUNDED_IN`, `REPLACES` are owned by `gap`, and no
    build or plan type, relation or vocabulary differs from the files without gap.

    The checks of ``test_graph_gap_ontology`` (owners of every gap type and relation; build and plan parts equal
    with and without the gap file).
    """

    ontology = load_ontology()
    assert {name: ontology.owner_of(name) for name in GAP_TYPES | {"REPLACES"}} == dict.fromkeys(
        GAP_TYPES | {"REPLACES"}, "gap"
    )
    ontology_checks.test_fr_gont_01_the_merged_ontology_holds_gap_and_clarification_owned_by_gap()
    ontology_checks.test_fr_gont_01_no_build_or_plan_type_relation_or_vocabulary_differs_with_the_gap_file()


def test_fr_gont_02_installing_changing_gap_ontology_file_change_extraction() -> None:
    """Given a project built without gap, when gap is installed and an update build runs on unchanged documents, then
    the build reports no change.

    An update build reuses every stored extraction whose fingerprint is unchanged, so it reports no change exactly
    when the gap file changes no extraction input: the ontology fingerprint, the label and relation sets and the
    version are the same with and without the gap file (``test_graph_gap_ontology``).
    """

    ontology_checks.test_the_gap_file_changes_no_extraction_fingerprint_label_set_or_version()
    ontology_checks.test_the_gap_file_alone_does_not_change_the_fingerprint_either()


def test_fr_gont_03_load_gap_rules_validate_rules_against_merged(tmp_path) -> None:
    """Given rules naming an unknown type, an unknown relation and an unknown gap kind, then the error names all
    three.

    The case of ``test_graph_gap_rules`` on the merged ontology: one error lists every problem.
    """

    ontology = load_ontology()
    for name in ("one", "all"):
        (tmp_path / name).mkdir()
    rules_checks.test_fr_gont_03_an_unknown_type_relation_and_kind_are_all_named(tmp_path / "one", ontology)
    rules_checks.test_fr_gont_03_every_section_reports_its_problems_in_one_error(tmp_path / "all", ontology)


def test_fr_gont_04_graph_gap_reach_spec_types_relations_attributes() -> None:
    """Inspection: the structure test finds no build label, relation or attribute name as a literal under
    `graph.gap`.

    The structure test of ``test_graph_gap_rules`` over every module of the package.
    """

    rules_checks.test_fr_gont_04_no_spec_label_relation_or_attribute_is_a_literal_of_the_gap_modules()


async def test_fr_gont_05_ensure_gap_schema_create_graph_gap_constraint() -> None:
    """When it runs twice, then the second run returns `()`.

    Offline on the schema driver (``test_graph_gap_schema``); on Neo4j and ArcadeDB in
    ``test_graph_gap_schema_live``.
    """

    await schema_checks.test_fr_gont_05_ensure_gap_schema_creates_the_gap_objects_and_is_idempotent()


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


def test_fr_gown_03_graph_gap_import_graph_core_graph_library() -> None:
    """Inspection: the import contract test passes.

    The contracts of ``importlinter.toml`` (checked by ``make validate``) keep ``graph.gap`` off the build, the plan
    and the agent runtime; here every import of every module under ``graph.gap`` is checked too: of the backend's own
    packages it names only ``graph.gap``, ``graph.core`` and the central logger.
    """

    contracts = tomllib.loads((BACKEND / "importlinter.toml").read_text(encoding="utf-8"))["tool"]["importlinter"]
    ids = {contract.get("id") for contract in contracts["contracts"]}
    assert "gap-no-agent-runtime" in ids
    assert any(
        "graph.gap" in contract.get("source_modules", ()) and "graph.build" in contract.get("forbidden_modules", ())
        for contract in contracts["contracts"]
    )
    own = {path.stem if path.is_file() else path.name for path in (BACKEND / "src").iterdir()} - {"__pycache__"}
    allowed = ("graph.gap", "graph.core", "logger")
    package = BACKEND / "src" / "graph" / "gap"
    for module in sorted(package.rglob("*.py")):
        for name in _imported(module):
            if name.split(".")[0] in own:
                assert name == "graph" or name.startswith(allowed), (module.relative_to(package).as_posix(), name)


def _imported(path: Path) -> set[str]:
    """The absolute module names a module imports (``from x import y`` gives ``x``)."""

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


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
