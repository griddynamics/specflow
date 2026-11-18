"""Live: answers, apply, dismissal and a raised gap through the API's gap runner on the live graph store and a migrated
scratch database (docs/graph/gap/answers.md; FR-GANS-01, FR-GANS-03, FR-GANS-08, FR-GANS-11, FR-GANS-12).

A returned build queues the gap run (seed investigator: no model call); then the runner's ``GapService`` answers
option A of the cancel-paid-order gap (clarification on the graph store, a ``pending`` amendment row in PostgreSQL),
a stale answer is refused, the quiet-period apply makes the amendment ``active``, moves the gap to ``applying`` and
requests one update build; a dismissal deletes the node and stores the suppression; a raised gap gets options from
its ``site`` run. Needs ``SPECFLOW_TEST_POSTGRES=1`` and a live graph store (``SPECFLOW_TEST_GRAPH_STORE``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from api.gap.records import PostgresAmendments, PostgresGapRecords
from api.gap.runner import GapRunner, GapRunnerDependencies
from db.session import build_engine, build_session_factory, transaction
from gap_acceptance_support import Planted, write_gap_corpus_graph
from gap_host_support import SETTINGS, store_build
from gap_port_fakes import MemoryBuildRequests, SeedInvestigator
from graph.build import open_file_stores
from graph.core import load_ontology
from graph.gap import GapSettings
from graph.gap.errors import StaleGapError
from graph.gap.models import AnswerInput, NewGap
from graph_probes import requires_live_graph
from graph_support import GraphProject
from settings import DatabaseSettings
from specflow_live import requires_live
from test_api_gap_runner_live import _gateways, _no_schema, _project

pytest_plugins = ["graph_support"]

pytestmark = [requires_live("POSTGRES"), requires_live_graph()]

MEMBER = "user-member-2"


class SignalledBuilds(MemoryBuildRequests):
    """Build requests that set ``requested`` when one arrives (the apply ran)."""

    def __init__(self) -> None:
        super().__init__()
        self.requested = asyncio.Event()

    async def request_update_build(self, project_id: str, reason: str, *, immediate: bool) -> str:
        found = await super().request_update_build(project_id, reason, immediate=immediate)
        self.requested.set()
        return found


async def _check_witness_lines(service: Any, project_id: str, gap_id: str) -> None:
    """FR-GWEB-04: the witness lines name their document and section."""

    located = await service.get_gap(project_id, gap_id)
    assert all(line.document_id and line.heading_path for line in located.witness)


async def test_answer_apply_dismiss_and_raise_on_live_stores(
    graph_project: GraphProject, migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        project_id = await _project(factory)
        graph_project.project_ids.append(project_id)
        await write_gap_corpus_graph(graph_project, project_id)
        await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
        builds = SignalledBuilds()
        runner = GapRunner(
            SETTINGS.model_copy(update={"state_dir": str(tmp_path)}),
            GapSettings(),
            store=graph_project.store,
            transaction=lambda: transaction(factory),
            dependencies=GapRunnerDependencies(
                builds=open_file_stores(tmp_path).builds,
                ontology=load_ontology(),
                gateway_factory=_gateways([]),
                investigator=lambda _gateway: SeedInvestigator(),
                ensure=_no_schema,
            ),
        )
        runner.use_builds(builds)
        records = PostgresGapRecords(lambda: transaction(factory))
        amendments = PostgresAmendments(lambda: transaction(factory))
        try:
            await runner.after_build(project_id, "build-1")
            await runner.wait_idle(project_id)
            service = await runner.service()
            page = await service.list_gaps(project_id, about=[Planted.STATED_QUESTION])
            gap = await service.get_gap(project_id, page.gaps[0].gap_id)
            assert gap.options

            result = await service.answer_gap(
                project_id,
                AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER, updated_at=gap.updated_at),
            )

            answered = await service.get_gap(project_id, gap.gap_id)
            assert answered.status == "answered" and answered.clarification is not None
            assert (answered.clarification.clr_id, answered.clarification.answered_by) == (result.clr_id, MEMBER)
            rows = await amendments.for_clarification(project_id, result.clr_id)
            assert [row.status for row in rows] == ["pending"] and rows[0].text.endswith(f"> Clarified by {gap.gap_id}")
            with pytest.raises(StaleGapError):
                await service.answer_gap(
                    project_id,
                    AnswerInput(gap_id=gap.gap_id, option_rank=2, answered_by=MEMBER, updated_at=gap.updated_at),
                )

            runner.schedule_apply(project_id, datetime.now(UTC))  # the quiet period has passed

            await asyncio.wait_for(builds.requested.wait(), 10)
            assert builds.requests == [(project_id, "gap_answers", False)]
            assert (await service.get_gap(project_id, gap.gap_id)).status == "applying"
            assert [row.status for row in await amendments.for_clarification(project_id, result.clr_id)] == ["active"]
            await _check_witness_lines(service, project_id, gap.gap_id)

            other = await service.list_gaps(project_id, about=[Planted.VAGUE_REQUIREMENT])
            dismissed = other.gaps[0]
            await service.dismiss_gap(project_id, dismissed.gap_id, reason="not a gap", by=MEMBER)
            assert dismissed.signature in await records.suppressed(project_id, [dismissed.signature])
            assert not (await service.list_gaps(project_id, about=[Planted.VAGUE_REQUIREMENT])).gaps
            assert [e.event for e in await records.events(project_id, dismissed.gap_id)][-1] == "dismissed"

            raised = await service.raise_gap(
                project_id,
                NewGap(
                    kind="missing",
                    question="Must a refund of a cancelled order go back to the card it was paid with?",
                    about=(Planted.PLACE_ORDER, Planted.REFUND_INTERFACE),
                    origin="person",
                    created_by=MEMBER,
                ),
            )
            await runner.start_site(project_id, raised.gap_id)
            await runner.wait_idle(project_id)
            found = await service.get_gap(project_id, raised.gap_id)
            assert found.origin == "person" and len(found.options) >= 2
        finally:
            await runner.aclose()
    finally:
        await engine.dispose()
