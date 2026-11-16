"""Live: the API's gap runner with the real library on the live graph store and a migrated scratch database.

A returned build queues an ``update`` gap run (``after_build``); the run reads the shop corpus graph, writes its
gaps through the gap owner and its rows through ``PostgresGapRecords``; the runner's service lists the planted gaps
with their ``ABOUT`` ids, status and noise report are polled, and a second run with no document change makes no model
call (docs/graph/gap/api.md "Host runner"; FR-GRUN-01, FR-GRUN-03, FR-GWEB-01). Needs ``SPECFLOW_TEST_POSTGRES=1``
and a live graph store (``SPECFLOW_TEST_GRAPH_STORE``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.gap.runner import GapRunner, GapRunnerDependencies
from db import repositories as repo
from db.session import build_engine, build_session_factory, transaction
from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded, write_gap_corpus_graph
from gap_host_support import SETTINGS, store_build
from gap_investigation_support import answer_by_site
from graph.build import open_file_stores
from graph.core import load_ontology
from graph.gap import GapSettings
from graph.gap.prompts import INVESTIGATE_PROMPT
from graph_probes import requires_live_graph
from graph_support import GraphProject
from settings import DatabaseSettings
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = [requires_live("POSTGRES"), requires_live_graph()]


class _Principal:
    subject = "gap-live"
    email = "gap-live@example.com"
    name = "Gap live"


async def _project(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session, session.begin():
        user = await repo.upsert_user(session, _Principal())
        project = await repo.create_project(session, user.id, "Shop")
    return str(project.id)


def _gateways(made: list[RecordedGapGateway]) -> Callable[[Any], RecordedGapGateway]:
    def factory(_llm: Any) -> RecordedGapGateway:
        gateway = RecordedGapGateway().handle(INVESTIGATE_PROMPT, answer_by_site)  # the live packs differ offline
        made.append(gateway)
        return gateway

    return factory


async def test_fr_grun_01_the_runner_runs_the_library_after_a_build_on_live_stores(
    graph_project: GraphProject, migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        project_id = await _project(factory)
        graph_project.project_ids.append(project_id)  # cleaned up with the test's graph projects
        await write_gap_corpus_graph(graph_project, project_id)
        await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
        made: list[RecordedGapGateway] = []
        ontology = load_ontology()
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
        try:
            await runner.after_build(project_id, "build-1")
            await runner.wait_idle(project_id)
            first = await runner.status(project_id)
            assert first.status == "completed", first
            assert first.result is not None and first.result.build_id == "build-1"
            noise = await runner.noise(project_id, first.result.run_id)
            assert noise is not None and noise.sections

            page = await (await runner.service()).list_gaps(project_id, about=[Planted.STATED_QUESTION])
            assert len(page.gaps) == 1
            assert {node.spec_id for node in page.gaps[0].about} == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
            listed = await (await runner.service()).list_gaps(project_id, limit=200)
            assert len(listed.gaps) == first.result.counts["gaps_new"]["total"]

            await store_build(tmp_path, "build-2", minutes=5, project_id=project_id)
            await runner.after_build(project_id, "build-2")
            await runner.wait_idle(project_id)
            second = await runner.status(project_id)
            assert second.status == "completed" and second.result is not None
            assert second.result.build_id == "build-2"
            assert second.result.counts["sites"]["investigated"] == 0
            assert second.result.counts["gaps_new"]["total"] == 0
            assert made[-1].usage().total().calls == 0  # FR-GRUN-03: no model call on an unchanged corpus
            assert_all_recorded(*made)
        finally:
            await runner.aclose()
    finally:
        await engine.dispose()


async def _no_schema(*_args: Any) -> None:
    """The live graph fixture owns its schema; the runner's ``ensure_gap_schema`` is checked elsewhere."""
