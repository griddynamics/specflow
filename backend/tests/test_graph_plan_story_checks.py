"""The plan checks of claim state: ``progress``, ``blocked_by`` and ``completed_by`` against the status (PD5), story
claim fields against their tasks (PD8), and the plan of the claim tests as the checks see it (FR-PCHK-07)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from graph.plan.checks import plan_state
from graph.plan.invariants import PlanState, StoryState, TaskState, problems
from graph.plan.reader import LoadedPlan
from graph.plan.snapshot import read_current_plan
from plan_claims_support import mini_plan

TOKEN = "t" * 32
STORY = "STORY::A::API"


def _state(**tasks: dict[str, Any]) -> PlanState:
    """One active story with the tasks ``T1`` (in progress, claimed), ``T2`` (pending) and the changes given."""

    held = StoryState(area_id="AREA::A", status="active", kind="core", held=True, claim_token=TOKEN, claim_stamped=True)
    base = {
        "T1": TaskState("AREA::A", STORY, "in_progress", claimed=True, claim_token=TOKEN, progress="done"),
        "T2": TaskState("AREA::A", STORY, "pending"),
    }
    merged = {name: replace(task, **tasks.get(name, {})) for name, task in base.items()}
    return PlanState(
        plannable=frozenset(),
        required=frozenset(),
        areas={"AREA::A": "active"},
        stories={STORY: held},
        tasks=merged,
        links=(("IN_STORY", "T1", STORY), ("IN_STORY", "T2", STORY)),
    )


def _task_state(state: PlanState) -> tuple[str, ...]:
    return problems(state)["task_state"]


def test_a_held_story_with_its_claimed_tasks_is_consistent() -> None:
    assert _task_state(_state()) == ()


@pytest.mark.parametrize(
    ("task", "change"),
    [
        ("T2", {"progress": "done"}),
        ("T2", {"blocked_by": "FND::1"}),
        ("T2", {"completed_by": "u1"}),
        ("T2", {"claim_token": TOKEN}),
    ],
)
def test_claim_fields_that_do_not_match_the_status_or_the_story_claim_are_reported(
    task: str, change: dict[str, Any]
) -> None:
    assert _task_state(_state(**{task: change})) == (task,)


def test_a_claimed_task_with_another_token_is_reported_with_the_story_it_leaves_without_a_claimed_task() -> None:
    assert _task_state(_state(T1={"claim_token": "other"})) == (STORY, "T1")


def test_a_completed_task_may_keep_completed_by_and_its_claim_free_commit() -> None:
    state = _state(T2={"status": "completed", "commit": "c" * 40, "completed_by": "u1"})

    assert _task_state(state) == ()


def test_a_blocked_task_may_carry_its_finding_and_a_superseded_task_its_completion_record() -> None:
    state = _state(
        T1={"status": "blocked", "claimed": False, "claim_token": None, "progress": None, "blocked_by": "FND::1"},
        T2={"status": "superseded", "commit": "c" * 40, "completed_by": "u1"},
    )
    state = replace(state, stories={STORY: replace(state.stories[STORY], held=False)})

    assert _task_state(state) == ()


@pytest.mark.parametrize("story", [{"claim_token": None}, {"claim_stamped": False}])
def test_a_held_story_without_its_token_or_time_is_reported(story: dict[str, Any]) -> None:
    state = _state()
    state = replace(state, stories={STORY: replace(state.stories[STORY], **story)})

    assert STORY in _task_state(state)


def test_a_held_story_without_a_claimed_task_is_reported() -> None:
    state = _state(T1={"status": "pending", "claimed": False, "claim_token": None, "progress": None})

    assert _task_state(state) == (STORY,)


async def test_the_claim_tests_plan_passes_the_state_checks_before_and_after_a_claim() -> None:
    memory = mini_plan()
    ready = plan_state(
        LoadedPlan(await read_current_plan(memory, "shop")).current, plannable=frozenset(), required=frozenset()
    )
    assert _task_state(ready) == ()

    memory.set_story(STORY, claimed_by="u1", claimed_at="2026-01-02T09:00:00+00:00", claim_token=TOKEN)
    for task_id in ("TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"):
        memory.set_task(
            task_id, status="in_progress", claimed_by="u1", claimed_at="2026-01-02T09:00:00+00:00", claim_token=TOKEN
        )
    held = plan_state(
        LoadedPlan(await read_current_plan(memory, "shop")).current, plannable=frozenset(), required=frozenset()
    )
    assert _task_state(held) == ()

    memory.set_task("TASK::A::API_LOGIC", status="pending", progress="done")
    broken = plan_state(
        LoadedPlan(await read_current_plan(memory, "shop")).current, plannable=frozenset(), required=frozenset()
    )
    assert _task_state(broken) == ("TASK::A::API_LOGIC",)
