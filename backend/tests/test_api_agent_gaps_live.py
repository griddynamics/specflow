"""Live: the plan's findings port and ``list_gaps`` over a real gap run on the live graph store and a migrated scratch
database (FR-GPLN-02, FR-GWEB-03; docs/graph/gap/lifecycle.md "Plan link").

The API's gap runner runs the real library after a build of the shop gap corpus (as ``test_api_gap_runner_live``);
then the host's ``OpenGaps`` over that runner fills a ``PlanService`` over the claim tests' mini plan whose task
``TASK::A::API_LOGIC`` implements ``REQ::FR::PLACE_ORDER``, reading the spec nodes from the live store. The task
brief's findings (as the agent route reads them), the story brief, the severities and the ``list_gaps`` read of the
story's nodes all show the open gap of the stated question. Needs ``SPECFLOW_TEST_POSTGRES=1`` and a live graph store
(``SPECFLOW_TEST_GRAPH_STORE``).
"""

from __future__ import annotations

from pathlib import Path

from api.gap.findings import OpenGaps
from api.gap.runner import GapRunner, GapRunnerDependencies
from db.session import build_engine, build_session_factory, transaction
from gap_acceptance_support import Planted, write_gap_corpus_graph
from gap_agent_support import API_LOGIC, API_STORY
from gap_host_support import SETTINGS, store_build
from graph.build import open_file_stores
from graph.core import GraphReader, load_ontology
from graph.core.store import as_executor
from graph.gap import GapSettings
from graph.plan import PlanService
from graph.plan.findings_port import open_findings_about
from graph_plan_support import plan_rules
from graph_probes import requires_live_graph
from graph_support import GraphProject
from plan_claims_support import mini_plan
from settings import DatabaseSettings
from specflow_live import requires_live
from test_api_gap_runner_live import _gateways, _no_schema, _project

pytest_plugins = ["graph_support"]

pytestmark = [requires_live("POSTGRES"), requires_live_graph()]


async def test_briefs_and_list_gaps_show_the_open_gap_on_live_stores(
    graph_project: GraphProject, migrated_database: DatabaseSettings, tmp_path: Path
) -> None:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        project_id = await _project(factory)
        graph_project.project_ids.append(project_id)
        await write_gap_corpus_graph(graph_project, project_id)
        await store_build(tmp_path, "build-1", minutes=0, project_id=project_id)
        ontology = load_ontology()
        runner = GapRunner(
            SETTINGS.model_copy(update={"state_dir": str(tmp_path)}),
            GapSettings(),
            store=graph_project.store,
            transaction=lambda: transaction(factory),
            dependencies=GapRunnerDependencies(
                builds=open_file_stores(tmp_path).builds,
                ontology=ontology,
                gateway_factory=_gateways([]),
                ensure=_no_schema,
            ),
        )
        try:
            await runner.after_build(project_id, "build-1")
            await runner.wait_idle(project_id)
            assert (await runner.status(project_id)).status == "completed"

            port = OpenGaps(None, lambda: runner)
            store = mini_plan(project_id)
            store.set_task(API_LOGIC, implements=[Planted.PLACE_ORDER])
            graph = GraphReader(as_executor(graph_project.store), ontology)
            plan = PlanService(store, ontology, plan_rules(ontology), graph=graph, findings=port)

            brief = await plan.task_brief(project_id, API_LOGIC)
            found = await open_findings_about(port, project_id, [node.spec_id for node in brief.implements])
            story = await plan.story_brief(project_id, API_STORY)
            severities = await port.severities(project_id, found)
            service = await runner.service()
            listed = await service.list_gaps(project_id, about=list(await plan.story_nodes(project_id, API_STORY)))
        finally:
            await runner.aclose()
    finally:
        await engine.dispose()

    question = next(item for item in found if Planted.STATED_QUESTION in item.about_spec_ids)  # the live loop's words
    assert question.text and Planted.PLACE_ORDER in question.about_spec_ids
    assert severities[question.finding_id] in {"blocker", "major", "minor"}
    assert question.finding_id in {item.finding_id for item in story.findings}
    assert question.finding_id in {gap.gap_id for gap in listed.gaps}
