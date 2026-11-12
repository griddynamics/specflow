"""Story claims on the in-memory plan (``plan_claims_support.mini_plan``): claim, release, progress, completion and
blocking, the repeat answers, the refusals and the races. The same calls run against every live store in
``test_graph_plan_story_claims_live``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from graph.core.errors import ConfigurationError
from graph.core.reader import GraphNode
from graph.plan import PlanService
from graph.plan.claim_support import same_token
from graph.plan.errors import TaskStateError
from graph.plan.lifecycle import TaskLifecycle
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import MemoryPlanExecutor, mini_plan

API = "STORY::A::API"
UI = "STORY::A::UI"
X, Y = "STORY::A::X", "STORY::A::Y"
CONTRACT, LOGIC = "TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"
COMMIT = "AB" * 20


class Clock:
    """A UTC clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 2, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class SpecNodes:
    """The spec graph reads of ``complete_story``: ``REQ::A`` was written by build ``b2``, ``REQ::OLD`` by ``b1``."""

    async def node(self, _project_id: str, spec_id: str) -> GraphNode | None:
        builds = {"REQ::A": "b2", "REQ::OLD": "b1", "REQ::NEW": None}
        if spec_id not in builds:
            return None
        return GraphNode(
            spec_id=spec_id, uuid=spec_id, label="Requirement", name=spec_id, last_build_id=builds[spec_id]
        )

    async def nodes(self, _project_id: str) -> list[GraphNode]:
        return []

    async def scope(self, _project_id: str, _spec_id: str) -> list[GraphNode]:
        return []


def _service(memory: MemoryPlanExecutor | None = None) -> tuple[PlanService, MemoryPlanExecutor, Clock]:
    memory = memory or mini_plan()
    clock = Clock()
    ontology = plan_ontology()
    service = PlanService(memory, ontology, plan_rules(ontology), graph=SpecNodes(), clock=clock)
    return service, memory, clock


def _lifecycle(memory: MemoryPlanExecutor, clock: Clock) -> TaskLifecycle:
    ontology = plan_ontology()
    return TaskLifecycle(memory, ontology, plan_rules(ontology), clock=clock)


async def _refused(call: Any) -> TaskStateError:
    with pytest.raises(TaskStateError) as raised:
        await call
    return raised.value


async def test_a_claim_moves_the_units_open_tasks_to_in_progress_under_one_token() -> None:
    # Scenario: u1 claims the API story, whose setup prerequisites are completed.
    # Service -> store: lock the story, lock its tasks, read the plan, write the story claim, write the task claims.
    service, memory, _ = _service()

    claim = await service.claim_story("shop", API, claimed_by="u1", agent_label="builder", branch="feat/api")

    assert claim.story_ids == (API,) and claim.repeat is False and len(claim.claim_token) == 32
    assert [task.task_id for task in claim.tasks] == [CONTRACT, LOGIC]
    assert [task.status for task in claim.tasks] == ["in_progress", "in_progress"]
    story = memory.stories[API]
    assert (story["claimed_by"], story["agent_label"], story["branch"]) == ("u1", "builder", "feat/api")
    assert story["claim_token"] == claim.claim_token and story["claimed_at"] == claim.claimed_at.isoformat()
    for task_id in (CONTRACT, LOGIC):
        task = memory.tasks[task_id]
        assert (task["status"], task["claimed_by"], task["claim_token"], task["attempts"]) == (
            "in_progress",
            "u1",
            claim.claim_token,
            1,
        )
    assert (await service.story_preview("shop", API)).state == "in_progress"


async def test_the_holder_claiming_again_gets_the_same_token_and_nothing_is_written() -> None:
    # u1 claims with a label, then claims again with another one: same token, same tasks, no write statements.
    service, memory, _ = _service()
    first = await service.claim_story("shop", API, claimed_by="u1", agent_label="one")
    writes = memory.statements.count("plan_set_stories") + memory.statements.count("plan_set_tasks")

    again = await service.claim_story("shop", API, claimed_by="u1", agent_label="two")

    assert again.claim_token == first.claim_token and again.repeat is True
    assert [task.task_id for task in again.tasks] == [CONTRACT, LOGIC]
    assert memory.stories[API]["agent_label"] == "one"
    assert memory.statements.count("plan_set_stories") + memory.statements.count("plan_set_tasks") == writes


async def test_another_user_gets_story_claimed_with_the_holder_and_up_to_three_alternatives() -> None:
    service, _, _ = _service()
    first = await service.claim_story("shop", API, claimed_by="u1")

    refused = await _refused(service.claim_story("shop", API, claimed_by="u2"))

    assert refused.code == "story_claimed" and refused.claimed_by == "u1"
    assert refused.claimed_at == first.claimed_at.isoformat()
    assert refused.alternatives == (X, Y)


async def test_a_story_waiting_for_unfinished_work_outside_its_unit_is_not_available() -> None:
    service, _, _ = _service()

    waiting = await _refused(service.claim_story("shop", UI, claimed_by="u1"))
    later = await _refused(service.claim_story("shop", "STORY::A::LATE", claimed_by="u1"))

    assert waiting.code == "not_available" and waiting.blockers == (CONTRACT,)
    assert later.blockers == (CONTRACT, "TASK::A::UI_SCREEN")


async def test_a_blocked_story_and_a_story_without_open_work_are_not_available_without_blockers() -> None:
    service, _, _ = _service()

    stuck = await _refused(service.claim_story("shop", "STORY::A::STUCK", claimed_by="u1"))
    done = await _refused(service.claim_story("shop", "STORY::P::SETUP", claimed_by="u1"))

    assert (stuck.code, stuck.blockers) == ("not_available", ())
    assert (done.code, done.blockers) == ("not_available", ())


async def test_claiming_a_story_of_a_cycle_claims_the_whole_group_under_one_token() -> None:
    # X's logic waits for Y's contract and Y's logic for X's contract: claiming X takes both stories.
    service, memory, _ = _service()

    claim = await service.claim_story("shop", X, claimed_by="u1")

    assert claim.story_ids == (X, Y)
    assert {task.task_id for task in claim.tasks} == {
        "TASK::A::X_CONTRACT",
        "TASK::A::X_LOGIC",
        "TASK::A::Y_CONTRACT",
        "TASK::A::Y_LOGIC",
    }
    order = [task.task_id for task in claim.tasks]
    assert order.index("TASK::A::Y_CONTRACT") < order.index("TASK::A::X_LOGIC")
    assert order.index("TASK::A::X_CONTRACT") < order.index("TASK::A::Y_LOGIC")
    assert {memory.stories[story]["claim_token"] for story in (X, Y)} == {claim.claim_token}
    assert all(memory.stories[story]["claimed_by"] == "u1" for story in (X, Y))
    second = await service.claim_story("shop", Y, claimed_by="u1")
    assert second.claim_token == claim.claim_token and second.story_ids == (Y, X)


async def test_a_release_through_any_story_of_a_cycle_frees_the_group() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", X, claimed_by="u1")

    released = await service.release_story("shop", Y, claimed_by="u1", claim_token=claim.claim_token)

    assert len(released.released) == 4 and released.repeat is False
    assert all(memory.stories[story]["claimed_by"] is None for story in (X, Y))
    assert all(memory.tasks[task_id]["status"] == "pending" for task_id in released.released)


async def test_task_claims_and_story_claims_exclude_each_other() -> None:
    # A task claimed alone makes its story partly claimed; a pending task a plan run added to a held story cannot be
    # claimed alone (A35).
    service, memory, clock = _service()
    life = _lifecycle(memory, clock)
    await life.claim_task("shop", CONTRACT, claimed_by="hotfix")

    partly = await _refused(service.claim_story("shop", API, claimed_by="u1"))

    assert partly.code == "partly_claimed" and partly.task_id == CONTRACT and partly.claimed_by == "hotfix"
    await life.release_task("shop", CONTRACT, force=True)
    await service.claim_story("shop", API, claimed_by="u1")
    memory.tasks["TASK::A::API_LATER"] = {**memory.tasks[LOGIC], "task_id": "TASK::A::API_LATER", "status": "pending"}
    memory.tasks["TASK::A::API_LATER"].update(claim_token=None, claimed_by=None, claimed_at=None)

    for call in (
        life.claim_task("shop", "TASK::A::API_LATER", claimed_by="hotfix"),
        life.claim_next("shop", API, claimed_by="hotfix"),
    ):
        held = await _refused(call)
        assert held.code == "story_claimed" and held.claimed_by == "u1" and held.alternatives == (X, Y)


async def test_claims_are_refused_while_a_plan_persists_and_for_unknown_or_retired_stories() -> None:
    service, memory, _ = _service()
    memory.set_story("STORY::A::LATE", status="retired")
    memory.plan = {**(memory.plan or {}), "applying": "run-1"}

    applying = await _refused(service.claim_story("shop", API, claimed_by="u1"))
    memory.plan = {**(memory.plan or {}), "applying": None}
    unknown = await _refused(service.claim_story("shop", "STORY::A::NOPE", claimed_by="u1"))
    retired = await _refused(service.claim_story("shop", "STORY::A::LATE", claimed_by="u1"))

    assert applying.code == "plan_applying"
    assert unknown.code == "unknown_story"
    assert (retired.code, retired.fields["status"]) == ("wrong_state", "retired")


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"claimed_by": ""}, "invalid_claimant"),
        ({"claimed_by": "u\n1"}, "invalid_claimant"),
        ({"claimed_by": "u1", "agent_label": ""}, "invalid_field"),
        ({"claimed_by": "u1", "agent_label": "x" * 81}, "invalid_field"),
        ({"claimed_by": "u1", "branch": "x" * 201}, "invalid_field"),
    ],
)
async def test_claim_arguments_are_checked_before_any_statement(arguments: dict[str, str], code: str) -> None:
    service, memory, _ = _service()

    with pytest.raises(ConfigurationError) as raised:
        await service.claim_story("shop", API, **arguments)

    assert raised.value.code == code and memory.statements == []


async def test_exactly_one_of_ten_concurrent_claims_of_a_story_wins() -> None:
    service, memory, _ = _service()

    results = await asyncio.wait_for(
        asyncio.gather(
            *(service.claim_story("shop", API, claimed_by=f"u{index}") for index in range(10)), return_exceptions=True
        ),
        5,
    )

    claims = [item for item in results if not isinstance(item, BaseException)]
    refused = [item for item in results if isinstance(item, TaskStateError)]
    assert len(claims) == 1 and len(refused) == 9
    assert {error.code for error in refused} == {"story_claimed"}
    assert {error.claimed_by for error in refused} == {memory.stories[API]["claimed_by"]}
    assert all(error.claimed_at for error in refused)


async def test_two_claims_of_the_stories_of_one_cycle_at_once_give_one_claim() -> None:
    service, memory, _ = _service()

    results = await asyncio.wait_for(
        asyncio.gather(
            service.claim_story("shop", X, claimed_by="u1"),
            service.claim_story("shop", Y, claimed_by="u2"),
            return_exceptions=True,
        ),
        5,
    )

    claims = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, TaskStateError)]
    assert len(claims) == 1 and len(losers) == 1, results
    winner = claims[0]
    holder = "u1" if winner.story_ids[0] == X else "u2"
    assert (losers[0].code, losers[0].claimed_by) == ("story_claimed", holder)
    for story in (X, Y):
        assert (memory.stories[story]["claim_token"], memory.stories[story]["claimed_by"]) == (
            winner.claim_token,
            holder,
        )


@pytest.mark.parametrize("first", ["story", "task"])
async def test_a_task_claim_racing_a_story_claim_waits_for_the_story_locks_and_exactly_one_wins(first: str) -> None:
    # Scenario ("story" first): u1's story claim locks API's stories and tasks and stops before it writes the tasks;
    # hotfix's claim_task on CONTRACT must wait for the task lock; once the story claim commits, the task claim finds
    # CONTRACT in_progress (wrong_state). ("task" first): u1's story claim stops after its unlocked read, before the
    # locks; hotfix claims CONTRACT; the story claim then locks, reads again and answers partly_claimed.
    #   story claim -> store: read (unlocked), [gate], lock stories, lock tasks, read, [gate], set stories, set tasks
    #   claim_task  -> store: read, claim (waits for the task lock while the story claim holds it)
    service, memory, clock = _service()
    life = _lifecycle(memory, clock)
    gate = memory.pause_before("plan_set_tasks" if first == "story" else "plan_lock_stories")
    story = asyncio.create_task(service.claim_story("shop", API, claimed_by="u1"))
    await asyncio.wait_for(gate.reached.wait(), 5)

    task = asyncio.create_task(life.claim_task("shop", CONTRACT, claimed_by="hotfix"))
    if first == "story":
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done(), "the task claim did not wait for the story claim's task lock"
    else:
        await asyncio.wait_for(asyncio.shield(task), 5)
    gate.go.set()
    results = await asyncio.wait_for(asyncio.gather(story, task, return_exceptions=True), 5)

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, TaskStateError)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].code == ("wrong_state" if first == "story" else "partly_claimed")
    claimed = {task_id: row for task_id, row in memory.tasks.items() if row["status"] == "in_progress"}
    assert set(claimed) == ({CONTRACT, LOGIC} if first == "story" else {CONTRACT})
    assert {row["claim_token"] for row in claimed.values()} == {winners[0].claim_token}
    assert memory.held_locks() == []


async def test_a_release_returns_the_claimed_tasks_without_progress_and_keeps_the_token_for_repeats() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    await service.report_progress(
        "shop", CONTRACT, claimed_by="u1", claim_token=claim.claim_token, progress="done", note="built"
    )

    released = await service.release_story(
        "shop", API, claimed_by="u1", claim_token=claim.claim_token, note="stopping", branch="feat/api-2"
    )

    assert released.released == (CONTRACT, LOGIC) and released.repeat is False
    for task_id in (CONTRACT, LOGIC):
        task = memory.tasks[task_id]
        assert (task["status"], task["progress"], task["claimed_by"], task["claim_token"], task["note"]) == (
            "pending",
            None,
            None,
            None,
            None,
        )
        assert task["attempts"] == 1
    story = memory.stories[API]
    assert (story["claimed_by"], story["claimed_at"], story["agent_label"]) == (None, None, None)
    assert (story["note"], story["branch"], story["claim_token"]) == ("stopping", "feat/api-2", claim.claim_token)
    again = await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token)
    assert again.released == () and again.repeat is True
    other = await _refused(service.release_story("shop", API, claimed_by="u1", claim_token="0" * 32))
    assert other.code == "claim_mismatch"


async def test_a_release_keeps_the_story_note_and_branch_when_none_are_given() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1", branch="feat/api")
    await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token, note="first")
    claim = await service.claim_story("shop", API, claimed_by="u2")

    await service.release_story("shop", API, claimed_by="u2", claim_token=claim.claim_token)

    assert (memory.stories[API]["note"], memory.stories[API]["branch"]) == ("first", "feat/api")


async def test_a_release_needs_the_current_token_or_force() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")

    none = await _refused(service.release_story("shop", API))
    wrong = await _refused(service.release_story("shop", API, claimed_by="u1", claim_token="f" * 32))
    assert (none.code, wrong.code) == ("claim_mismatch", "claim_mismatch")
    assert memory.stories[API]["claimed_by"] == "u1"

    forced = await service.release_story("shop", API, force=True)

    assert forced.released == (CONTRACT, LOGIC)
    assert memory.stories[API]["claimed_by"] is None and memory.tasks[CONTRACT]["status"] == "pending"
    again = await service.release_story("shop", API, force=True)
    assert again.released == () and again.repeat is True
    assert claim.claim_token == memory.stories[API]["claim_token"]


async def test_an_old_token_after_a_new_claim_is_a_mismatch_for_every_call() -> None:
    # u1 claims and releases API; u2 claims it; u1's old token no longer matches on progress, release or completion.
    service, memory, _ = _service()
    old = await service.claim_story("shop", API, claimed_by="u1")
    await service.release_story("shop", API, claimed_by="u1", claim_token=old.claim_token)
    new = await service.claim_story("shop", API, claimed_by="u2")
    token = old.claim_token

    calls = (
        service.report_progress("shop", CONTRACT, claimed_by="u1", claim_token=token, progress="done"),
        service.release_story("shop", API, claimed_by="u1", claim_token=token),
        service.complete_story("shop", API, claim_token=token, completed_by="u1", merge_commit=COMMIT),
    )

    for call in calls:
        assert (await _refused(call)).code == "claim_mismatch"
    assert (memory.stories[API]["claimed_by"], memory.stories[API]["claim_token"]) == ("u2", new.claim_token)
    assert {memory.tasks[task_id]["claim_token"] for task_id in (CONTRACT, LOGIC)} == {new.claim_token}
    assert memory.tasks[CONTRACT]["progress"] is None


async def test_a_release_without_a_token_of_a_story_never_claimed_is_a_mismatch() -> None:
    service, memory, _ = _service()
    assert memory.stories[API].get("claim_token") is None

    refused = await _refused(service.release_story("shop", API))

    assert refused.code == "claim_mismatch" and same_token(None, None) is False


async def test_a_release_of_an_unknown_story_is_refused() -> None:
    service, _, _ = _service()

    assert (await _refused(service.release_story("shop", "STORY::A::NOPE", force=True))).code == "unknown_story"


async def test_progress_is_set_on_a_claimed_task_and_the_status_stays() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")

    item = await service.report_progress(
        "shop", CONTRACT, claimed_by="u1", claim_token=claim.claim_token, progress="skipped", note="not needed"
    )
    again = await service.report_progress(
        "shop", CONTRACT, claimed_by="u1", claim_token=claim.claim_token, progress="skipped", note="not needed"
    )

    assert (item.status, item.progress) == ("in_progress", "skipped") and again == item
    assert memory.tasks[CONTRACT]["note"] == "not needed"
    wrong = await _refused(
        service.report_progress("shop", LOGIC, claimed_by="u1", claim_token="1" * 32, progress="done")
    )
    assert wrong.code == "claim_mismatch" and memory.tasks[LOGIC]["progress"] is None


async def test_progress_needs_a_claimed_task_of_a_held_story() -> None:
    service, memory, clock = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    await _lifecycle(memory, clock).claim_task("shop", "TASK::A::X_CONTRACT", claimed_by="hotfix")
    alone = memory.tasks["TASK::A::X_CONTRACT"]["claim_token"]

    pending = await _refused(
        service.report_progress(
            "shop", "TASK::A::UI_SCREEN", claimed_by="u1", claim_token=claim.claim_token, progress="done"
        )
    )
    task_claim = await _refused(
        service.report_progress("shop", "TASK::A::X_CONTRACT", claimed_by="u1", claim_token=alone, progress="done")
    )
    unknown = await _refused(
        service.report_progress("shop", "TASK::A::NOPE", claimed_by="u1", claim_token=alone, progress="done")
    )

    assert (pending.code, task_claim.code, unknown.code) == ("claim_mismatch", "claim_mismatch", "unknown_task")


@pytest.mark.parametrize(
    ("progress", "note", "code"), [("finished", None, "invalid_field"), ("done", "n" * 501, "invalid_note")]
)
async def test_progress_arguments_are_checked(progress: str, note: str | None, code: str) -> None:
    service, memory, _ = _service()

    with pytest.raises(ConfigurationError) as raised:
        await service.report_progress(
            "shop", CONTRACT, claimed_by="u1", claim_token="t" * 32, progress=cast(Any, progress), note=note
        )

    assert raised.value.code == code and memory.statements == []


async def test_a_completion_completes_the_done_tasks_returns_the_rest_and_names_what_became_available() -> None:
    # Scenario: u1 claims API (CONTRACT, LOGIC), finishes only the contract and completes with done=[CONTRACT].
    # The UI story waited only for that contract, so it is now available; the API story itself is claimable again.
    service, memory, _ = _service()
    memory.set_task(CONTRACT, implements=["REQ::A", "REQ::OLD"])
    claim = await service.claim_story("shop", API, claimed_by="u1")
    await service.report_progress("shop", CONTRACT, claimed_by="u1", claim_token=claim.claim_token, progress="done")

    result = await service.complete_story(
        "shop",
        API,
        claim_token=claim.claim_token,
        completed_by="u1",
        merge_commit=COMMIT,
        pr_url="https://git.example.test/pr/7",
        done=[CONTRACT],
    )

    assert (result.completed, result.released) == ((CONTRACT,), (LOGIC,))
    assert result.now_available == (UI,) and result.spec_changed == ("REQ::A",) and result.repeat is False
    done, rest = memory.tasks[CONTRACT], memory.tasks[LOGIC]
    assert (done["status"], done["commit"], done["completed_by"]) == ("completed", COMMIT.lower(), "u1")
    assert done["completed_at"] and done["claimed_by"] is None and done["progress"] is None
    assert (rest["status"], rest["claimed_by"], rest["claim_token"]) == ("pending", None, None)
    story = memory.stories[API]
    assert (story["claimed_by"], story["pr_url"], story["claim_token"]) == (
        None,
        "https://git.example.test/pr/7",
        claim.claim_token,
    )


async def test_completing_every_claimed_task_completes_the_story_in_build_order() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", X, claimed_by="u1")

    result = await service.complete_story(
        "shop", X, claim_token=claim.claim_token, completed_by="u1", merge_commit=COMMIT
    )

    assert result.completed == tuple(task.task_id for task in claim.tasks) and result.released == ()
    assert all(memory.tasks[task_id]["status"] == "completed" for task_id in result.completed)
    assert all(memory.stories[story]["claimed_by"] is None for story in (X, Y))
    assert (await service.story_preview("shop", X)).state == "completed"


async def test_a_repeated_completion_answers_the_same_tasks_and_changes_nothing() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    first = await service.complete_story(
        "shop", API, claim_token=claim.claim_token, completed_by="u1", merge_commit=COMMIT, done=[CONTRACT]
    )
    writes = memory.statements.count("plan_set_tasks")

    again = await service.complete_story(
        "shop", API, claim_token=claim.claim_token, completed_by="u1", merge_commit=COMMIT, done=[CONTRACT]
    )

    assert again.completed == first.completed == (CONTRACT,) and again.released == ()
    assert again.now_available == (UI,) and again.spec_changed == () and again.repeat is True
    assert memory.statements.count("plan_set_tasks") == writes


@pytest.mark.parametrize("change", [{"completed_by": "u2"}, {"claim_token": "9" * 32}, {"merge_commit": "d" * 40}])
async def test_a_completion_repeated_by_another_caller_token_or_commit_is_a_mismatch(change: dict[str, str]) -> None:
    service, _, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    arguments = {"claim_token": claim.claim_token, "completed_by": "u1", "merge_commit": COMMIT}
    await service.complete_story("shop", API, **arguments)

    refused = await _refused(service.complete_story("shop", API, **{**arguments, **change}))

    assert refused.code == "claim_mismatch"


async def test_a_completion_with_a_stale_token_or_for_an_unknown_story_is_refused() -> None:
    service, memory, _ = _service()
    await service.claim_story("shop", API, claimed_by="u1")

    stale = await _refused(
        service.complete_story("shop", API, claim_token="8" * 32, completed_by="u1", merge_commit=COMMIT)
    )
    unknown = await _refused(
        service.complete_story("shop", "STORY::A::NOPE", claim_token="8" * 32, completed_by="u1", merge_commit=COMMIT)
    )

    assert (stale.code, unknown.code) == ("claim_mismatch", "unknown_story")
    assert memory.tasks[CONTRACT]["status"] == "in_progress"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"merge_commit": "a" * 39}, "invalid_commit"),
        ({"merge_commit": "g" * 40}, "invalid_commit"),
        ({"completed_by": ""}, "invalid_claimant"),
        ({"pr_url": "ftp://git.example.test/1"}, "invalid_field"),
        ({"pr_url": "https://user:secret@git.example.test/1"}, "invalid_field"),
        ({"pr_url": "https://" + "h" * 2000}, "invalid_field"),
        ({"done": ["TASK::A::UI_SCREEN"]}, "invalid_done"),
    ],
)
async def test_completion_arguments_are_checked_before_any_write_and_the_story_stays_held(
    arguments: dict[str, Any], code: str
) -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    base = {"claim_token": claim.claim_token, "completed_by": "u1", "merge_commit": COMMIT}

    with pytest.raises(ConfigurationError) as raised:
        await service.complete_story("shop", API, **{**base, **arguments})

    assert raised.value.code == code
    assert memory.stories[API]["claimed_by"] == "u1" and memory.tasks[CONTRACT]["status"] == "in_progress"


async def test_blocking_a_story_blocks_its_claimed_tasks_and_unblocking_a_finding_returns_only_its_tasks() -> None:
    # Scenario: u1 claims API and the host blocks it for FND::A1; a plan run then resets one blocked task (pending, no
    # blocked_by); unblock_finding returns only the other one.
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")

    blocked = await service.block_story(
        "shop", API, claim_token=claim.claim_token, reason="spec_ambiguous", finding_id="FND::A1"
    )

    assert blocked.released == (CONTRACT, LOGIC)
    for task_id in (CONTRACT, LOGIC):
        task = memory.tasks[task_id]
        assert (task["status"], task["block_reason"], task["blocked_by"], task["claim_token"]) == (
            "blocked",
            "spec_ambiguous",
            "FND::A1",
            None,
        )
    assert memory.stories[API]["claimed_by"] is None
    repeat = await service.block_story(
        "shop", API, claim_token=claim.claim_token, reason="spec_ambiguous", finding_id="FND::A1"
    )
    assert repeat.released == (CONTRACT, LOGIC) and repeat.repeat is True
    memory.set_task(CONTRACT, status="pending", block_reason=None, blocked_by=None)

    returned = await service.unblock_finding("shop", "FND::A1")

    assert returned == (LOGIC,)
    assert (memory.tasks[LOGIC]["status"], memory.tasks[LOGIC]["blocked_by"]) == ("pending", None)
    assert memory.tasks["TASK::A::STUCK_BLOCKED"]["status"] == "blocked"
    assert await service.unblock_finding("shop", "FND::A1") == ()


async def test_blocking_needs_the_token_and_a_story_reason() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")

    for reason in ("environment", "other", "bored"):
        with pytest.raises(ConfigurationError) as raised:
            await service.block_story(
                "shop", API, claim_token=claim.claim_token, reason=cast(Any, reason), finding_id="F"
            )
        assert raised.value.code == "invalid_reason"
    with pytest.raises(ConfigurationError) as invalid:
        await service.block_story("shop", API, claim_token=claim.claim_token, reason="spec_missing", finding_id="")
    wrong = await _refused(
        service.block_story("shop", API, claim_token="7" * 32, reason="spec_missing", finding_id="F")
    )
    unknown = await _refused(
        service.block_story("shop", "STORY::A::NOPE", claim_token="7" * 32, reason="spec_missing", finding_id="F")
    )

    assert invalid.value.code == "invalid_field"
    assert (wrong.code, unknown.code) == ("claim_mismatch", "unknown_story")
    assert memory.tasks[CONTRACT]["status"] == "in_progress"


async def test_blocking_a_cycle_group_blocks_every_claimed_task_and_ends_the_whole_claim() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", X, claimed_by="u1")

    blocked = await service.block_story(
        "shop", Y, claim_token=claim.claim_token, reason="spec_missing", finding_id="FND::C"
    )

    assert len(blocked.released) == 4
    assert all(memory.stories[story]["claimed_by"] is None for story in (X, Y))
    assert {memory.tasks[task_id]["blocked_by"] for task_id in blocked.released} == {"FND::C"}


async def test_the_task_calls_record_completed_by_clear_progress_and_blocked_by() -> None:
    _, memory, clock = _service()
    life = _lifecycle(memory, clock)
    claim = await life.claim_task("shop", CONTRACT, claimed_by="alice")

    done = await life.complete_task("shop", CONTRACT, claim_token=claim.claim_token, commit=COMMIT)

    row = memory.tasks[CONTRACT]
    assert (row["completed_by"], row["claimed_by"], row["progress"]) == ("alice", None, None)
    assert row["completed_at"] and done.commit == COMMIT.lower() and done.claimed_by is None
    memory.set_task("TASK::A::STUCK_BLOCKED", status="blocked", blocked_by="FND::1")
    unblocked = await life.unblock_task("shop", "TASK::A::STUCK_BLOCKED")
    assert (unblocked.status, unblocked.block_reason) == ("pending", None)
    assert memory.tasks["TASK::A::STUCK_BLOCKED"]["blocked_by"] is None
    for call in ("fail", "block", "release"):
        started = await life.claim_task("shop", LOGIC, claimed_by="alice")
        memory.set_task(LOGIC, progress="done")
        kwargs: dict[str, Any] = {"claim_token": started.claim_token}
        if call == "block":
            kwargs["reason"] = "other"
        await getattr(life, f"{call}_task")("shop", LOGIC, **kwargs)
        assert memory.tasks[LOGIC]["progress"] is None
        memory.set_task(LOGIC, status="pending", block_reason=None)


async def test_a_completion_by_task_name_unknown_task_is_refused() -> None:
    _, memory, clock = _service()

    with pytest.raises(TaskStateError) as raised:
        await _lifecycle(memory, clock).complete_task("shop", "TASK::A::NOPE", claim_token="t" * 32, commit=COMMIT)

    assert raised.value.code == "unknown_task"


async def test_a_task_call_on_a_task_of_a_held_story_is_refused_and_the_host_frees_it_through_the_story() -> None:
    # Scenario: u1 holds API (the task claims carry the story token). complete/fail/block/release (with the token and
    # forced) on one of its tasks answer story_claimed with the holder and change nothing; release_story(force) frees.
    service, memory, clock = _service()
    life = _lifecycle(memory, clock)
    claim = await service.claim_story("shop", API, claimed_by="u1")
    token = claim.claim_token
    before = {task_id: dict(row) for task_id, row in memory.tasks.items()}
    calls = (
        life.complete_task("shop", CONTRACT, claim_token=token, commit=COMMIT),
        life.fail_task("shop", CONTRACT, claim_token=token),
        life.block_task("shop", CONTRACT, claim_token=token, reason="other"),
        life.release_task("shop", CONTRACT, claim_token=token),
        life.release_task("shop", CONTRACT, force=True),
    )

    for call in calls:
        held = await _refused(call)
        assert held.code == "story_claimed" and held.claimed_by == "u1" and held.alternatives == (X, Y)

    assert memory.tasks == before and memory.stories[API]["claimed_by"] == "u1"
    released = await service.release_story("shop", API, force=True)
    assert set(released.released) == {CONTRACT, LOGIC}
    assert {memory.tasks[task_id]["status"] for task_id in released.released} == {"pending"}


async def test_a_repeated_claim_after_a_plan_run_joined_an_unheld_story_to_the_unit_is_partly_claimed() -> None:
    # Scenario: u1 holds API; a plan run makes API's contract wait for UI's screen (one cycle), so the unit grows by
    # the unheld UI story; u1's repeat is partly_claimed (documented in lifecycle.md "Story claims").
    service, memory, _ = _service()
    await service.claim_story("shop", API, claimed_by="u1")
    memory.add_waits((CONTRACT, "TASK::A::UI_SCREEN"))
    before = {task_id: dict(row) for task_id, row in memory.tasks.items()}

    partly = await _refused(service.claim_story("shop", API, claimed_by="u1"))

    assert partly.code == "partly_claimed" and partly.claimed_by == "u1"
    assert memory.tasks == before and not memory.stories[UI].get("claimed_by")


async def test_the_story_calls_are_refused_while_a_plan_persists_except_a_forced_release() -> None:
    # Scenario: u1 holds API; a persist starts (it locks a task before its story, the story calls lock stories first);
    # progress, completion, blocking, unblocking and a release with the token answer plan_applying and change nothing;
    # the host's forced release still frees the claim.
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    token = claim.claim_token
    before = {task_id: dict(row) for task_id, row in memory.tasks.items()}
    memory.plan = {**(memory.plan or {}), "applying": "run-1"}
    calls = (
        service.report_progress("shop", CONTRACT, claimed_by="u1", claim_token=token, progress="done"),
        service.complete_story("shop", API, claim_token=token, completed_by="u1", merge_commit=COMMIT),
        service.block_story("shop", API, claim_token=token, reason="spec_missing", finding_id="FND::1"),
        service.unblock_finding("shop", "FND::1"),
        service.release_story("shop", API, claimed_by="u1", claim_token=token),
    )

    for call in calls:
        assert (await _refused(call)).code == "plan_applying"

    assert memory.tasks == before
    released = await service.release_story("shop", API, force=True)
    assert set(released.released) == {CONTRACT, LOGIC}


# --- the holder, not only the token (another member who learned the token) -------------------------------------


def _writes(memory: MemoryPlanExecutor) -> int:
    return memory.statements.count("plan_set_tasks") + memory.statements.count("plan_set_stories")


async def test_another_member_with_the_holders_valid_token_can_neither_release_report_nor_complete() -> None:
    # u1 holds API; u2 presents u1's current token on every claim call: each is claim_mismatch and nothing is written.
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    token, writes = claim.claim_token, _writes(memory)

    calls = (
        service.release_story("shop", API, claimed_by="u2", claim_token=token),
        service.report_progress("shop", CONTRACT, claimed_by="u2", claim_token=token, progress="done"),
        service.complete_story("shop", API, claim_token=token, completed_by="u2", merge_commit=COMMIT),
    )

    for call in calls:
        assert (await _refused(call)).code == "claim_mismatch"
    assert _writes(memory) == writes
    assert (memory.stories[API]["claimed_by"], memory.stories[API]["claim_token"]) == ("u1", token)
    assert {memory.tasks[task_id]["status"] for task_id in (CONTRACT, LOGIC)} == {"in_progress"}
    assert memory.tasks[CONTRACT]["progress"] is None


async def test_a_release_needs_a_caller_unless_forced() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")

    with pytest.raises(ConfigurationError) as caught:
        await service.release_story("shop", API, claim_token=claim.claim_token)

    assert caught.value.code == "invalid_claimant" and memory.stories[API]["claimed_by"] == "u1"
    assert (await service.release_story("shop", API, force=True)).released == (CONTRACT, LOGIC)


async def test_a_release_repeated_by_another_member_with_the_last_token_is_a_mismatch() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token)
    writes = _writes(memory)

    other = await _refused(service.release_story("shop", API, claimed_by="u2", claim_token=claim.claim_token))
    again = await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token)

    assert other.code == "claim_mismatch" and (again.released, again.repeat) == ((), True)
    assert _writes(memory) == writes


async def test_a_release_after_a_completion_is_a_repeat_for_the_holder_only() -> None:
    service, _, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    await service.complete_story("shop", API, claim_token=claim.claim_token, completed_by="u1", merge_commit=COMMIT)

    other = await _refused(service.release_story("shop", API, claimed_by="u2", claim_token=claim.claim_token))
    again = await service.release_story("shop", API, claimed_by="u1", claim_token=claim.claim_token)

    assert other.code == "claim_mismatch" and again.repeat is True


@pytest.mark.parametrize("holder", ["u1", "u2"])
async def test_a_stale_token_after_a_release_and_a_new_claim_is_a_mismatch_even_for_the_old_holder(holder: str) -> None:
    # u1 claims and releases API; ``holder`` (u2, or u1 again) claims it anew; u1's old token matches no call.
    service, memory, _ = _service()
    old = await service.claim_story("shop", API, claimed_by="u1")
    await service.release_story("shop", API, claimed_by="u1", claim_token=old.claim_token)
    new = await service.claim_story("shop", API, claimed_by=holder)
    token, writes = old.claim_token, _writes(memory)

    calls = (
        service.release_story("shop", API, claimed_by="u1", claim_token=token),
        service.report_progress("shop", CONTRACT, claimed_by="u1", claim_token=token, progress="done"),
        service.complete_story("shop", API, claim_token=token, completed_by="u1", merge_commit=COMMIT),
    )

    for call in calls:
        assert (await _refused(call)).code == "claim_mismatch"
    assert _writes(memory) == writes and new.claim_token != token
    assert (memory.stories[API]["claimed_by"], memory.stories[API]["claim_token"]) == (holder, new.claim_token)


async def test_a_completion_repeated_with_another_done_answers_the_first_completion() -> None:
    service, memory, _ = _service()
    claim = await service.claim_story("shop", API, claimed_by="u1")
    arguments = {"claim_token": claim.claim_token, "completed_by": "u1", "merge_commit": COMMIT}
    first = await service.complete_story("shop", API, **arguments, done=[CONTRACT])
    writes = _writes(memory)

    again = await service.complete_story("shop", API, **arguments, done=[CONTRACT, LOGIC])

    assert (again.completed, again.repeat) == (first.completed, True) and _writes(memory) == writes
    assert memory.tasks[LOGIC]["status"] == "pending"
