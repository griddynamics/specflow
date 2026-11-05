"""Checks of planner answers: one failing answer per check, problems as codes with ids."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from graph.plan.answers import (
    DescribeAnswer,
    DoneWhenAnswer,
    InterfaceAnswer,
    NotBuildableAnswer,
    ProjectAnswer,
    StorySummaryAnswer,
    TasksAnswer,
)
from graph.plan.files import open_plan_store
from graph.plan.validate import oversized, problem_counts, validate_describe, validate_project, validate_tasks
from graph.plan.view import PlanView
from graph_plan_support import planned_shop, script_plan, shop_answers, shop_view
from graph_support import ScriptedGateway

ORDERS_PART = "AREA::ORDERS_SERVICE#1"


async def _tasks(**changes: Any) -> tuple[PlanView, TasksAnswer]:
    view = await shop_view()
    answer = shop_answers(view)[("tasks", ORDERS_PART)]
    assert isinstance(answer, TasksAnswer)
    return view, answer.model_copy(update=changes)


def _check(view: PlanView, answer: TasksAnswer, *, conventions: tuple[str, ...] = ()) -> list[str]:
    part = next(part for part in view.partition.parts if part.part_id == ORDERS_PART)
    return validate_tasks(answer, view=view, part=part, earlier_actions=(), convention_ids=conventions)


async def test_the_scripted_shop_answers_are_valid() -> None:
    view = await shop_view()
    answers = shop_answers(view)

    for part in view.partition.parts:
        answer = answers[("tasks", part.part_id)]
        assert isinstance(answer, TasksAnswer)
        assert (
            validate_tasks(answer, view=view, part=part, earlier_actions=(), convention_ids=("CON::EU_REGION",)) == []
        )
    project = answers[("project", "project")]
    assert isinstance(project, ProjectAnswer)
    assert validate_project(project, view=view) == []
    for area in view.active_areas:
        describe = answers[("describe", area.area_id)]
        assert isinstance(describe, DescribeAnswer)
        assert validate_describe(describe, view=view, area_id=area.area_id) == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"action": "lower_case"}, "invalid_action"),
        ({"action": "LOGIC_R2"}, "invalid_action"),
        ({"phase": "deploy"}, "invalid_phase"),
        ({"phase": "setup"}, "invalid_phase"),
        ({"complexity": "huge"}, "invalid_complexity"),
        ({"story": "STORY::WEB_SHOP::CHECKOUT"}, "wrong_story"),
        ({"implements": ["REQ::FR::CHECKOUT"]}, "unknown_member"),
        ({"done_when": []}, "empty_done_when"),
        ({"waits_for": ["NOWHERE"]}, "unknown_wait"),
    ],
)
async def test_a_task_that_breaks_a_check_is_reported(change: dict[str, Any], code: str) -> None:
    view, answer = await _tasks()
    first = answer.tasks[0].model_copy(update=change)

    problems = _check(view, answer.model_copy(update={"tasks": [first, *answer.tasks[1:]]}))

    assert code in problem_counts(problems)


async def test_duplicate_actions_waits_cycles_and_spec_items_without_criteria_are_reported() -> None:
    view, answer = await _tasks()
    first, second = answer.tasks[0], answer.tasks[1]
    duplicate = second.model_copy(update={"action": first.action})
    cyclic = [
        first.model_copy(update={"waits_for": [second.action]}),
        second.model_copy(update={"waits_for": [first.action]}),
    ]
    spec_item = first.model_copy(
        update={"implements": ["BR::RELEASE"], "done_when": [*first.done_when, *first.done_when[:0]]}
    )
    spec_item = spec_item.model_copy(
        update={"done_when": [spec_item.done_when[0].model_copy(update={"source": "spec"})]}
    )

    assert "duplicate_action" in problem_counts(_check(view, answer.model_copy(update={"tasks": [first, duplicate]})))
    assert "wait_cycle" in problem_counts(_check(view, answer.model_copy(update={"tasks": cyclic})))
    assert "spec_done_when_without_criteria" in problem_counts(
        _check(view, answer.model_copy(update={"tasks": [spec_item, *answer.tasks[1:]]}))
    )


async def test_every_required_member_must_be_implemented_not_buildable_or_cited() -> None:
    view, answer = await _tasks()
    without = [task for task in answer.tasks if "REQ::BR::ORDER_LIMIT" not in task.implements]
    trimmed = answer.model_copy(update={"tasks": without})

    assert "uncovered_member:REQ::BR::ORDER_LIMIT" in _check(view, trimmed)
    assert "uncovered_member:REQ::BR::ORDER_LIMIT" not in _check(view, trimmed, conventions=("REQ::BR::ORDER_LIMIT",))
    listed = trimmed.model_copy(
        update={
            "not_buildable": [NotBuildableAnswer(spec_id="REQ::BR::ORDER_LIMIT", reason="governance", note="Policy.")]
        }
    )
    assert "uncovered_member:REQ::BR::ORDER_LIMIT" not in _check(view, listed)


async def test_not_buildable_entries_are_checked() -> None:
    view, answer = await _tasks()
    implemented = answer.tasks[0].implements[0]
    entries = [
        NotBuildableAnswer(spec_id=implemented, reason="process", note=""),
        NotBuildableAnswer(spec_id="REQ::FR::CHECKOUT", reason="process", note=""),
        NotBuildableAnswer(spec_id="REQ::BR::ORDER_LIMIT", reason="whim", note=""),
    ]

    counts = problem_counts(_check(view, answer.model_copy(update={"not_buildable": entries})))

    assert counts["not_buildable_implemented"] == 2  # the first and the third entry are implemented
    assert counts["unknown_member"] == 1
    assert counts["invalid_reason"] == 1


async def test_oversized_tasks_are_a_warning_not_a_problem() -> None:
    view, answer = await _tasks()

    assert oversized(answer.tasks, 1) != []
    assert not [problem for problem in _check(view, answer) if problem.startswith("oversized")]


async def test_a_project_answer_needs_exactly_one_setup_root_and_setup_phases() -> None:
    view = await shop_view()
    answer = shop_answers(view)[("project", "project")]
    assert isinstance(answer, ProjectAnswer)
    two_roots = [task.model_copy(update={"waits_for": []}) for task in answer.setup_tasks]
    wrong_phase = [answer.setup_tasks[0].model_copy(update={"phase": "logic"}), *answer.setup_tasks[1:]]
    closing_wait = [answer.closing_tasks[0].model_copy(update={"waits_for": ["WORKSPACE"]})]

    assert "setup_roots:2" in validate_project(answer.model_copy(update={"setup_tasks": two_roots}), view=view)
    assert "invalid_phase" in problem_counts(
        validate_project(answer.model_copy(update={"setup_tasks": wrong_phase}), view=view)
    )
    assert "closing_waits" in problem_counts(
        validate_project(answer.model_copy(update={"closing_tasks": closing_wait}), view=view)
    )
    uncovered = answer.model_copy(update={"setup_tasks": [answer.setup_tasks[0]]})
    assert "uncovered_member:REQ::FR::ORPHAN" in validate_project(
        uncovered, view=view, setup_members=("REQ::FR::ORPHAN",)
    )


async def test_a_describe_answer_names_only_the_areas_interfaces_and_stories() -> None:
    view = await shop_view()
    answer = DescribeAnswer(
        title="Orders",
        description="Orders.",
        interfaces=[
            InterfaceAnswer(spec_id="IFC::STRIPE_API", name="Stripe API", kind="api", summary="", spec_ids=[]),
            InterfaceAnswer(spec_id="", name="Order events", kind="queue", summary="", spec_ids=["REQ::FR::CHECKOUT"]),
        ],
        stories=[StorySummaryAnswer(story_id="STORY::WEB_SHOP::CHECKOUT", summary="")],
    )

    counts = problem_counts(validate_describe(answer, view=view, area_id="AREA::ORDERS_SERVICE"))

    assert counts == {"invalid_interface_kind": 1, "unknown_interface": 1, "unknown_member": 1, "unknown_story": 1}


async def test_a_spec_done_when_item_is_accepted_when_an_implemented_node_has_acceptance_criteria() -> None:
    view, answer = await _tasks()
    place = next(task for task in answer.tasks if "REQ::FR::PLACE_ORDER" in task.implements)
    spec_only = place.model_copy(
        update={"done_when": [DoneWhenAnswer(text="The order is created and queued", source="spec")]}
    )
    tasks = [spec_only if task.action == place.action else task for task in answer.tasks]

    assert view.rules.acceptance_of(view.nodes["REQ::FR::PLACE_ORDER"].attributes)
    assert _check(view, answer.model_copy(update={"tasks": tasks})) == []


async def test_an_interface_implemented_only_by_a_logic_task_misses_its_contract_phase() -> None:
    view, answer = await _tasks()
    tasks = [
        task.model_copy(update={"phase": "logic"}) if "IFC::ORDERS_API" in task.implements else task
        for task in answer.tasks
    ]

    problems = _check(view, answer.model_copy(update={"tasks": tasks}))

    assert "missing_phase:contract:IFC::ORDERS_API" in problems
    assert problem_counts(problems) == {"missing_phase": 1}


async def test_a_not_buildable_entry_is_accepted_and_counted_for_its_area(tmp_path: Path) -> None:
    view, answer = await _tasks()
    tasks = [
        task.model_copy(update={"implements": [item for item in task.implements if item != "REQ::BR::ORDER_LIMIT"]})
        for task in answer.tasks
    ]
    entry = NotBuildableAnswer(spec_id="REQ::BR::ORDER_LIMIT", reason="governance", note="An approval rule.")
    listed = answer.model_copy(update={"tasks": tasks, "not_buildable": [entry]})
    assert _check(view, listed) == []

    answers: dict[tuple[str, str], Any] = {**shop_answers(view), ("tasks", ORDERS_PART): listed}
    shop = await planned_shop(view, open_plan_store(tmp_path / "plans"), script_plan(ScriptedGateway(), answers))

    outcome = shop.tasks.outcomes[ORDERS_PART]
    assert outcome.invalid == 0 and not outcome.fallback
    entries = shop.tasks.not_buildable["AREA::ORDERS_SERVICE"]
    assert [(item.spec_id, item.reason) for item in entries] == [("REQ::BR::ORDER_LIMIT", "governance")]
    assert sum(len(items) for items in shop.tasks.not_buildable.values()) == 1
    assert not [task for task in shop.tasks.tasks if "REQ::BR::ORDER_LIMIT" in task.implements]
