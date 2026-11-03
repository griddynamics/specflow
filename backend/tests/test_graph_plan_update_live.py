"""Live plan updates from change reports: redone work, link-only changes, fallbacks planned again, cost."""

from __future__ import annotations

from pathlib import Path

from graph.core.db import Executor
from graph.core.errors import LLMError
from graph.plan.files import open_plan_store
from graph.plan.models import BuildChangeReport, ChangedSpecEdge, ChangedSpecNode
from graph.plan.prompts import PromptName, gateway_prompt
from graph.plan.run import StepFunctions, run_with
from graph.plan.snapshot import read_current_plan
from graph_plan_support import (
    live_context,
    plan_ontology,
    planned_live_shop,
    script_plan,
    shop_answers,
    shop_graph,
    shop_view,
    write_spec_graph,
)
from graph_support import Neo4jProject, ScriptedGateway
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
COMMIT = "12" * 20


async def _complete(project: Neo4jProject, project_id: str, task_id: str) -> None:
    """Mark a task completed as an agent's work would (raw test Cypher, prerequisites aside)."""

    await project.run(
        "MATCH (t:Task {group_id: $p, task_id: $t}) SET t.status = 'completed', t.commit = $c",
        p=project_id,
        t=task_id,
        c=COMMIT,
    )


def _calls(gateway: ScriptedGateway, planner: PromptName) -> int:
    return sum(1 for call in gateway.calls if call.prompt == gateway_prompt(planner))


async def test_a_meaning_change_redoes_finished_work_and_calls_only_its_area(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    await _complete(neo4j_project, project_id, PLACE)
    gateway = script_plan(ScriptedGateway(), shop_answers(await shop_view()))
    report = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        nodes=(ChangedSpecNode(spec_id="REQ::FR::PLACE_ORDER", classes=("changed_meaning",)),),
    )
    context = live_context(
        neo4j_project, project_id, open_plan_store(tmp_path), gateway, build_id="build-2", changes=(report,)
    )

    result = await run_with(context, StepFunctions())

    assert result.status == "succeeded" and result.checks is not None and result.checks.ok
    superseded = {item.task_id: item for item in result.changes.tasks_superseded}
    assert superseded[PLACE].successor == f"{PLACE}_R2" and superseded[PLACE].cause == "changed_meaning"
    assert result.stale_commits == {PLACE: COMMIT}
    current = await read_current_plan(Executor(neo4j_project.driver, neo4j_project.database), project_id)
    statuses = {task.task_id: task.status for task in current.tasks}
    assert statuses[PLACE] == "superseded" and statuses[f"{PLACE}_R2"] == "pending"
    assert any(link.type == "SUPERSEDES" and link.source == f"{PLACE}_R2" for link in current.links)
    # Cost: only the changed area is described and planned again; the project plan is reused.
    assert (_calls(gateway, "describe"), _calls(gateway, "project"), _calls(gateway, "tasks")) == (1, 0, 1)
    assert _calls(gateway, "review") == 1


async def test_a_link_only_change_keeps_finished_work(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    await _complete(neo4j_project, project_id, PLACE)
    await neo4j_project.run(
        "MATCH (r:SpecNode {group_id: $p, spec_id: 'REQ::FR::PLACE_ORDER'}), "
        "(e:SpecNode {group_id: $p, spec_id: 'ENT::ORDER'}) "
        "CREATE (r)-[:RELATES_TO {group_id: $p, edge_key: 'REQ_FR_PLACE_ORDER|RELATES_TO|ENT_ORDER', "
        "basis: 'inferred', confidence: 0.8}]->(e)",
        p=project_id,
    )
    report = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        edges=(
            ChangedSpecEdge(
                source="REQ::FR::PLACE_ORDER",
                relation="RELATES_TO",
                target="ENT::ORDER",
                change="added",
                basis="inferred",
            ),
        ),
    )
    gateway = script_plan(ScriptedGateway(), shop_answers(await shop_view()))
    context = live_context(
        neo4j_project, project_id, open_plan_store(tmp_path), gateway, build_id="build-2", changes=(report,)
    )

    result = await run_with(context, StepFunctions())

    assert result.status == "succeeded" and not result.unchanged
    assert result.changes.tasks_superseded == () and PLACE in result.changes.tasks_kept
    # The new edge touches an Orders service member and the Order entity: those two areas are planned again.
    assert (_calls(gateway, "describe"), _calls(gateway, "tasks")) == (2, 2)


async def test_a_fallback_is_planned_again_by_the_next_run(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id = neo4j_project.new_project()
    await write_spec_graph(neo4j_project, project_id, shop_graph(), plan_ontology())
    answers = shop_answers(await shop_view())
    store = open_plan_store(tmp_path)
    failing = script_plan(ScriptedGateway(), {**answers, ("tasks", "AREA::WEB_SHOP#1"): LLMError("timeout")})
    first = await run_with(live_context(neo4j_project, project_id, store, failing), StepFunctions())
    assert first.status == "partially_succeeded" and first.planning.tasks.fallbacks == 1

    gateway = script_plan(ScriptedGateway(), answers)
    report = BuildChangeReport(build_id="build-2", previous_build_id="build-1")
    second = await run_with(
        live_context(neo4j_project, project_id, store, gateway, build_id="build-2", changes=(report,)),
        StepFunctions(),
    )

    assert not second.unchanged and second.status == "succeeded"
    assert _calls(gateway, "tasks") == 1 and _calls(gateway, "describe") == 0
    assert second.checks is not None and second.checks.ok
