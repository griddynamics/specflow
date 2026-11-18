"""Live: an AI answer, its apply and its revert through the API's gap runner on the live graph store and a migrated
scratch database (docs/graph/gap/answers.md "AI answers", "Review list", "Revert"; FR-GRTE-03, FR-GANS-10).

A returned build queues the gap run (the structured loop answered by site, the recorded second check): the CSV export
gap is answered by AI (an ``ai`` clarification on the graph store, a ``pending`` amendment row in PostgreSQL), staged
like a person's answer: the run requests no build. The review list shows it; "Resolve gaps" (``apply_now``) makes the
row ``active``; a revert of the pending answer reopens the gap for a person and requests a build. A second answer,
resolved as the close check would leave it, is reverted into a follow-up gap that ``FOLLOWS_UP`` it with its question
and options. Needs ``SPECFLOW_TEST_POSTGRES=1`` and a live graph store (``SPECFLOW_TEST_GRAPH_STORE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.gap.records import PostgresAmendments
from api.gap.runner import GapRunner, GapRunnerDependencies
from db.session import build_engine, build_session_factory, transaction
from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded, write_gap_corpus_graph
from gap_host_support import SETTINGS, store_build
from gap_port_fakes import MemoryBuildRequests
from graph.build import open_file_stores
from graph.core import load_ontology
from graph.core.writer import OwnedWriter
from graph.gap import GapSettings
from graph.gap.gaps import clarification_uuid, gap_uuid
from graph.gap.models import AnswerInput, GapView
from graph.gap.names import CLARIFICATION, GAP, OWNER
from graph_probes import requires_live_graph
from graph_support import GraphProject
from settings import DatabaseSettings
from specflow_live import requires_live
from test_api_gap_runner_live import _gateways, _no_schema, _project

pytest_plugins = ["graph_support"]

pytestmark = [requires_live("POSTGRES"), requires_live_graph()]

MEMBER = "user-member-2"


async def test_an_ai_answer_is_listed_applied_and_reverted_on_live_stores(
    graph_project: GraphProject, migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        project_id = await _project(factory)
        graph_project.project_ids.append(project_id)
        await write_gap_corpus_graph(graph_project, project_id)
        await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
        made: list[RecordedGapGateway] = []
        ontology = load_ontology()
        builds = MemoryBuildRequests()
        runner = GapRunner(
            SETTINGS.model_copy(update={"state_dir": str(tmp_path)}),
            GapSettings(),
            store=graph_project.store,
            transaction=lambda: transaction(factory),
            dependencies=GapRunnerDependencies(
                builds=open_file_stores(tmp_path).builds,
                ontology=ontology,
                gateway_factory=_gateways(made),
                ensure=_no_schema,
            ),
        )
        runner.use_builds(builds)
        amendments = PostgresAmendments(lambda: transaction(factory))
        try:
            await runner.after_build(project_id, "build-1")
            await runner.wait_idle(project_id)
            status = await runner.status(project_id)
            assert status.result is not None and status.result.counts["ai_answered"] == 1
            assert builds.requests == []
            service = await runner.service()

            (listed,) = (await service.ai_answers(project_id)).answers
            gap = await service.get_gap(project_id, listed.gap_id)
            assert {node.spec_id for node in gap.about} == {Planted.OWNERLESS_CSV_EXPORT}
            assert (gap.status, gap.route, listed.clarification.mode) == ("answered", "ai", "ai")
            assert listed.clarification.confidence == 0.92 and listed.clarification.basis
            clr = listed.clarification.clr_id
            assert [row.status for row in await amendments.for_clarification(project_id, clr)] == ["pending"]

            await service.apply_now(project_id, by=MEMBER)
            assert [row.status for row in await amendments.for_clarification(project_id, clr)] == ["active"]
            reverted = await service.revert_clarification(project_id, clr, by=MEMBER, reason="A person decides.")

            assert (reverted.gap_id, reverted.follow_up) == (gap.gap_id, False)
            assert builds.requests[-1] == (project_id, "gap_revert", False)
            reopened = await service.get_gap(project_id, gap.gap_id)
            assert (reopened.status, reopened.route, reopened.clarification) == ("open", "ask", None)
            assert [row.status for row in await amendments.for_clarification(project_id, clr)] == ["reverted"]

            await _revert_a_resolved_answer(graph_project, ontology, service, project_id, gap)
            assert_all_recorded(*made)
        finally:
            await runner.aclose()
    finally:
        await engine.dispose()


async def _revert_a_resolved_answer(
    graph_project: GraphProject, ontology: Any, service: Any, project_id: str, gap: GapView
) -> None:
    """A person's answer, resolved as the close check leaves a fixed gap, reverted into a follow-up."""

    again = await service.answer_gap(project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))
    await service.apply_now(project_id, by=MEMBER)
    writer = OwnedWriter(graph_project.store, ontology, OWNER, project_id)
    async with writer.transaction() as tx:  # as the close check leaves a fixed gap
        await tx.upsert_node(GAP, gap_uuid(project_id, gap.gap_id), {"status": "resolved"})
        await tx.upsert_node(CLARIFICATION, clarification_uuid(project_id, again.clr_id), {"status": "applied"})
    follow = await service.revert_clarification(project_id, again.clr_id, by=MEMBER, reason="Not this one.")

    assert follow.follow_up and follow.gap_id != gap.gap_id
    follow_up = await service.get_gap(project_id, follow.gap_id)
    assert (follow_up.status, follow_up.route, follow_up.question) == ("open", "ask", gap.question)
    assert [option.label for option in follow_up.options] == [option.label for option in gap.options]
    assert follow_up.history is not None
    assert [(item.gap_id, item.reason) for item in follow_up.history.follow_ups] == [(gap.gap_id, "reverted")]
    assert {node.spec_id for node in follow_up.about} == {Planted.OWNERLESS_CSV_EXPORT}
