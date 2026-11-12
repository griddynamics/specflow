"""The locked transaction of the story calls on the in-memory plan (``plan_claims_support.mini_plan``): a claim unit
that grew between the unlocked read and the locks is locked in a further round, a unit that keeps growing gives up,
and a transaction that fails leaves nothing written and no lock held."""

from __future__ import annotations

from typing import Any

import pytest

from graph.plan import PlanService
from graph.plan.claims import ATTEMPTS
from graph.plan.errors import TaskStateError
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import Pair, mini_plan

API, UI, LATE, STUCK = "STORY::A::API", "STORY::A::UI", "STORY::A::LATE", "STORY::A::STUCK"
CONTRACT, SCREEN = "TASK::A::API_CONTRACT", "TASK::A::UI_SCREEN"
LATE_ONE, STUCK_OPEN, X_CONTRACT = "TASK::A::LATE_ONE", "TASK::A::STUCK_OPEN", "TASK::A::X_CONTRACT"


def _service(memory: Any) -> PlanService:
    ontology = plan_ontology()
    return PlanService(memory, ontology, plan_rules(ontology))


async def test_a_unit_that_grew_after_the_unlocked_read_is_locked_whole_in_a_second_round() -> None:
    # Scenario: a plan run made API's contract wait for UI's screen (API and UI are one cycle) after the claim's
    # unlocked read: that read still sees API alone. The locked read sees the cycle, so the claim locks again with both
    # stories before it decides.
    #   claim -> store: read (API alone) | tx 1: lock [API], read (API+UI) | tx 2: lock [API, UI], read, claim both
    memory = mini_plan()
    memory.add_waits((CONTRACT, SCREEN))
    memory.waits_view = lambda waits, where: waits if where == "tx" else waits - {(CONTRACT, SCREEN)}

    claim = await _service(memory).claim_story("shop", API, claimed_by="u1")

    assert claim.story_ids == (API, UI)
    assert {memory.stories[story]["claim_token"] for story in (API, UI)} == {claim.claim_token}
    assert {task.task_id for task in claim.tasks} == {CONTRACT, "TASK::A::API_LOGIC", SCREEN}
    assert memory.lock_rounds == [(API,), (API, UI)]
    assert memory.held_locks() == []


async def test_a_unit_that_keeps_growing_under_the_locks_is_not_available() -> None:
    # Scenario: every locked read finds one more story in API's cycle (UI, then LATE, then STUCK, then X and Y), so
    # each round needs more locks than it holds; after ATTEMPTS rounds the claim answers not_available, writes nothing.
    memory = mini_plan()
    steps: list[set[Pair]] = [
        {(CONTRACT, SCREEN)},
        {(SCREEN, LATE_ONE)},
        {(LATE_ONE, STUCK_OPEN), (STUCK_OPEN, LATE_ONE)},
        {(STUCK_OPEN, X_CONTRACT), (X_CONTRACT, STUCK_OPEN)},
    ]
    locked_reads = 0

    def view(waits: set[Pair], where: str) -> set[Pair]:
        nonlocal locked_reads
        if where != "tx":
            return waits
        locked_reads += 1
        return waits.union(*steps[:locked_reads])

    memory.waits_view = view
    before = (dict(memory.stories), dict(memory.tasks))

    with pytest.raises(TaskStateError) as refused:
        await _service(memory).claim_story("shop", API, claimed_by="u1")

    assert refused.value.code == "not_available"
    assert len(memory.lock_rounds) == ATTEMPTS
    assert memory.lock_rounds == [(API,), (API, UI), (API, LATE, UI), (API, LATE, STUCK, UI)]
    assert (memory.stories, memory.tasks) == before
    assert "plan_set_stories" not in memory.statements and memory.held_locks() == []


async def test_a_claim_whose_task_write_fails_leaves_the_story_unclaimed_and_no_lock_held() -> None:
    # Scenario: the story claim writes the story fields, then the task write fails; the transaction is dropped, so the
    # story keeps no claim fields, the locks are released and the next claim succeeds.
    memory = mini_plan()
    service = _service(memory)
    before = (dict(memory.stories[API]), {task_id: dict(row) for task_id, row in memory.tasks.items()})
    memory.fail_once("plan_set_tasks")

    with pytest.raises(RuntimeError):
        await service.claim_story("shop", API, claimed_by="u1")

    assert "plan_set_stories" in memory.statements
    assert (memory.stories[API], memory.tasks) == before
    assert memory.held_locks() == []
    claim = await service.claim_story("shop", API, claimed_by="u2")
    assert memory.stories[API]["claimed_by"] == "u2" and claim.claim_token == memory.stories[API]["claim_token"]
