"""End to end on real build output: build shop-v2, plan it, work a task, rebuild, plan the update."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from graph.build.models import BuildResult
from graph.core import load_ontology
from graph.plan.answers import (
    DescribeAnswer,
    DoneWhenAnswer,
    InterfaceAnswer,
    ProfileAnswer,
    ProjectAnswer,
    ReviewAnswer,
    StorySummaryAnswer,
    TaskAnswer,
    TasksAnswer,
)
from graph.plan.files import open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.models import PlanResult
from graph.plan.prompts import gateway_prompt
from graph.plan.rules import PlanRules, load_plan_rules
from graph.plan.run import StepFunctions, run_with
from graph_plan_support import change_reports, live_context, planner_payload
from graph_support import Neo4jProject, ScriptedGateway, ScriptedRequest, open_neo4j_project
from graph_v2_support import ShopV2Builds
from specflow_live import requires_live

pytestmark = [requires_live("NEO4J"), pytest.mark.asyncio(loop_scope="module")]

COMMIT = "ef" * 20


def _build_gateways(config: Any = None, **options: Any) -> ScriptedGateway:
    """The build's gateway factory: a scripted gateway answering from shop-v2's hand-written answers."""

    return ScriptedGateway(config, **options)


def generic_planners(rules: PlanRules) -> ScriptedGateway:
    """Planners that answer any project from the request itself: one task per home story and phase."""

    gateway = ScriptedGateway()

    def describe(request: ScriptedRequest) -> DescribeAnswer:
        payload = planner_payload(request)
        return DescribeAnswer(
            title=f"Area {payload['area']['area_id']}",
            description="What the area builds.",
            interfaces=[
                InterfaceAnswer(spec_id=item["spec_id"], name="Interface", kind=item["kind"], summary="", spec_ids=[])
                for item in payload["interfaces"]
            ],
            stories=[
                StorySummaryAnswer(story_id=item["story_id"], summary="Delivers it.") for item in payload["stories"]
            ],
        )

    def project(request: ScriptedRequest) -> ProjectAnswer:
        members = [item["spec_id"] for item in planner_payload(request)["members"]]
        return ProjectAnswer(
            profile=ProfileAnswer(
                languages=["python"],
                frameworks=[],
                layout="src",
                test_command="pytest",
                test_paths=[],
                spec_ids=[],
                conventions=[],
            ),
            setup_tasks=[_task("WORKSPACE", "setup", [], []), _task("TOOLCHAIN", "setup", members, ["WORKSPACE"])],
            closing_tasks=[_task("END_TO_END", "acceptance", [], [])],
            setup_summary="Setup.",
            closing_summary="Closing.",
        )

    def tasks(request: ScriptedRequest) -> TasksAnswer:
        payload = planner_payload(request)
        number = payload["part_id"].rsplit("#", 1)[1]
        groups: dict[tuple[str, str], list[str]] = {}
        for member in payload["members"]:
            phase = rules.rule_of(member["label"]).phase or "logic"
            groups.setdefault((member["home_story"], phase), []).append(member["spec_id"])
        return TasksAnswer(
            tasks=[
                _task(f"{phase.upper()}_{number}_{index}", phase, implements, [], story=story)
                for index, ((story, phase), implements) in enumerate(sorted(groups.items()), start=1)
            ],
            not_buildable=[],
        )

    gateway.handle(gateway_prompt("describe"), describe)
    gateway.handle(gateway_prompt("project"), project)
    gateway.handle(gateway_prompt("tasks"), tasks)
    gateway.handle(gateway_prompt("review"), lambda _request: ReviewAnswer(issues=[]))
    return gateway


def _task(action: str, phase: str, implements: list[str], waits: list[str], *, story: str = "") -> TaskAnswer:
    return TaskAnswer(
        story=story,
        action=action,
        phase=phase,
        title=f"Build {action.lower()}",
        description="What to build.",
        implements=implements,
        waits_for=waits,
        done_when=[DoneWhenAnswer(text=f"{action} works", source="planner")],
        complexity="medium",
    )


@dataclass
class Planned:
    project: Neo4jProject
    builds: ShopV2Builds
    rules: PlanRules
    store_root: Path
    first_build: BuildResult
    first: PlanResult


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def planned(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Planned]:
    """shop-v2 snapshot 1 built with inference, then planned with the generic planners."""

    async with open_neo4j_project() as project:
        builds = ShopV2Builds(project, _build_gateways, Path(tmp_path_factory.mktemp("shop-v2-plan")))
        first_build, _ = await builds.build(1)
        rules = load_plan_rules(ontology=load_ontology())
        store_root = Path(tmp_path_factory.mktemp("plans"))
        context = live_context(
            project,
            builds.project_id,
            open_plan_store(store_root),
            generic_planners(rules),
            build_id=first_build.build_id,
            ontology=load_ontology(),
            rules=rules,
        )
        first = await run_with(context, StepFunctions())
        yield Planned(project, builds, rules, store_root, first_build, first)


async def test_the_first_plan_of_a_real_build_passes_every_check(planned: Planned) -> None:
    result = planned.first

    assert result.mode == "first" and result.status == "succeeded", result.failure
    assert result.checks is not None and result.checks.ok, result.checks.problems
    assert result.checks.metrics.ready == 1
    assert result.metrics.areas >= 2 and result.changes.tasks_added


async def test_work_then_an_unchanged_rebuild_plans_nothing(planned: Planned) -> None:
    project_id = planned.builds.project_id
    ontology = load_ontology()
    life = TaskLifecycle(planned.project.driver, ontology, planned.rules, database=planned.project.database)
    claim = await life.claim_task(project_id, "TASK::PROJECT::WORKSPACE", claimed_by="agent")
    await life.complete_task(project_id, "TASK::PROJECT::WORKSPACE", claim_token=claim.claim_token, commit=COMMIT)
    rebuild, _ = await planned.builds.build(1)
    gateway = generic_planners(planned.rules)
    context = live_context(
        planned.project,
        project_id,
        open_plan_store(planned.store_root),
        gateway,
        build_id=rebuild.build_id,
        changes=change_reports([planned.first_build, rebuild])[1:],
        ontology=ontology,
        rules=planned.rules,
    )

    result = await run_with(context, StepFunctions())

    assert result.mode == "update" and result.status == "succeeded", result.failure
    assert result.unchanged and gateway.calls == []
    assert result.changes.tasks_superseded == ()


async def test_a_reworded_snapshot_reuses_every_answer_and_keeps_finished_work(planned: Planned) -> None:
    """shop-v2 snapshot 2 rewords documents: its report classes nodes ``changed_evidence`` only and changes
    edges, so every planner answer is reused, no task is redone and finished work stays."""

    project_id = planned.builds.project_id
    ontology = load_ontology()
    store = open_plan_store(planned.store_root)
    previous = await store.get_revisions(project_id)
    second, _ = await planned.builds.build(2)
    reports = tuple(
        report.model_copy(update={"previous_build_id": previous.consumed[-1]}) for report in change_reports([second])
    )
    context = live_context(
        planned.project,
        project_id,
        store,
        generic_planners(planned.rules),
        build_id=second.build_id,
        changes=reports,
        ontology=ontology,
        rules=planned.rules,
    )

    result = await run_with(context, StepFunctions())

    assert result.mode == "update" and result.status == "succeeded", result.failure
    assert result.checks is not None and result.checks.ok, result.checks.problems
    classes = {item for report in reports for node in report.nodes for item in node.classes}
    assert classes == {"changed_evidence"}, classes
    planning = result.planning
    assert (planning.describe.calls, planning.project.calls, planning.tasks.calls) == (0, 0, 0)
    assert planning.tasks.reused == planned.first.planning.tasks.calls
    assert result.changes.tasks_superseded == () and "TASK::PROJECT::WORKSPACE" in result.changes.tasks_kept
