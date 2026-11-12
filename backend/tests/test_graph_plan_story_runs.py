"""Plan runs and story claims: what a run writes never touches a claim, a task's progress or its completion record
(FR-PDIF-10, FR-PDIF-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.plan.models import CurrentPlan
from graph.plan.persist import batches
from graph_plan_support import applied_plan, next_view
from test_graph_plan_diff import PLACE, _diff, _first, _ops, _second

CLAIM_KEYS = frozenset(
    {"claimed_by", "claimed_at", "agent_label", "claim_token", "branch", "note", "pr_url", "progress", "attempts"}
)
DONE_KEYS = frozenset({"commit", "completed_by", "completed_at"})
PLACE_STORY = "STORY::ORDERS_SERVICE::PLACE_ORDER"
CHANGE = {"REQ::FR::PLACE_ORDER": "changed_meaning"}
HELD = {
    "claimed_by": "u1",
    "claimed_at": "2026-01-02T09:00:00+00:00",
    "agent_label": "builder",
    "branch": "feat/place",
    "note": "keep",
    "pr_url": "https://git.example.test/1",
    "claim_token": "t" * 32,
}


def _held(current: CurrentPlan, story_id: str) -> CurrentPlan:
    stories = tuple(story.model_copy(update=HELD) if story.story_id == story_id else story for story in current.stories)
    return current.model_copy(update={"stories": stories})


@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_a_reset_clears_the_block_reason_and_the_blocking_finding(tmp_path: Path, status: str) -> None:
    blocked = status == "blocked"
    states = {
        PLACE: {
            "status": status,
            "block_reason": "spec_missing" if blocked else None,
            "blocked_by": "FND::A1" if blocked else None,
        }
    }

    _, artifact = await _second(tmp_path, states, changed=CHANGE)

    write = next(op for op in artifact.tasks if op.id == PLACE)
    assert write.op == "reset" and write.properties["status"] == "pending"
    assert write.properties["block_reason"] is None and write.properties["blocked_by"] is None


async def test_a_run_never_writes_a_story_claim_nor_the_progress_of_a_claimed_task(tmp_path: Path) -> None:
    # Scenario: u1 holds the place-order story and its task reported done; the requirement changes meaning; the run
    # leaves the claimed task, its progress and the story claim alone and flags the task as changed in progress.
    view, first = await _first(tmp_path)
    claimed = {
        PLACE: {
            "status": "in_progress",
            "claimed_by": "u1",
            "claimed_at": HELD["claimed_at"],
            "progress": "done",
            "attempts": 1,
        }
    }
    current = _held(applied_plan(view, first, states=claimed), PLACE_STORY)

    artifact = await _diff(next_view(view, current, changed=CHANGE), tmp_path)

    assert PLACE not in _ops(artifact) and artifact.in_progress_changed == (PLACE,)
    written = [
        *(op.properties for op in artifact.stories),
        *(op.properties for op in artifact.tasks),
        *(row["properties"] for batch in batches(artifact, "shop") for row in batch.rows if "properties" in row),
    ]
    assert written
    assert all(not (CLAIM_KEYS | DONE_KEYS) & set(properties) for properties in written)


async def test_a_task_completed_by_a_claim_keeps_its_record_when_a_later_run_supersedes_it(tmp_path: Path) -> None:
    view, first = await _first(tmp_path)
    done = {"status": "completed", "commit": "c" * 40, "completed_by": "u1", "completed_at": HELD["claimed_at"]}
    current = applied_plan(view, first, states={PLACE: done})

    artifact = await _diff(next_view(view, current, changed=CHANGE), tmp_path)

    superseded = next(op for op in artifact.tasks if op.id == PLACE)
    assert superseded.op == "supersede" and set(superseded.properties) == {"status"}
    assert f"{PLACE}_R2" in _ops(artifact)
    assert not any(DONE_KEYS & set(op.properties) for op in artifact.tasks)
