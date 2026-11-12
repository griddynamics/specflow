"""The blocking link: ``FindingFlow`` composes ``Findings`` with the real ``PlanService`` story calls (in-memory plan
and findings) in the order match -> guard -> add -> block_story, and closing unblocks (gap.md "Blocking link").

Sequence of a blocking ``add_finding`` (agent u1 holds story API under token T):

    agent        FindingFlow            Findings              PlanService
      |--add_finding-->|--match------------>|                       |
      |                |<--none-------------|                       |
      |                |--check_block(T)----------------------------->|  holds the story, node implemented?
      |                |--add(blocking)---->| (finding + edges)      |
      |                |--block_story(T, reason, finding_id)--------->|  tasks -> blocked, claim ends
      |                |   (on failure: withdraw)                    |
      |<--finding, blocked task ids                                 |
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from gap_support import Clock, MemoryGapExecutor, spec_graph
from graph.core.errors import ConfigurationError
from graph.core.ontology import load_ontology
from graph.core.reader import GraphNode
from graph.gap import FindingError, FindingFlow, Findings, NewFinding
from graph.plan import PlanService, TaskStateError
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import MemoryPlanExecutor, mini_plan

PROJECT = "shop"
API, API_CONTRACT, API_LOGIC = "STORY::A::API", "TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"
UI, UI_SCREEN = "STORY::A::UI", "TASK::A::UI_SCREEN"
CARD, FEATURE, MODULE = "REQ::FR::CARD_PAYMENT", "FEAT::CARD_PAYMENT", "MOD::PAYMENTS_SERVICE"


class NodesOnly:
    """The one spec graph read the guard needs: the nodes with their aliases."""

    def __init__(self, memory: MemoryGapExecutor) -> None:
        self._memory = memory

    async def nodes(self, _project_id: str) -> list[Any]:
        return [
            GraphNode(
                spec_id=spec_id,
                uuid=node["uuid"],
                label=node["label"],
                name=spec_id,
                spec_id_aliases=tuple(node["aliases"]),
            )
            for spec_id, node in self._memory.specs.items()
        ]


class World:
    """One project: the plan of ``mini_plan`` (API implements CARD and FEATURE), the findings, and the flow."""

    def __init__(self) -> None:
        self.plan_store: MemoryPlanExecutor = mini_plan()
        self.plan_store.set_task(API_CONTRACT, implements=[FEATURE])
        self.plan_store.set_task(API_LOGIC, implements=[CARD])
        self.plan_store.set_task(UI_SCREEN, implements=[MODULE])
        self.gap_store = spec_graph(MemoryGapExecutor())
        ontology = plan_ontology()
        self.plan = PlanService(self.plan_store, ontology, plan_rules(ontology), graph=NodesOnly(self.gap_store))
        self.findings = Findings(self.gap_store, load_ontology(), clock=Clock())
        self.flow = FindingFlow(self.findings, self.plan)

    async def claim(self, story_id: str = API, user: str = "u1") -> str:
        return (await self.plan.claim_story(PROJECT, story_id, claimed_by=user)).claim_token

    async def add(self, token: str | None, **values: Any) -> Any:
        arguments: dict[str, Any] = {
            "created_by": "u1",
            "kind": "question",
            "text": "Is the amount in cents?",
            "about": (CARD,),
            "story_id": API,
            "blocking": True,
        }
        new = NewFinding(
            **{**arguments, **{k: (tuple(v) if k in {"about", "refers_to"} else v) for k, v in values.items()}}
        )
        return await self.flow.add_finding(PROJECT, new, claim_token=token)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.plan_store.tasks[task_id]


@pytest.fixture
def world() -> World:
    return World()


@pytest.mark.parametrize(
    ("kind", "reason"), [("question", "spec_ambiguous"), ("conflict", "spec_contradiction"), ("gap", "spec_missing")]
)
async def test_a_blocking_finding_blocks_the_claimed_tasks_with_the_mapped_reason_and_ends_the_claim(
    world: World, kind: str, reason: str
) -> None:
    token = await world.claim()

    added = await world.add(token, kind=kind)

    assert added.created and added.finding.blocking and added.finding.story_id == API
    assert set(added.blocked) == {API_CONTRACT, API_LOGIC}
    for task_id in (API_CONTRACT, API_LOGIC):
        task = world.task(task_id)
        assert (task["status"], task["block_reason"], task["blocked_by"]) == (
            "blocked",
            reason,
            added.finding.finding_id,
        )
        assert task.get("claimed_by") is None and task.get("claim_token") is None
    assert world.plan_store.stories[API].get("claimed_by") is None  # the claim ended
    assert world.plan_store.stories[API]["claim_token"] == token  # kept until the next claim (D23)
    stored = world.gap_store.findings()[added.finding.finding_id]
    assert stored["status"] == "open" and stored["blocking"] is True


@pytest.mark.parametrize("how", ["resolve", "dismiss"])
async def test_resolving_or_dismissing_a_blocking_finding_returns_its_tasks_to_pending(world: World, how: str) -> None:
    # Scenario: u1 blocks API; a person closes the finding (resolve, or dismiss: A20); the tasks are ready again.
    token = await world.claim()
    added = await world.add(token)

    closed = await world.flow.close_finding(
        PROJECT, added.finding.finding_id, by="lead", note="cents", dismiss=how == "dismiss"
    )

    assert closed.finding.status == {"resolve": "resolved", "dismiss": "dismissed"}[how]
    assert set(closed.unblocked) == {API_CONTRACT, API_LOGIC}
    for task_id in (API_CONTRACT, API_LOGIC):
        task = world.task(task_id)
        assert task["status"] == "pending" and task.get("blocked_by") is None and task.get("block_reason") is None
    assert (await world.plan.claim_story(PROJECT, API, claimed_by="u2")).tasks  # claimable by someone else again


async def test_a_task_a_plan_run_already_reset_is_left_alone_and_the_finding_stays_open_until_closed(
    world: World,
) -> None:
    # Scenario: the finding blocks API; a plan run resets API_LOGIC (status pending, block cleared) because its node
    # changed; the finding is still open; closing it unblocks only what is still blocked by it.
    token = await world.claim()
    added = await world.add(token)
    world.plan_store.set_task(
        API_LOGIC, status="pending", block_reason=None, blocked_by=None, note="reset by a plan run"
    )

    current = await world.findings.finding(PROJECT, added.finding.finding_id)

    assert current is not None and current.status == "open"
    closed = await world.flow.close_finding(PROJECT, added.finding.finding_id, by="lead")

    assert closed.unblocked == (API_CONTRACT,)
    assert world.task(API_LOGIC)["note"] == "reset by a plan run" and world.task(API_LOGIC)["status"] == "pending"


async def test_a_non_blocking_finding_never_touches_the_plan(world: World) -> None:
    token = await world.claim()

    added = await world.add(token, blocking=False, kind="note")
    plain = await world.add(None, blocking=False, kind="gap", story_id=None, text="no claim")

    assert added.created and not added.finding.blocking and added.blocked == ()
    assert plain.created and plain.finding.story_id is None
    assert world.task(API_LOGIC)["status"] == "in_progress" and world.plan_store.stories[API]["claimed_by"] == "u1"
    closed = await world.flow.close_finding(PROJECT, added.finding.finding_id, by="lead")
    assert closed.unblocked == ()


async def test_the_dedupe_is_checked_before_the_guard_so_a_repeat_never_needs_a_claim(world: World) -> None:
    # Scenario: after the block the claim is gone; the same add (no token, nobody holds the story) answers the open
    # finding.
    token = await world.claim()
    first = await world.add(token)

    again = await world.add(token)
    without_token = await world.add(None)

    assert not again.created and not without_token.created and again.blocked == ()
    assert again.finding.finding_id == without_token.finding.finding_id == first.finding.finding_id
    assert len(world.gap_store.findings()) == 1


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"token": "wrong"}, TaskStateError),
        ({"token": None}, TaskStateError),
        ({"created_by": "u2"}, TaskStateError),  # another user's finding about a story u1 holds
        ({"about": [MODULE]}, TaskStateError),  # no node of the story implements it
        ({"story_id": UI}, TaskStateError),  # a story u1 does not hold
        ({"story_id": "STORY::A::NONE"}, TaskStateError),
        ({"kind": "note"}, ConfigurationError),
        ({"story_id": None}, ConfigurationError),
        ({"about": ["REQ::FR::NOPE"]}, FindingError),  # node_not_found before the dedupe and the guard
        ({"about": [CARD, "REQ::FR::NOPE"]}, FindingError),  # one unknown id among known ones: the same
    ],
)
async def test_the_guard_refuses_before_anything_is_written(
    world: World, change: dict[str, Any], error: type[Exception]
) -> None:
    token = await world.claim()
    arguments = dict(change)
    asked = arguments.pop("token", token) if "token" in arguments else token

    with pytest.raises(error) as caught:
        await world.add(asked, **arguments)

    if error is TaskStateError:
        assert isinstance(caught.value, TaskStateError) and caught.value.code == "claim_mismatch"
    assert world.gap_store.findings() == {} and world.gap_store.edges == {}
    assert world.task(API_LOGIC)["status"] == "in_progress" and world.plan_store.stories[API]["claimed_by"] == "u1"


async def test_when_the_block_fails_after_the_guard_the_finding_is_withdrawn(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scenario (FR-GAP-10): u1 holds API; the guard passes; the story is released before block_story runs; block_story
    # answers claim_mismatch; the finding written in between is withdrawn and no task changed.
    token = await world.claim()
    real = world.plan.check_block

    async def release_after_the_guard(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        await world.plan.release_story(PROJECT, API, claimed_by="u1", claim_token=token)

    monkeypatch.setattr(world.plan, "check_block", release_after_the_guard)

    with pytest.raises(TaskStateError) as caught:
        await world.add(token, refers_to=[("commit", "abcdef1")])

    assert caught.value.code == "claim_mismatch"
    assert world.gap_store.findings() == {} and world.gap_store.refs() == [] and world.gap_store.edges == {}
    assert {world.task(API_CONTRACT)["status"], world.task(API_LOGIC)["status"]} == {"pending"}


async def test_the_block_error_wins_when_the_withdraw_fails_too(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await world.claim()
    await world.plan.release_story(PROJECT, API, claimed_by="u1", claim_token=token)
    monkeypatch.setattr(world.plan, "check_block", _passes)
    world.gap_store.fail_on = "gap_delete_open_finding"

    with caplog.at_level(logging.WARNING), pytest.raises(TaskStateError) as caught:
        await world.add(token)

    assert caught.value.code == "claim_mismatch"
    assert [r.getMessage() for r in caplog.records if r.getMessage() == "graph.gap.withdraw_failed"]
    assert "Is the amount" not in caplog.text


async def _passes(*_args: Any, **_kwargs: Any) -> None:
    return None


async def test_closing_a_closed_blocking_finding_repeats_the_unblock_so_a_lost_one_heals(world: World) -> None:
    # Scenario: the finding is resolved but the unblock was lost (tasks blocked again); a second close raises
    # wrong_state and still returns the tasks to pending.
    token = await world.claim()
    added = await world.add(token)
    await world.flow.close_finding(PROJECT, added.finding.finding_id, by="lead")
    for task_id in (API_CONTRACT, API_LOGIC):
        world.plan_store.set_task(
            task_id, status="blocked", block_reason="spec_ambiguous", blocked_by=added.finding.finding_id
        )

    with pytest.raises(FindingError) as caught:
        await world.flow.close_finding(PROJECT, added.finding.finding_id, by="lead")

    assert caught.value.code == "wrong_state" and world.task(API_LOGIC)["status"] == "pending"
    with pytest.raises(FindingError) as unknown:
        await world.flow.close_finding(PROJECT, "FND::NONE00000000", by="lead")
    assert unknown.value.code == "unknown_finding"


async def test_a_merged_id_in_about_still_passes_the_guard_through_aliases(world: World) -> None:
    # Scenario: the task implements REQ::FR::REFUND, which a build merged into CARD; a finding about CARD blocks the
    # story.
    world.plan_store.set_task(API_LOGIC, implements=["REQ::FR::REFUND"])
    world.gap_store.merge_spec("REQ::FR::REFUND", CARD)
    token = await world.claim()

    added = await world.add(token)

    assert added.created and API_LOGIC in added.blocked


async def test_a_cancelled_block_still_withdraws_the_finding_and_stays_cancelled(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scenario: the caller is cancelled while block_story runs; the compensating withdraw is shielded and completes,
    # then the cancellation propagates.
    token = await world.claim()
    started = asyncio.Event()

    async def hang(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(world.plan, "block_story", hang)
    task = asyncio.ensure_future(world.add(token))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert world.gap_store.findings() == {} and world.gap_store.edges == {}
