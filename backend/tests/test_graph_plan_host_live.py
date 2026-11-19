"""Live: the API plan runner passes the stored build change reports to a real plan run (FR-GPLN-01).

The shop fixture is planned through ``GraphPlanRunner`` with the real ``run_plan`` on a live store and scripted
planners; two builds are then stored with their change reports in the file build store, and the next plan run is an
``update`` that receives both and re-plans only the changed story.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from api.plan.runner import GraphPlanRunner, RunnerDependencies
from graph.build import BuildOptions, BuildRecord, BuildResult, open_file_stores
from graph.build.models import ChangedNode, ChangeReport
from graph.plan.models import PlanResult
from graph.plan.prompts import gateway_prompt
from graph.plan.run import run_plan
from graph.plan.snapshot import read_current_plan
from graph_plan_support import (
    plan_ontology,
    plan_rules,
    script_plan,
    shop_answers,
    shop_graph,
    shop_view,
    write_spec_graph,
)
from graph_probes import requires_live_graph
from graph_support import GraphProject, ScriptedGateway
from settings import GraphBuildSettings

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PLACE_REQUIREMENT = "REQ::FR::PLACE_ORDER"
PLACE_TASK = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
SETTINGS = GraphBuildSettings(
    LLM_GATEWAY_URL="https://gateway.example.test/openai",
    LLM_GATEWAY_API_KEY="gateway-key-3c6e",
    GRAPH_LLM_MODEL="main/model",
    GRAPH_SMALL_MODEL="small/model",
    GRAPH_EMBEDDING_MODEL="embed/model",
    GRAPH_EMBEDDING_DIM="8",
)


async def _build(root: Path, project_id: str, build_id: str, minutes: int, report: ChangeReport | None) -> None:
    builds = open_file_stores(root).builds
    started = T0 + timedelta(minutes=minutes)
    await builds.create_build(
        BuildRecord(
            build_id=build_id,
            project_id=project_id,
            mode="first" if report is None else "update",
            options=BuildOptions(),
            ontology_fingerprint="fp",
            label_set=(),
            embedding_model="embed/model",
            started_at=started,
        )
    )
    await builds.finish_build(
        BuildResult(
            project_id=project_id,
            build_id=build_id,
            mode="first" if report is None else "update",
            status="succeeded",
            started_at=started,
            finished_at=started + timedelta(minutes=1),
            change_report=report,
        )
    )


async def test_fr_gpln_01_two_builds_after_a_plan_update_it_and_keep_unchanged_tasks(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id = graph_project.new_project()
    await write_spec_graph(graph_project, project_id, shop_graph(), plan_ontology())
    await _build(tmp_path, project_id, "build-1", 0, None)
    answers = shop_answers(await shop_view())
    contexts: list[Any] = []
    results: list[PlanResult] = []
    gateways: list[ScriptedGateway] = []

    async def plan(context: Any) -> PlanResult:
        contexts.append(context)
        result = await run_plan(context)
        results.append(result)
        return result

    async def ensure(*_args: Any) -> None:
        return None

    ontology = plan_ontology()
    runner = GraphPlanRunner(
        SETTINGS.model_copy(update={"state_dir": str(tmp_path)}),
        store=graph_project.store,
        dependencies=RunnerDependencies(
            plan=plan,
            ontology=ontology,
            rules=plan_rules(ontology),
            ensure=ensure,
            gateway_factory=lambda _llm: gateways.append(script_plan(ScriptedGateway(), answers)) or gateways[-1],
        ),
    )

    await runner.start(project_id)
    await _settle(runner)
    first = await read_current_plan(graph_project.executor, project_id)
    await _build(
        tmp_path,
        project_id,
        "build-2",
        5,
        ChangeReport(nodes=(ChangedNode(spec_id="ENT::ORDER", label="Entity", classes=("changed_evidence",)),)),
    )
    await _build(
        tmp_path,
        project_id,
        "build-3",
        10,
        ChangeReport(
            nodes=(ChangedNode(spec_id=PLACE_REQUIREMENT, label="FunctionalRequirement", classes=("changed_meaning",)),)
        ),
    )
    await runner.start(project_id)
    await _settle(runner)
    await runner.aclose()

    assert [result.mode for result in results] == ["first", "update"]
    second_context, second = contexts[1], results[1]
    assert second_context.replan is False
    assert [(report.previous_build_id, report.build_id) for report in second_context.changes] == [
        ("build-1", "build-2"),
        ("build-2", "build-3"),
    ]
    assert second.status == "succeeded" and not second.unchanged
    current = await read_current_plan(graph_project.executor, project_id)
    assert current.plan is not None and current.plan.last_build_id == "build-3"
    before = {task.task_id: task for task in first.tasks}
    after = {task.task_id: task for task in current.tasks}
    # Only the part holding the changed requirement is planned again; the evidence-only report changes nothing.
    tasks_calls = [call for call in gateways[1].calls if call.prompt == gateway_prompt("tasks")]
    assert len(tasks_calls) == 1
    assert second.changes.tasks_superseded == ()
    for task_id, task in before.items():  # every task outside the changed story keeps its id and revision
        if task.story_id != before[PLACE_TASK].story_id:
            assert task_id in after and after[task_id].revision == task.revision


async def _settle(runner: GraphPlanRunner) -> None:
    for run in list(runner._runs.values()):
        await run.task
