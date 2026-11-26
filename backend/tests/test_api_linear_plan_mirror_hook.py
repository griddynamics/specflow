"""The hook of the task status mirror: the real status write routes (``claim_story``, ``complete_story``,
``add_finding``) tell ``runtime.linear_plan`` after the write; the runner mirrors in the background and never raises."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES
from api.linear_plan.mirror import TaskChange
from api.linear_plan.runner import LinearPlanRunner
from db import linear_plan as plan_db
from db.models import LinearIssue
from jira_sync_support import T0, basic_plan
from linear_plan_support import FakeStore, Harness
from people_support import API, PeopleRig, serve_people
from specflow_api_agent_support import ALPHA, agent_env

CLAIM, COMPLETE, FINDINGS = (AGENT_ROUTES[name] for name in ("claim_story", "complete_story", "add_finding"))
TASKS = ["TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"]
COMMIT, PR = "a" * 40, "https://git.example.test/org/repo/pull/7"


class Spy:
    """Stands for ``runtime.linear_plan``: records what the routes tell it."""

    def __init__(self) -> None:
        self.told: list[tuple[str, tuple[TaskChange, ...]]] = []

    def mirror_task_status(self, project_id: str, changes: tuple[TaskChange, ...]) -> None:
        self.told.append((project_id, changes))


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


async def claim(people: PeopleRig) -> str:
    return (await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)).json()["claim_token"]


async def test_claim_tells_the_mirror_the_tasks_now_in_progress(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        spy = people.rig.runtime.linear_plan = Spy()
        await claim(people)
    assert spy.told == [(ALPHA, tuple(TaskChange(task, "in_progress") for task in TASKS))]


async def test_completion_tells_the_mirror_the_completed_tasks_with_the_pull_request(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        spy = people.rig.runtime.linear_plan = Spy()
        body = {"story_id": API, "claim_token": token, "merge_commit": COMMIT, "pr_url": PR}
        response = await people.post(COMPLETE, body, project_id=ALPHA)
    assert response.status_code == 200
    assert spy.told == [(ALPHA, tuple(TaskChange(task, "completed", pr_url=PR) for task in TASKS))]


async def test_a_blocking_finding_tells_the_mirror_the_blocked_tasks(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        spy = people.rig.runtime.linear_plan = Spy()
        body = {
            "kind": "gap",
            "text": "The limit is not defined.",
            "about": ["FEAT::API"],
            "story_id": API,
            "claim_token": token,
            "blocking": True,
        }
        response = await people.post(FINDINGS, body, project_id=ALPHA)
    finding_id = response.json()["finding"]["finding_id"]
    assert response.status_code == 200
    assert spy.told == [(ALPHA, tuple(TaskChange(task, "blocked", reason=f"finding {finding_id}") for task in TASKS))]


async def test_a_refused_write_tells_nothing_and_linear_off_changes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        spy = people.rig.runtime.linear_plan = Spy()
        refused = await people.post(
            COMPLETE,
            {"story_id": API, "claim_token": "nope", "merge_commit": COMMIT},
            project_id=ALPHA,
        )
        people.rig.runtime.linear_plan = None
        accepted = await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)
    assert refused.status_code != 200 and spy.told == []
    assert accepted.status_code == 200


# --- the runner ---------------------------------------------------------------------------------------------------


class RunnerRig(Harness):
    """The runner over the harness's rows; ``plan_db.get_item`` answers from them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(basic_plan())
        monkeypatch.setattr(plan_db, "get_item", self.get_item)
        self.runner = LinearPlanRunner(lambda _user: self.gateway(), transaction=self.transaction, graph_store=object())
        self.runner._store = self.store  # the rows of the harness
        self.runner._mirror_deps = self.runner._mirror_deps.__class__(
            store=self.store, gateway_for=self._gateway_for, issue_of=self.runner.issue_of, load_plan=self._load_plan
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield None

    @asynccontextmanager
    async def gateway(self) -> AsyncIterator[Any]:
        yield self.linear

    async def get_item(self, _session: Any, project_id: uuid.UUID, node_id: str) -> LinearIssue | None:
        return self.store.items.get(node_id) if project_id == self.project_id else None


async def test_the_runner_mirrors_in_the_background_and_closing_it_cancels_what_is_left(monkeypatch):
    rig = RunnerRig(monkeypatch)
    await rig.push()
    rig.runner.mirror_task_status(str(rig.project_id), (TaskChange("TASK::A::1", "in_progress"),))
    await asyncio.gather(*rig.runner._mirrors)
    assert rig.issue("TASK::A::1").state.name == "In Progress"
    assert rig.runner._mirrors == set()
    await rig.runner.aclose()
    rig.runner.mirror_task_status(str(rig.project_id), (TaskChange("TASK::A::1", "completed"),))
    assert rig.runner._mirrors == set()


async def test_issue_of_reads_the_row_of_a_node(monkeypatch):
    rig = RunnerRig(monkeypatch)
    rig.store.items["TASK::A::1"] = LinearIssue(
        project_id=rig.project_id, node_id="TASK::A::1", kind="subtask", requested_at=T0
    )
    found = await rig.runner.issue_of(str(rig.project_id), "TASK::A::1")
    assert found is rig.store.items["TASK::A::1"]
    assert await rig.runner.issue_of(str(rig.project_id), "TASK::A::9") is None
    assert isinstance(rig.store, FakeStore)
