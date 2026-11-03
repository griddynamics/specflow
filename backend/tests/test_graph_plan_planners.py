"""Planner runs: reuse, one repair round, fallback, fatal errors, and the Describe, Project and Tasks steps."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from graph.core.errors import LLMError, LLMFatalError
from graph.plan.answers import DescribeAnswer, TasksAnswer
from graph.plan.describe import describe
from graph.plan.fallback import rules_tasks, tasks_fallback
from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.models import (
    CurrentPlan,
    CurrentTask,
    DescribeArtifact,
    DoneWhen,
    PlanInfo,
    PlanOptions,
    ProjectArtifact,
    TasksArtifact,
)
from graph.plan.names import Phase, PlanMode
from graph.plan.project import project
from graph.plan.prompts import gateway_prompt
from graph.plan.tasks import plan_tasks
from graph.plan.view import PlanView
from graph_plan_support import planning_context, script_plan, shop_answers, shop_view
from graph_support import ScriptedGateway

STAMP = datetime(2026, 1, 1, tzinfo=UTC)
ORDERS_PART = "AREA::ORDERS_SERVICE#1"


def _store(tmp_path: Path) -> FilePlanStore:
    return open_plan_store(tmp_path / "plans")


async def _plan_all(
    view: PlanView, gateway: ScriptedGateway, store: FilePlanStore, *, run_id: str = "run-1", replan: bool = False
) -> tuple[DescribeArtifact, ProjectArtifact, TasksArtifact]:
    ctx = planning_context(view, store, gateway, run_id=run_id, replan=replan)
    mode: PlanMode = "replan" if replan else view.snapshot.mode
    described = await describe(ctx, view, mode=mode)
    projected = await project(ctx, view, described, mode=mode)
    tasks = await plan_tasks(ctx, view, described, projected, mode=mode)
    return described, projected, tasks


async def test_a_first_plan_calls_every_planner_once_and_stores_the_answers(tmp_path: Path) -> None:
    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), shop_answers(view))
    store = _store(tmp_path)

    described, projected, tasks = await _plan_all(view, gateway, store)

    prompts = [call.prompt for call in gateway.calls]
    assert prompts.count(gateway_prompt("describe")) == len(view.active_areas)
    assert prompts.count(gateway_prompt("project")) == 1
    assert prompts.count(gateway_prompt("tasks")) == len(view.partition.parts)
    assert {call.call_class for call in gateway.calls} == {"extraction"}
    assert all(outcome.calls == 1 and not outcome.fallback for outcome in described.outcomes.values())
    assert described.areas["AREA::ORDERS_SERVICE"].interfaces[0].spec_id == "IFC::ORDERS_API"
    assert described.areas["AREA::ORDERS_SERVICE"].interfaces[0].planned_by == "rules"
    assert described.story_summaries["STORY::ORDERS_SERVICE::PLACE_ORDER"] == "Delivers the story."
    assert [task.action for task in projected.setup] == ["WORKSPACE", "TOOLCHAIN"]
    assert projected.setup[1].waits == ("TASK::PROJECT::WORKSPACE",)
    assert projected.profile.conventions[0].spec_ids == ("CON::EU_REGION",)
    assert all(task.planned_by == "model" and task.prompt_ref == "tasks.1" for task in tasks.tasks)
    place_order = next(task for task in tasks.tasks if "REQ::FR::PLACE_ORDER" in task.implements)
    assert place_order.release == ("MVP",)
    stored = list((tmp_path / "plans" / "shop" / "plan-answers").glob("*/*.json.gz"))
    assert len(stored) == len(view.active_areas) + 1 + len(view.partition.parts)


async def test_a_second_run_with_the_same_keys_reuses_every_answer(tmp_path: Path) -> None:
    view = await shop_view()
    store = _store(tmp_path)
    await _plan_all(view, script_plan(ScriptedGateway(), shop_answers(view)), store)
    second = ScriptedGateway()

    described, projected, tasks = await _plan_all(view, second, store, run_id="run-2")

    assert second.calls == []
    assert all(
        outcome.reused for outcome in (*described.outcomes.values(), projected.outcome, *tasks.outcomes.values())
    )


async def test_a_replan_reuses_only_its_own_runs_answers(tmp_path: Path) -> None:
    view = await shop_view()
    store = _store(tmp_path)
    await _plan_all(view, script_plan(ScriptedGateway(), shop_answers(view)), store)

    other_run = script_plan(ScriptedGateway(), shop_answers(view))
    await _plan_all(view, other_run, store, run_id="run-2", replan=True)
    assert len(other_run.calls) == len(view.active_areas) + 1 + len(view.partition.parts)

    same_run = ScriptedGateway()
    await _plan_all(view, same_run, store, run_id="run-2", replan=True)
    assert same_run.calls == []


async def test_an_invalid_answer_is_sent_back_once_with_its_problems(tmp_path: Path) -> None:
    view = await shop_view()
    answers = shop_answers(view)
    valid = answers[("tasks", ORDERS_PART)]
    assert isinstance(valid, TasksAnswer)
    broken = valid.model_copy(
        update={"tasks": [valid.tasks[0].model_copy(update={"phase": "deploy"}), *valid.tasks[1:]]}
    )
    gateway = script_plan(ScriptedGateway(), {**answers, ("tasks", ORDERS_PART): [broken, valid]})

    _, _, tasks = await _plan_all(view, gateway, _store(tmp_path))

    calls = [
        call for call in gateway.calls if call.prompt == gateway_prompt("tasks") and ORDERS_PART in str(call.messages)
    ]
    assert len(calls) == 2
    assert "invalid_phase:" in calls[1].messages[-1]["content"]
    assert tasks.outcomes[ORDERS_PART].calls == 2 and tasks.outcomes[ORDERS_PART].invalid == 1
    assert not tasks.outcomes[ORDERS_PART].fallback


async def test_an_answer_that_stays_invalid_falls_back_and_is_never_stored(tmp_path: Path) -> None:
    view = await shop_view()
    answers = shop_answers(view)
    valid = answers[("tasks", ORDERS_PART)]
    assert isinstance(valid, TasksAnswer)
    broken = valid.model_copy(update={"tasks": []})
    gateway = script_plan(ScriptedGateway(), {**answers, ("tasks", ORDERS_PART): broken})
    store = _store(tmp_path)

    _, _, tasks = await _plan_all(view, gateway, store)

    outcome = tasks.outcomes[ORDERS_PART]
    assert outcome.fallback and outcome.calls == 2 and outcome.invalid == 2
    orders = [task for task in tasks.tasks if task.area_id == "AREA::ORDERS_SERVICE"]
    assert orders and all(task.planned_by == "rules" for task in orders)
    stored = list((tmp_path / "plans" / "shop" / "plan-answers" / "tasks").glob("*.json.gz"))
    assert len(stored) == len(view.partition.parts) - 1


@pytest.mark.parametrize("failure", [LLMError("malformed"), LLMError("timeout")])
async def test_a_non_fatal_model_error_falls_back(tmp_path: Path, failure: LLMError) -> None:
    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), {**shop_answers(view), ("project", "project"): failure})

    _, projected, _ = await _plan_all(view, gateway, _store(tmp_path))

    assert projected.outcome.fallback and projected.outcome.calls == 1
    assert [task.action for task in projected.setup] == ["WORKSPACE"]
    assert set(projected.setup[0].implements) == {
        "CAP::AUTHENTICATION",
        "CAP::UNREALISED",
        "REQ::FR::A",
        "REQ::FR::B",
        "REQ::FR::ORPHAN",
    }


async def test_a_fatal_model_error_propagates(tmp_path: Path) -> None:
    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), {**shop_answers(view), ("describe", "AREA::WEB_SHOP"): LLMFatalError()})

    with pytest.raises(LLMFatalError):
        await _plan_all(view, gateway, _store(tmp_path))


async def test_describe_keeps_spec_interfaces_whatever_the_describer_answers(tmp_path: Path) -> None:
    view = await shop_view()
    answers = shop_answers(view)
    original = answers[("describe", "AREA::ORDERS_SERVICE")]
    assert isinstance(original, DescribeAnswer)
    gateway = script_plan(
        ScriptedGateway(),
        {**answers, ("describe", "AREA::ORDERS_SERVICE"): original.model_copy(update={"interfaces": []})},
    )

    described = await describe(planning_context(view, _store(tmp_path), gateway), view, mode="first")

    interfaces = described.areas["AREA::ORDERS_SERVICE"].interfaces
    assert [(item.spec_id, item.summary) for item in interfaces] == [("IFC::ORDERS_API", None)]


def _current_task(task_id: str, implements: tuple[str, ...], story_id: str, phase: Phase = "logic") -> CurrentTask:
    return CurrentTask(
        task_id=task_id,
        area_id="AREA::ORDERS_SERVICE",
        action=task_id.rsplit("::", 1)[-1],
        revision=1,
        story_id=story_id,
        phase=phase,
        title="Current work",
        done_when=(DoneWhen(text="Done", source="planner"),),
        complexity="medium",
        implements=implements,
        planned_by="model",
        status="completed",
        commit="a" * 40,
        spec_build_id="build-2",
        created_at=STAMP,
        updated_at=STAMP,
    )


async def test_the_tasks_fallback_keeps_current_work_and_adds_rules_tasks_for_uncovered_members() -> None:
    view = await shop_view()
    current = CurrentPlan(
        plan=PlanInfo(
            plan_id="PLAN::MAIN",
            ontology_version="2.0",
            plan_ontology_version="2.0",
            rules_version="2.0",
            created_at=STAMP,
            updated_at=STAMP,
        ),
        tasks=(
            _current_task(
                "TASK::ORDERS_SERVICE::PLACE",
                ("REQ::FR::PLACE_ORDER", "FEAT::PLACE_ORDER"),
                "STORY::ORDERS_SERVICE::PLACE_ORDER",
            ),
        ),
    )
    view = PlanView(view.snapshot.model_copy(update={"current": current}), view.partition, view.rules)
    part = next(part for part in view.partition.parts if part.part_id == ORDERS_PART)

    tasks = tasks_fallback(view, part, area_tasks=(), not_buildable=(), convention_ids=())

    assert tasks[0].task_key == "TASK::ORDERS_SERVICE::PLACE" and tasks[0].planned_by == "model"
    covered = {spec_id for task in tasks for spec_id in task.implements}
    required = {spec_id for spec_id in part.members if view.required(spec_id)}
    assert required <= covered
    assert all(task.planned_by == "rules" for task in tasks[1:])
    assert not {"REQ::FR::PLACE_ORDER"} & {spec_id for task in tasks[1:] for spec_id in task.implements}


async def test_rules_tasks_have_phase_actions_ordinals_and_phase_waits() -> None:
    view = await shop_view()
    part = next(part for part in view.partition.parts if part.part_id == "AREA::PAYMENTS_SERVICE#1")

    tasks = rules_tasks(view, part, members=part.members, taken={"LOGIC_1"}, area_tasks=())

    by_action = {task.action: task for task in tasks}
    assert "LOGIC_1_2" in by_action  # LOGIC_1 is held, so the next free suffix is used
    assert by_action["CONTRACT_3"].implements == ("IFC::INTEGRATED",)
    integration = by_action["INTEGRATION_4"]
    assert integration.waits == tuple(sorted(by_action[action].task_key for action in ("LOGIC_1_2",)))
    assert all(task.done_when and task.planned_by == "rules" for task in tasks)


async def test_project_and_tasks_steps_use_the_options_and_record_oversized_tasks(tmp_path: Path) -> None:
    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), shop_answers(view))
    ctx = planning_context(view, _store(tmp_path), gateway, options=PlanOptions(task_size=1))

    projected: ProjectArtifact = await project(ctx, view, DescribeArtifact(), mode="first")
    tasks = await plan_tasks(ctx, view, DescribeArtifact(), projected, mode="first")

    by_key = {task.task_key: task for task in tasks.tasks}
    assert tasks.oversized and all(len(by_key[key].implements) > 1 for key in tasks.oversized)
    assert projected.setup[0].task_key == "TASK::PROJECT::WORKSPACE"
