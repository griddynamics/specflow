"""Plan checks: one violating plan per check, story progress, readiness and the run step functions offline."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from graph.plan.checks import plan_state
from graph.plan.diff import DiffInputs, diff
from graph.plan.files import open_plan_store
from graph.plan.invariants import CheckName, PlanState, TaskState, failing, problems, ready_tasks, story_progress
from graph.plan.models import CurrentPlan, PlanLink
from graph.plan.stitch import stitch
from graph.plan.view import PlanView
from graph_plan_support import applied_plan, planned_shop, shop_view

COMMIT = "d" * 40
PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"


async def _plan(tmp_path: Path) -> tuple[PlanView, CurrentPlan]:
    view = await shop_view()
    shop = await planned_shop(view, open_plan_store(tmp_path))
    artifact = diff(
        view,
        describe=shop.describe,
        project=shop.project,
        tasks=shop.tasks,
        stitch=stitch(view, shop.project, shop.tasks),
        inputs=DiffInputs("2.0", "2.0", "2.0"),
    )
    return view, applied_plan(view, artifact)


def _state(view: PlanView, current: CurrentPlan) -> PlanState:
    return plan_state(
        current,
        plannable=frozenset(view.nodes),
        required=frozenset(spec_id for spec_id in view.nodes if view.required(spec_id)),
    )


def _with_task(state: PlanState, task_id: str, **changes: Any) -> PlanState:
    return replace(state, tasks={**state.tasks, task_id: replace(state.tasks[task_id], **changes)})


async def test_a_fresh_plan_passes_every_check_and_only_the_setup_root_is_ready(tmp_path: Path) -> None:
    view, current = await _plan(tmp_path)
    state = _state(view, current)

    assert failing(problems(state)) == ()
    assert ready_tasks(state) == ("TASK::PROJECT::WORKSPACE",)
    assert ready_tasks(state, applying=True) == ()


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"status": "completed"}, "task_state"),
        ({"status": "in_progress"}, "task_state"),
        ({"area_id": "AREA::GONE"}, "task_area"),
        ({"story_id": "STORY::WEB_SHOP::CHECKOUT"}, "stories"),
        ({"implements": ("REQ::FR::DELETED",)}, "lost_target"),
    ],
)
async def test_each_check_reports_the_task_that_breaks_it(
    tmp_path: Path, change: dict[str, Any], check: CheckName
) -> None:
    view, current = await _plan(tmp_path)

    found = problems(_with_task(_state(view, current), PLACE, **change))

    assert PLACE in found[check]


async def test_membership_coverage_and_dependency_problems_are_found(tmp_path: Path) -> None:
    view, current = await _plan(tmp_path)
    state = _state(view, current)
    without_area = replace(
        state, links=frozenset(link for link in state.links if link[:2] != ("IN_AREA", "REQ::FR::CHECKOUT"))
    )
    without_task = replace(state, tasks={key: task for key, task in state.tasks.items() if key != PLACE})
    cycle = replace(
        state,
        links=state.links | {("WAITS_FOR", "TASK::PROJECT::WORKSPACE", "TASK::PROJECT::TOOLCHAIN")},
    )
    closed = _with_task(state, "TASK::PROJECT::WORKSPACE", status="retired")

    assert problems(without_area)["membership"] == ("REQ::FR::CHECKOUT",)
    assert "REQ::FR::PLACE_ORDER" in problems(without_task)["coverage"]
    assert problems(cycle)["dependencies"]
    assert "TASK::PROJECT::TOOLCHAIN" in problems(closed)["dependencies"]


async def test_a_not_buildable_entry_or_a_convention_covers_a_required_node(tmp_path: Path) -> None:
    view, current = await _plan(tmp_path)
    state = replace(_state(view, current), tasks={})

    assert "REQ::FR::PLACE_ORDER" in problems(state)["coverage"]
    covered = replace(state, not_buildable=frozenset(state.required))
    assert problems(covered)["coverage"] == ()


@pytest.mark.parametrize(
    ("statuses", "progress"),
    [
        (["completed", "completed", "superseded"], "completed"),
        (["completed", "blocked"], "blocked"),
        (["completed", "pending"], "in_progress"),
        (["pending", "failed"], "pending"),
        ([], "pending"),
        (["retired"], "pending"),
    ],
)
def test_story_progress_is_derived_first_matching_row(statuses: list[str], progress: str) -> None:
    assert story_progress(statuses) == progress


async def test_a_task_waiting_on_completed_work_becomes_ready(tmp_path: Path) -> None:
    view, current = await _plan(tmp_path)
    state = _with_task(_state(view, current), "TASK::PROJECT::WORKSPACE", status="completed", commit=COMMIT)

    assert ready_tasks(state) == ("TASK::PROJECT::TOOLCHAIN",)
    retired_area = replace(state, areas={**state.areas, "AREA::PROJECT": "retired"})
    assert ready_tasks(retired_area) == ()


def test_persisted_relationship_properties_are_kept_on_read() -> None:
    link = PlanLink.model_validate(
        {
            "type": "WAITS_FOR",
            "source": "a",
            "target": "b",
            "edge_key": "a|WAITS_FOR|b",
            "properties": [["source", "stitch"]],
        }
    )
    assert dict(link.properties) == {"source": "stitch"}
    assert TaskState(area_id="A", story_id="S", status="pending").claimed is False


async def test_waits_of_claimed_tasks_take_no_part_in_the_cycle_check(tmp_path: Path) -> None:
    view, current = await _plan(tmp_path)
    state = _with_task(_state(view, current), "TASK::PROJECT::TOOLCHAIN", status="in_progress", claimed=True)
    cycle = replace(state, links=state.links | {("WAITS_FOR", "TASK::PROJECT::WORKSPACE", "TASK::PROJECT::TOOLCHAIN")})

    assert problems(cycle)["dependencies"] == ()
