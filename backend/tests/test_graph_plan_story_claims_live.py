"""Story claims on the persisted shop plan of the live store (every store the live switch selects): claim, repeat,
race, cycle group, progress, completion, blocking, and a plan run after claim, progress and block."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from graph.core.store import Executor, Record, ScopedTransaction
from graph.plan import PlanService, TaskStateError, check_plan
from graph.plan.files import open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.models import CurrentTask
from graph.plan.options import PlanContext
from graph.plan.reader import load_plan
from graph.plan.run import StepFunctions, run_with
from graph_plan_support import (
    live_context,
    plan_ontology,
    plan_rules,
    planned_live_shop,
    script_plan,
    shop_answers,
    shop_view,
)
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, select
from graph_support import GraphProject, ScriptedGateway

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

COMMIT = "ab" * 20
WORKSPACE, TOOLCHAIN = "TASK::PROJECT::WORKSPACE", "TASK::PROJECT::TOOLCHAIN"
UNSHARED, UNSHARED_TASK = "STORY::PROJECT::UNSHARED", "TASK::PROJECT::CONTRACT_UNSHARED"
PLACE, PLACE_TASK = "STORY::ORDERS_SERVICE::PLACE_ORDER", "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
EXPORT, EXPORT_TASK = "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT", "TASK::ORDERS_SERVICE::LOGIC_ORDER_HISTORY_EXPORT"
STRIPE, CHECKOUT = "STORY::PAYMENTS_SERVICE::STRIPE_API", "STORY::WEB_SHOP::CHECKOUT"


def _service(project: GraphProject) -> PlanService:
    ontology = plan_ontology()
    return PlanService(project.store, ontology, plan_rules(ontology))


async def _stored(project: GraphProject, project_id: str, task_id: str) -> CurrentTask:
    """The stored task with its claim and completion record (reads of ``PlanReader`` show neither)."""

    return (await load_plan(project.store.executor(), project_id)).tasks[task_id]


def _lifecycle(project: GraphProject) -> TaskLifecycle:
    ontology = plan_ontology()
    return TaskLifecycle(project.store, ontology, plan_rules(ontology))


async def _complete(life: TaskLifecycle, project_id: str, *task_ids: str) -> None:
    for task_id in task_ids:
        claim = await life.claim_task(project_id, task_id, claimed_by="setup")
        await life.complete_task(project_id, task_id, claim_token=claim.claim_token, commit=COMMIT)


async def _ready_after_setup(project: GraphProject, tmp_path: Path, *also: str) -> tuple[str, PlanService]:
    project_id, _ = await planned_live_shop(project, tmp_path)
    await _complete(_lifecycle(project), project_id, WORKSPACE, TOOLCHAIN, *also)
    return project_id, _service(project)


async def test_a_story_is_claimed_reported_and_completed_and_the_stories_it_unblocks_are_named(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # Scenario: with setup done, u1 claims the unshared-contract story, reports its task done and completes it; the
    # stories that waited for that contract (checked by the plan checks too) become claimable.
    project_id, service = await _ready_after_setup(graph_project, tmp_path)

    claim = await service.claim_story(project_id, UNSHARED, claimed_by="u1", agent_label="builder", branch="feat/u")
    assert claim.story_ids == (UNSHARED,) and [task.task_id for task in claim.tasks] == [UNSHARED_TASK]
    item = await service.report_progress(
        project_id, UNSHARED_TASK, claimed_by="u1", claim_token=claim.claim_token, progress="done"
    )
    assert (item.status, item.progress) == ("in_progress", "done")
    preview = await service.story_preview(project_id, UNSHARED)
    assert (preview.state, preview.claimed_by, preview.agent_label, preview.branch) == (
        "in_progress",
        "u1",
        "builder",
        "feat/u",
    )

    result = await service.complete_story(
        project_id,
        UNSHARED,
        claim_token=claim.claim_token,
        completed_by="u1",
        merge_commit=COMMIT.upper(),
        pr_url="https://git.example.test/pr/1",
    )

    assert result.completed == (UNSHARED_TASK,) and result.released == () and result.repeat is False
    assert {PLACE, EXPORT, STRIPE} <= set(result.now_available)
    task = await _stored(graph_project, project_id, UNSHARED_TASK)
    assert (task.status, task.commit, task.completed_by) == ("completed", COMMIT, "u1")
    assert task.completed_at is not None and task.claimed_by is None and task.progress is None
    done = await service.story_list(project_id, view="done", caller="u1")
    assert {(row.story_id, row.commit, row.completed_by, row.pr_url) for row in done} == {
        (UNSHARED, COMMIT, "u1", "https://git.example.test/pr/1"),
        ("STORY::PROJECT::SETUP", COMMIT, "setup", None),
    }
    again = await service.complete_story(
        project_id, UNSHARED, claim_token=claim.claim_token, completed_by="u1", merge_commit=COMMIT
    )
    assert again.completed == (UNSHARED_TASK,) and again.repeat is True and again.released == ()
    with pytest.raises(TaskStateError) as other:
        await service.complete_story(
            project_id, UNSHARED, claim_token=claim.claim_token, completed_by="u2", merge_commit=COMMIT
        )
    assert other.value.code == "claim_mismatch"
    report = await check_plan(_context(graph_project, project_id, tmp_path))
    assert report.ok, report.problems


def _context(project: GraphProject, project_id: str, tmp_path: Path) -> PlanContext:
    return live_context(project, project_id, open_plan_store(tmp_path), ScriptedGateway())


async def test_the_holder_claims_again_for_the_same_token_and_another_user_is_refused(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, service = await _ready_after_setup(graph_project, tmp_path)
    first = await service.claim_story(project_id, UNSHARED, claimed_by="u1", agent_label="one")

    again = await service.claim_story(project_id, UNSHARED, claimed_by="u1", agent_label="two")
    with pytest.raises(TaskStateError) as refused:
        await service.claim_story(project_id, UNSHARED, claimed_by="u2")

    assert again.claim_token == first.claim_token and again.repeat is True
    assert (await service.story_preview(project_id, UNSHARED)).agent_label == "one"
    assert refused.value.code == "story_claimed" and refused.value.claimed_by == "u1"
    assert refused.value.claimed_at is not None and len(refused.value.alternatives) <= 3
    assert UNSHARED not in refused.value.alternatives
    with pytest.raises(TaskStateError) as not_holder:  # u2 presents u1's valid token
        await service.release_story(project_id, UNSHARED, claimed_by="u2", claim_token=first.claim_token)
    assert not_holder.value.code == "claim_mismatch"
    assert (await service.story_preview(project_id, UNSHARED)).claimed_by == "u1"
    released = await service.release_story(
        project_id, UNSHARED, claimed_by="u1", claim_token=first.claim_token, note="later"
    )
    assert released.released == (UNSHARED_TASK,)
    repeat = await service.release_story(project_id, UNSHARED, claimed_by="u1", claim_token=first.claim_token)
    assert repeat.released == () and repeat.repeat is True
    with pytest.raises(TaskStateError) as other_repeat:  # the stored token holder makes a repeat the holder's only
        await service.release_story(project_id, UNSHARED, claimed_by="u2", claim_token=first.claim_token)
    assert other_repeat.value.code == "claim_mismatch"
    with pytest.raises(TaskStateError) as stale:
        await service.release_story(project_id, UNSHARED, claimed_by="u1", claim_token="0" * 32)
    assert stale.value.code == "claim_mismatch"


async def test_exactly_one_of_ten_concurrent_story_claims_wins(graph_project: GraphProject, tmp_path: Path) -> None:
    project_id, service = await _ready_after_setup(graph_project, tmp_path)

    results = await asyncio.wait_for(
        asyncio.gather(
            *(service.claim_story(project_id, UNSHARED, claimed_by=f"user-{index}") for index in range(10)),
            return_exceptions=True,
        ),
        30,
    )

    claims = [item for item in results if not isinstance(item, BaseException)]
    refused = [item for item in results if isinstance(item, TaskStateError)]
    assert len(claims) == 1 and len(refused) == 9, results
    assert {error.code for error in refused} == {"story_claimed"}
    holder = (await service.story_preview(project_id, UNSHARED)).claimed_by
    assert {error.claimed_by for error in refused} == {holder} and all(error.claimed_at for error in refused)


class _PausingExecutor:
    """The store's executor, except that a statement named ``pause_at`` run in a transaction waits for ``go`` (after
    setting ``reached``), so the transaction holds its locks meanwhile."""

    def __init__(self, inner: Executor, pause_at: str) -> None:
        self._inner = inner
        self.pause_at = pause_at
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def read(self, query: Any, project_id: str, /, **params: Any) -> list[Record]:
        return await self._inner.read(query, project_id, **params)

    async def write(self, query: Any, project_id: str, /, **params: Any) -> list[Record]:
        return await self._inner.write(query, project_id, **params)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[_PausingTransaction]:
        async with self._inner.tx(project_id) as inner:
            yield _PausingTransaction(self, inner)


class _PausingTransaction:
    def __init__(self, executor: _PausingExecutor, inner: ScopedTransaction) -> None:
        self._executor = executor
        self._inner = inner

    async def run(self, query: Any, /, **params: Any) -> list[Record]:
        if query.name == self._executor.pause_at and not self._executor.reached.is_set():
            self._executor.reached.set()
            await self._executor.go.wait()
        return await self._inner.run(query, **params)


@pytest.mark.parametrize("first", ["story", "task"])
async def test_a_task_claim_and_a_story_claim_never_share_a_task(
    graph_project: GraphProject, tmp_path: Path, first: str
) -> None:
    # Scenario ("story" first): u1's story claim holds the locks of UNSHARED and its task and stops before it writes
    # the task; hotfix's claim_task waits on the task lock; once the story claim commits, the task claim answers
    # wrong_state (on Postgres, READ COMMITTED re-reads the locked task, now in_progress, after the wait).
    # ("task" first): the story claim stops after its unlocked read, before its locks; hotfix claims the task; the story
    # claim then locks, reads again and answers partly_claimed. Every in_progress task carries the winner's token.
    project_id, _ = await _ready_after_setup(graph_project, tmp_path)
    ontology = plan_ontology()
    pausing = _PausingExecutor(graph_project.executor, "plan_set_tasks" if first == "story" else "plan_lock_stories")
    service = PlanService(pausing, ontology, plan_rules(ontology))
    story = asyncio.create_task(service.claim_story(project_id, UNSHARED, claimed_by="u1"))
    await asyncio.wait_for(pausing.reached.wait(), 30)

    task = asyncio.create_task(_lifecycle(graph_project).claim_task(project_id, UNSHARED_TASK, claimed_by="hotfix"))
    if first == "story":
        waited, _ = await asyncio.wait({task}, timeout=2)
        assert not waited, "the task claim did not wait for the story claim's task lock"
    else:
        await asyncio.wait_for(asyncio.shield(task), 30)
    pausing.go.set()
    results = await asyncio.wait_for(asyncio.gather(story, task, return_exceptions=True), 30)

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, TaskStateError)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0].code == ("wrong_state" if first == "story" else "partly_claimed"), results
    stored = await _stored(graph_project, project_id, UNSHARED_TASK)
    assert (stored.status, stored.claim_token) == ("in_progress", winners[0].claim_token)
    assert stored.claimed_by == ("u1" if first == "story" else "hotfix")


async def test_a_held_story_refuses_task_claims_and_a_task_claim_makes_a_story_partly_claimed(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, service = await _ready_after_setup(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    await life.claim_task(project_id, UNSHARED_TASK, claimed_by="hotfix")
    with pytest.raises(TaskStateError) as partly:
        await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    assert (partly.value.code, partly.value.task_id, partly.value.claimed_by) == (
        "partly_claimed",
        UNSHARED_TASK,
        "hotfix",
    )
    await life.release_task(project_id, UNSHARED_TASK, force=True)

    claim = await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    # A plan run added a pending task to the held story: here the claimed task is put back to pending to stand for it.
    await graph_project.update_nodes(
        select("Task", task_id=UNSHARED_TASK),
        {"status": "pending", "claim_token": None, "claimed_by": None, "claimed_at": None},
        project_id=project_id,
    )

    with pytest.raises(TaskStateError) as held:
        await life.claim_task(project_id, UNSHARED_TASK, claimed_by="hotfix")
    with pytest.raises(TaskStateError) as next_ready:
        await life.claim_next(project_id, UNSHARED, claimed_by="hotfix")

    assert (held.value.code, held.value.claimed_by) == ("story_claimed", "u1") and held.value.claimed_at
    assert (next_ready.value.code, next_ready.value.claimed_by) == ("story_claimed", "u1")
    assert claim.claim_token


async def test_the_task_calls_on_a_task_of_a_held_story_are_refused_and_the_host_frees_it_through_the_story(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, service = await _ready_after_setup(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    claim = await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    token = claim.claim_token
    calls = (
        life.complete_task(project_id, UNSHARED_TASK, claim_token=token, commit=COMMIT),
        life.fail_task(project_id, UNSHARED_TASK, claim_token=token),
        life.block_task(project_id, UNSHARED_TASK, claim_token=token, reason="other"),
        life.release_task(project_id, UNSHARED_TASK, claim_token=token),
        life.release_task(project_id, UNSHARED_TASK, force=True),
    )

    for call in calls:
        with pytest.raises(TaskStateError) as held:
            await call
        assert (held.value.code, held.value.claimed_by) == ("story_claimed", "u1")

    assert (await load_plan(graph_project.store.executor(), project_id)).tasks[UNSHARED_TASK].status == "in_progress"
    released = await service.release_story(project_id, UNSHARED, force=True)
    assert UNSHARED_TASK in released.released


async def test_claims_are_refused_while_a_plan_persists_and_without_the_prerequisites(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    service = _service(graph_project)

    with pytest.raises(TaskStateError) as blocked:
        await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    assert blocked.value.code == "not_available" and blocked.value.blockers == (WORKSPACE, TOOLCHAIN)

    await _complete(_lifecycle(graph_project), project_id, WORKSPACE, TOOLCHAIN)
    await graph_project.update_nodes(select("Plan"), {"applying": "run-x"}, project_id=project_id)
    with pytest.raises(TaskStateError) as applying:
        await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    assert applying.value.code == "plan_applying"
    await graph_project.update_nodes(select("Plan"), {"applying": None}, project_id=project_id)
    with pytest.raises(TaskStateError) as unknown:
        await service.claim_story(project_id, "STORY::NOWHERE::X", claimed_by="u1")
    assert unknown.value.code == "unknown_story"


async def test_a_cycle_of_stories_is_claimed_released_and_blocked_as_one_unit(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # Checkout's logic waits on the Stripe contract; the Stripe integration task now waits on checkout's quality task:
    # the two stories wait on each other while the tasks stay acyclic. Claiming one claims both.
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    source, target = ("TASK::PAYMENTS_SERVICE::INTEGRATION_STRIPE_API", "TASK::WEB_SHOP::QUALITY_CHECKOUT")
    await graph_project.create_edges(
        [
            EdgeSpec(
                "WAITS_FOR",
                select("Task", task_id=source),
                select("Task", task_id=target),
                {"group_id": project_id, "edge_key": f"{source}|WAITS_FOR|{target}", "source": "stitch"},
            )
        ],
        project_id=project_id,
    )
    await _complete(
        _lifecycle(graph_project),
        project_id,
        WORKSPACE,
        TOOLCHAIN,
        UNSHARED_TASK,
        "TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API",
        "TASK::PAYMENTS_SERVICE::CONTRACT_INTEGRATED",
        "TASK::PAYMENTS_SERVICE::LOGIC_CARD_PAYMENT",
    )
    service = _service(graph_project)

    claim = await service.claim_story(project_id, CHECKOUT, claimed_by="u1")

    assert set(claim.story_ids) == {CHECKOUT, STRIPE} and claim.story_ids[0] == CHECKOUT
    order = [task.task_id for task in claim.tasks]
    assert order.index("TASK::PAYMENTS_SERVICE::CONTRACT_STRIPE_API") < order.index("TASK::WEB_SHOP::LOGIC_CHECKOUT")
    assert order.index("TASK::WEB_SHOP::QUALITY_CHECKOUT") < order.index(source)
    assert (await service.story_preview(project_id, STRIPE)).claimed_by == "u1"
    with pytest.raises(TaskStateError) as refused:
        await service.claim_story(project_id, STRIPE, claimed_by="u2")
    assert refused.value.code == "story_claimed"
    report = await check_plan(_context(graph_project, project_id, tmp_path))
    assert report.ok, report.problems

    released = await service.release_story(project_id, STRIPE, claimed_by="u1", claim_token=claim.claim_token)

    assert set(released.released) == set(order)
    assert (await service.story_preview(project_id, CHECKOUT)).claimed_by is None


async def test_a_blocked_story_returns_to_pending_through_its_finding_only(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, service = await _ready_after_setup(graph_project, tmp_path, UNSHARED_TASK)
    claim = await service.claim_story(project_id, EXPORT, claimed_by="u1")

    blocked = await service.block_story(
        project_id, EXPORT, claim_token=claim.claim_token, reason="spec_ambiguous", finding_id="FND::A1"
    )

    assert blocked.released == (EXPORT_TASK,)
    task = await _stored(graph_project, project_id, EXPORT_TASK)
    assert (task.status, task.block_reason, task.blocked_by) == ("blocked", "spec_ambiguous", "FND::A1")
    mine = await service.story_list(project_id, view="mine", caller="u1", finding_ids=["FND::A1"])
    assert [(row.story_id, row.state, row.blocked_by) for row in mine] == [(EXPORT, "blocked", ("FND::A1",))]
    assert await service.unblock_finding(project_id, "FND::OTHER") == ()

    assert await service.unblock_finding(project_id, "FND::A1") == (EXPORT_TASK,)

    task = await _stored(graph_project, project_id, EXPORT_TASK)
    assert (task.status, task.blocked_by, task.block_reason) == ("pending", None, None)


async def test_a_plan_run_after_claim_progress_and_block_keeps_the_claim_and_resets_the_blocked_task(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # Scenario: u1 holds PLACE and reported its task done; EXPORT was blocked by a finding; both requirements change
    # meaning; a replan leaves the held story, its task, token and progress alone and sets the blocked task back to
    # pending without its finding.
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    await _complete(life, project_id, WORKSPACE, TOOLCHAIN, UNSHARED_TASK)
    service = _service(graph_project)
    held = await service.claim_story(project_id, PLACE, claimed_by="u1", agent_label="builder")
    await service.report_progress(
        project_id, PLACE_TASK, claimed_by="u1", claim_token=held.claim_token, progress="done", note="built"
    )
    other = await service.claim_story(project_id, EXPORT, claimed_by="u2")
    await service.block_story(
        project_id, EXPORT, claim_token=other.claim_token, reason="spec_missing", finding_id="FND::H"
    )
    changed = {
        spec_id
        for task_id in (PLACE_TASK, EXPORT_TASK)
        for spec_id in (await _stored(graph_project, project_id, task_id)).implements
    }
    for spec_id in sorted(changed):
        await graph_project.update_nodes(
            select("SpecNode", spec_id=spec_id), {"last_build_id": "build-2"}, project_id=project_id
        )
    replan = live_context(
        graph_project,
        project_id,
        open_plan_store(tmp_path),
        script_plan(ScriptedGateway(), shop_answers(await shop_view())),
        build_id="build-2",
        replan=True,
    )

    result = await run_with(replan, StepFunctions())

    assert result.checks is not None and result.checks.ok, result.checks
    assert PLACE_TASK in result.in_progress_changed
    claimed = await _stored(graph_project, project_id, PLACE_TASK)
    assert (claimed.status, claimed.claimed_by, claimed.progress, claimed.note) == (
        "in_progress",
        "u1",
        "done",
        "built",
    )
    story = await service.story_preview(project_id, PLACE)
    assert (story.claimed_by, story.agent_label, story.state) == ("u1", "builder", "in_progress")
    reset = await _stored(graph_project, project_id, EXPORT_TASK)
    assert (reset.status, reset.blocked_by, reset.block_reason) == ("pending", None, None)
    assert await service.unblock_finding(project_id, "FND::H") == ()
    released = await service.release_story(project_id, PLACE, claimed_by="u1", claim_token=held.claim_token)
    assert released.released == (PLACE_TASK,)


async def test_the_story_calls_are_refused_while_a_plan_persists_except_a_forced_release(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # A71: u1 holds UNSHARED; with Plan.applying set, progress, completion, blocking, unblocking and a release with the
    # token answer plan_applying and change nothing; the host's forced release frees the claim.
    project_id, service = await _ready_after_setup(graph_project, tmp_path)
    claim = await service.claim_story(project_id, UNSHARED, claimed_by="u1")
    token = claim.claim_token
    await graph_project.update_nodes(select("Plan"), {"applying": "run-a71"}, project_id=project_id)
    calls = (
        service.report_progress(project_id, UNSHARED_TASK, claimed_by="u1", claim_token=token, progress="done"),
        service.complete_story(project_id, UNSHARED, claim_token=token, completed_by="u1", merge_commit=COMMIT),
        service.block_story(project_id, UNSHARED, claim_token=token, reason="spec_missing", finding_id="FND::A71"),
        service.unblock_finding(project_id, "FND::A71"),
        service.release_story(project_id, UNSHARED, claimed_by="u1", claim_token=token),
    )

    for call in calls:
        with pytest.raises(TaskStateError) as refused:
            await call
        assert refused.value.code == "plan_applying"

    task = await _stored(graph_project, project_id, UNSHARED_TASK)
    assert (task.status, task.claim_token, task.progress) == ("in_progress", token, None)
    released = await service.release_story(project_id, UNSHARED, force=True)
    assert released.released == (UNSHARED_TASK,)
    assert (await _stored(graph_project, project_id, UNSHARED_TASK)).status == "pending"


async def test_progress_reported_while_a_plan_persists_succeeds_or_answers_plan_applying_and_never_hangs(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # A71: a replan persist (it locks a task before its story) runs at the same time as report_progress (it locks the
    # story first); the lock orders differ, so the story call must refuse with plan_applying or finish, never deadlock.
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    await _complete(_lifecycle(graph_project), project_id, WORKSPACE, TOOLCHAIN, UNSHARED_TASK)
    service = _service(graph_project)
    held = await service.claim_story(project_id, PLACE, claimed_by="u1")
    for spec_id in sorted((await _stored(graph_project, project_id, PLACE_TASK)).implements):
        await graph_project.update_nodes(
            select("SpecNode", spec_id=spec_id), {"last_build_id": "build-2"}, project_id=project_id
        )
    replan = live_context(
        graph_project,
        project_id,
        open_plan_store(tmp_path),
        script_plan(ScriptedGateway(), shop_answers(await shop_view())),
        build_id="build-2",
        replan=True,
    )

    run, progress = await asyncio.wait_for(
        asyncio.gather(
            run_with(replan, StepFunctions()),
            service.report_progress(
                project_id, PLACE_TASK, claimed_by="u1", claim_token=held.claim_token, progress="done"
            ),
            return_exceptions=True,
        ),
        30,
    )

    assert not isinstance(run, BaseException), run
    assert run.checks is not None and run.checks.ok, run.checks
    if isinstance(progress, BaseException):
        assert isinstance(progress, TaskStateError) and progress.code == "plan_applying", progress
    else:
        assert (progress.status, progress.progress) == ("in_progress", "done")
    task = await _stored(graph_project, project_id, PLACE_TASK)
    assert (task.status, task.claim_token) == ("in_progress", held.claim_token)
