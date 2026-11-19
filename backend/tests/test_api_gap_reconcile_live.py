"""Live: reconcile through the API's gap runner on the live graph store and a migrated scratch database
(docs/graph/gap/reconcile.md; FR-GREC-01, FR-GREC-05, FR-GREC-07).

A returned build queues the gap run (seed investigator); option A of the cancel-paid-order gap is answered and applied
(the amendment ``active`` in PostgreSQL). The corpus reader's call ``corpus_answers`` then meets a re-upload of
orders-spec.md whose rule changed: the previous text comes from the graph store's fragments, the same-meaning judge
(a scripted ``gap_judge`` answer) finds a different meaning, the amendment becomes ``suspended`` in PostgreSQL with a
``reconciled`` event on the gap, and the build reads the upload (GD3). A dry run stores nothing. Needs
``SPECFLOW_TEST_POSTGRES=1`` and a live graph store (``SPECFLOW_TEST_GRAPH_STORE``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.gap.records import PostgresAmendments, PostgresGapRecords
from api.graph_build.corpus import with_answers
from db.session import build_engine, build_session_factory, transaction
from gap_acceptance_support import REUPLOADS, Planted, RecordedGapGateway, corpus_revisions, write_gap_corpus_graph
from gap_host_support import store_build
from graph.gap.models import AnswerInput
from graph.gap.prompts import RECONCILE_PROMPT
from graph_probes import requires_live_graph
from graph_support import GraphProject
from settings import DatabaseSettings
from specflow_live import requires_live
from test_api_gap_runner_live import _project, live_runner

pytest_plugins = ["graph_support"]

pytestmark = [requires_live("POSTGRES"), requires_live_graph()]

MEMBER = "user-member-2"
ORDERS_LINEAGE = "upload:orders-spec"


def _judging(made: list[Any]) -> Any:
    def factory(_llm: Any) -> RecordedGapGateway:
        gateway = RecordedGapGateway().handle(
            RECONCILE_PROMPT, lambda _request: {"verdict": "different", "confidence": 0.9, "reason": "fee"}
        )
        made.append(gateway)
        return gateway

    return factory


async def test_a_changed_upload_suspends_the_answer_on_live_stores(
    graph_project: GraphProject, migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    made: list[Any] = []
    try:
        project_id = await _project(factory)
        graph_project.project_ids.append(project_id)
        await write_gap_corpus_graph(graph_project, project_id)
        await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
        runner, _builds = live_runner(graph_project, factory, tmp_path, _judging(made))
        records = PostgresGapRecords(lambda: transaction(factory))
        amendments = PostgresAmendments(lambda: transaction(factory))
        try:
            await runner.after_build(project_id, "build-1")
            await runner.wait_idle(project_id)
            service = await runner.service()
            page = await service.list_gaps(project_id, about=[Planted.STATED_QUESTION])
            gap = page.gaps[0]
            result = await service.answer_gap(
                project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER)
            )
            await service.apply_now(project_id, by=MEMBER)
            (row,) = await amendments.for_clarification(project_id, result.clr_id)
            assert row.status == "active"

            upload = next(case for case in REUPLOADS if case.name == "changed-rule").markdown
            revisions = [
                item.model_copy(update={"markdown": upload, "revision_id": "REV::UPLOADED"})
                if item.lineage_id == ORDERS_LINEAGE
                else item
                for item in corpus_revisions()
            ]
            made_before = len(made)
            dry = await runner.corpus_answers(project_id, revisions, dry_run=True)
            assert len(made) == made_before  # no judge in a dry run
            assert [item.status for item in await amendments.for_clarification(project_id, result.clr_id)] == ["active"]
            assert dry.amendments[ORDERS_LINEAGE][0].operation == "insert_after"  # undecided: kept applied

            read = await runner.corpus_answers(project_id, revisions)

            orders = next(
                item for item in with_answers(project_id, revisions, read) if item.lineage_id == ORDERS_LINEAGE
            )
            assert orders.markdown == upload  # GD3: the build reads the upload
            (stored,) = await amendments.for_clarification(project_id, result.clr_id)
            assert (stored.status, stored.last_revision_id) == ("suspended", "REV::UPLOADED")
            assert stored.anchor_text == "Paid orders can be cancelled until the order ships. A 5% fee is kept."
            events = [item for item in await records.events(project_id, gap.gap_id) if item.event == "reconciled"]
            assert [item.detail["outcome"] for item in events] == ["conflict"]
            assert made and all(gateway.calls_of("gap_judge") for gateway in made[made_before:])
            assert events[0].at <= datetime.now(UTC)
        finally:
            await runner.aclose()
    finally:
        await engine.dispose()
