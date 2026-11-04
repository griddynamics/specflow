"""Live task lifecycle on the persisted shop plan: readiness, claims, transitions, races and stale claims."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from graph.plan.errors import TaskStateError
from graph.plan.files import open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.reader import PlanReader
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
from graph_probes.base import select
from graph_support import GraphProject, ScriptedGateway

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

WORKSPACE = "TASK::PROJECT::WORKSPACE"
TOOLCHAIN = "TASK::PROJECT::TOOLCHAIN"
CHECKOUT = "TASK::WEB_SHOP::LOGIC_CHECKOUT"
COMMIT = "ab" * 20
UNKNOWN_CLAIM = "0" * 32


def _lifecycle(graph_project: GraphProject, clock: datetime | None = None) -> TaskLifecycle:
    ontology = plan_ontology()
    return TaskLifecycle(
        graph_project.store, ontology, plan_rules(ontology), clock=(lambda: clock) if clock is not None else None
    )


async def _complete(life: TaskLifecycle, project_id: str, task_id: str) -> None:
    claim = await life.claim_task(project_id, task_id, claimed_by="agent")
    await life.complete_task(project_id, task_id, claim_token=claim.claim_token, commit=COMMIT)


async def test_only_the_setup_root_is_ready_and_a_blocked_claim_lists_its_prerequisites(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)

    assert [task.task_id for task in await life.ready_tasks(project_id)] == [WORKSPACE]
    with pytest.raises(TaskStateError) as raised:
        await life.claim_task(project_id, CHECKOUT, claimed_by="agent")

    assert raised.value.code == "not_ready"
    assert raised.value.blockers[:2] == (WORKSPACE, TOOLCHAIN)
    assert "TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API" in raised.value.blockers


async def test_a_claim_moves_through_its_transitions_with_its_token(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)

    with pytest.raises(TaskStateError) as pending:
        await life.complete_task(project_id, WORKSPACE, claim_token=UNKNOWN_CLAIM, commit=COMMIT)
    assert pending.value.code == "wrong_state"

    first = await life.claim_task(project_id, WORKSPACE, claimed_by="agent-1")
    assert first.task.status == "in_progress" and first.task.attempts == 1 and first.task.claimed_by == "agent-1"
    await life.release_task(project_id, WORKSPACE, claim_token=first.claim_token, note="stopping")
    second = await life.claim_task(project_id, WORKSPACE, claimed_by="agent-2")

    with pytest.raises(TaskStateError) as stale:
        await life.complete_task(project_id, WORKSPACE, claim_token=first.claim_token, commit=COMMIT)
    assert stale.value.code == "claim_mismatch"

    done = await life.complete_task(project_id, WORKSPACE, claim_token=second.claim_token, commit=COMMIT.upper())
    assert done.status == "completed" and done.commit == COMMIT and done.claimed_by is None
    assert done.attempts == 2
    assert [task.task_id for task in await life.ready_tasks(project_id)] == [TOOLCHAIN]


async def test_fail_retry_block_unblock_and_forced_release(graph_project: GraphProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)

    claim = await life.claim_task(project_id, WORKSPACE, claimed_by="agent")
    failed = await life.fail_task(project_id, WORKSPACE, claim_token=claim.claim_token, note="tests red")
    assert failed.status == "failed" and failed.note == "tests red"
    assert (await life.retry_task(project_id, WORKSPACE)).status == "pending"

    claim = await life.claim_task(project_id, WORKSPACE, claimed_by="agent")
    blocked = await life.block_task(project_id, WORKSPACE, claim_token=claim.claim_token, reason="spec_missing")
    assert blocked.status == "blocked" and blocked.block_reason == "spec_missing"
    unblocked = await life.unblock_task(project_id, WORKSPACE)
    assert unblocked.status == "pending" and unblocked.block_reason is None

    await life.claim_task(project_id, WORKSPACE, claimed_by="crashed-agent")
    released = await life.release_task(project_id, WORKSPACE, force=True)
    assert released.status == "pending" and released.claimed_by is None

    with pytest.raises(TaskStateError) as unknown:
        await life.retry_task(project_id, "TASK::PROJECT::NOPE")
    assert unknown.value.code == "unknown_task"


async def test_exactly_one_of_ten_concurrent_claims_wins(graph_project: GraphProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)

    results = await asyncio.gather(
        *(life.claim_task(project_id, WORKSPACE, claimed_by=f"agent-{index}") for index in range(10)),
        return_exceptions=True,
    )

    claims = [result for result in results if not isinstance(result, BaseException)]
    refused = [result for result in results if isinstance(result, TaskStateError)]
    assert len(claims) == 1 and len(refused) == 9
    assert {error.code for error in refused} == {"wrong_state"}


async def test_claim_next_takes_the_first_ready_task_or_lists_outside_blockers(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)

    with pytest.raises(TaskStateError) as nothing:
        await life.claim_next(project_id, "STORY::WEB_SHOP::CHECKOUT", claimed_by="agent")
    assert nothing.value.code == "nothing_ready"
    assert WORKSPACE in nothing.value.blockers
    assert not [task_id for task_id in nothing.value.blockers if task_id.startswith("TASK::WEB_SHOP::")]

    claim = await life.claim_next(project_id, "STORY::PROJECT::SETUP", claimed_by="agent")
    assert claim.task.task_id == WORKSPACE

    with pytest.raises(TaskStateError) as unknown:
        await life.claim_next(project_id, "STORY::NOWHERE::X", claimed_by="agent")
    assert unknown.value.code == "unknown_story"


async def test_no_task_is_ready_or_claimable_while_a_plan_persists(graph_project: GraphProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    await graph_project.update_nodes(select("Plan"), {"applying": "run-x"}, project_id=project_id)

    assert await life.ready_tasks(project_id) == []
    with pytest.raises(TaskStateError) as applying:
        await life.claim_task(project_id, WORKSPACE, claimed_by="agent")
    assert applying.value.code == "plan_applying"

    await graph_project.update_nodes(select("Plan"), {"applying": None}, project_id=project_id)
    assert [task.task_id for task in await life.ready_tasks(project_id)] == [WORKSPACE]


async def test_stale_claims_are_those_older_than_the_limit(graph_project: GraphProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    await _complete(_lifecycle(graph_project, start), project_id, WORKSPACE)
    await _lifecycle(graph_project, start).claim_task(project_id, TOOLCHAIN, claimed_by="old")

    later = _lifecycle(graph_project, start + timedelta(hours=2))
    assert [task.task_id for task in await later.stale_claims(project_id, older_than=timedelta(hours=1))] == [TOOLCHAIN]
    sooner = _lifecycle(graph_project, start + timedelta(minutes=5))
    assert await sooner.stale_claims(project_id, older_than=timedelta(hours=1)) == []


async def test_a_task_claimed_while_a_plan_run_writes_is_left_alone_as_a_conflict(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, context = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    await _complete(life, project_id, WORKSPACE)
    await _complete(life, project_id, TOOLCHAIN)
    for spec_id in ("ENT::ORDER", "REQ::DATA::ORDER_FIELDS"):
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
    functions = StepFunctions()
    bound = replan.with_run_id(await functions.snapshot(replan))
    for name in ("partition", "describe", "project", "tasks", "stitch", "review", "diff"):
        await getattr(functions, name)(bound)
    ready = await life.ready_tasks(project_id)
    claimed = await life.claim_task(project_id, ready[0].task_id, claimed_by="agent")

    result = await run_with(bound, functions)

    ontology = plan_ontology()
    reader = PlanReader(graph_project.store, ontology, plan_rules(ontology))
    task = await reader.task(project_id, claimed.task.task_id)
    assert task is not None and task.status == "in_progress" and task.claimed_by == "agent"
    assert result.conflicts == (claimed.task.task_id,)
    assert context.project_id == project_id


async def test_claim_next_takes_ready_tasks_in_order_and_refuses_while_a_plan_persists(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(graph_project, tmp_path)
    life = _lifecycle(graph_project)
    await _complete(life, project_id, WORKSPACE)
    await _complete(life, project_id, TOOLCHAIN)
    story = "STORY::PAYMENTS_SERVICE::STRIPE_API"
    ready = [task.task_id for task in await life.ready_tasks(project_id, story_id=story)]
    assert ready == ["TASK::PAYMENTS_SERVICE::CONTRACT_STRIPE_API"]

    first = await life.claim_next(project_id, story, claimed_by="agent-1")
    with pytest.raises(TaskStateError) as waiting:
        await life.claim_next(project_id, story, claimed_by="agent-2")

    assert first.task.task_id == ready[0]
    assert waiting.value.code == "nothing_ready" and ready[0] in waiting.value.blockers

    await graph_project.update_nodes(select("Plan"), {"applying": "run-x"}, project_id=project_id)
    with pytest.raises(TaskStateError) as applying:
        await life.claim_next(project_id, "STORY::ORDERS_SERVICE::ORDERS_API", claimed_by="agent")
    assert applying.value.code == "plan_applying"
