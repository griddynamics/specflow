"""Plans, a harness and helpers for the Jira sync engine tests (no tests in here)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.jira.desired import SpecRef
from api.jira.rank import Move
from api.jira.sync import SyncDeps, SyncResult, sync_once
from db.jira import JiraTargetView
from graph.plan.models import CurrentPlan, CurrentTask, PlanInfo, PlanLink, StoryInfo
from graph.plan.reader import LoadedPlan
from jira_db_fakes import FakeJiraDb
from jira_fake_support import FakeJiraGateway

T0 = datetime(2026, 1, 1, tzinfo=UTC)
EPIC_KEY = "PRJ-900"
_REVISION = re.compile(r"_R(\d+)$")


def task(task_id: str, story: str = "STORY::A::ONE", status: str = "pending", **values: Any) -> CurrentTask:
    revision = _REVISION.search(task_id)
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "area_id": "AREA::A",
        "action": _REVISION.sub("", task_id).rsplit("::", 1)[-1],
        "revision": int(revision.group(1)) if revision else 1,
        "story_id": story,
        "phase": "logic",
        "title": f"Task {task_id}",
        "complexity": "medium",
        "planned_by": "model",
        "status": status,
        "created_at": T0,
        "updated_at": T0,
    }
    return CurrentTask(**(defaults | values))


def story(story_id: str, title: str | None = None, status: str = "active", **values: Any) -> StoryInfo:
    defaults: dict[str, Any] = {
        "story_id": story_id,
        "area_id": "AREA::A",
        "kind": "feature",
        "title": title or f"Story {story_id}",
        "status": status,
        "created_at": T0,
        "updated_at": T0,
    }
    return StoryInfo(**(defaults | values))


def link(kind: str, source: str, target: str) -> PlanLink:
    return PlanLink(type=kind, source=source, target=target, edge_key=f"{source}>{target}")


def make_plan(
    stories: Iterable[StoryInfo],
    tasks: Iterable[CurrentTask],
    waits: Iterable[tuple[str, str]] = (),
    implements: Iterable[tuple[str, str]] = (),
    *,
    applying: bool = False,
) -> LoadedPlan:
    """``waits`` are ``(waiter, prerequisite)`` task ids; ``implements`` are ``(story id, spec id)``."""

    links = [link("WAITS_FOR", waiter, prerequisite) for waiter, prerequisite in waits]
    links += [link("IMPLEMENTS", source, target) for source, target in implements]
    info = PlanInfo(plan_id="plan-1", created_at=T0, updated_at=T0, applying=applying)
    return LoadedPlan(CurrentPlan(plan=info, areas=(), stories=tuple(stories), tasks=tuple(tasks), links=tuple(links)))


def basic_plan() -> LoadedPlan:
    """Three stories, A and B with two tasks and C with one; ``TASK::A::2`` waits for ``TASK::A::1``."""

    stories = [story("STORY::A::ONE"), story("STORY::A::TWO"), story("STORY::A::THREE")]
    tasks = [
        task("TASK::A::1", complexity="low", done_when=({"text": "it works", "source": "planner"},)),
        task("TASK::A::2", description="Second.\n\nMore."),
        task("TASK::A::3", "STORY::A::TWO", complexity="high"),
        task("TASK::A::4", "STORY::A::TWO"),
        task("TASK::A::5", "STORY::A::THREE", "completed"),
    ]
    return make_plan(stories, tasks, waits=[("TASK::A::2", "TASK::A::1")])


class Harness:
    """A project with an enabled target, the fake Jira and the fake repository; ``sync`` runs one pass. ``share``
    puts a second project on the same fakes."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        plan: LoadedPlan | None = None,
        *,
        epic_key: str | None = None,
        public_url: str | None = None,
        specs: Mapping[str, SpecRef] | None = None,
        share: Harness | None = None,
    ) -> None:
        self.project_id = uuid.uuid4()
        self.plan = plan or basic_plan()
        self.specs: Mapping[str, SpecRef] = specs or {}
        self.public_url = public_url
        self.gateway = share.gateway if share else FakeJiraGateway()
        self.db = share.db if share else FakeJiraDb()
        self.clock = T0
        self.slept: list[float] = []
        self.project_name = "Shop"
        self.db.project_names[self.project_id] = self.project_name
        self.db.targets[self.project_id] = JiraTargetView(
            project_id=self.project_id,
            project_name=self.project_name,
            jira_project="PRJ",
            epic_key=epic_key,
            enabled=True,
            updated_at=T0,
            last_status=None,
            last_trigger=None,
            last_started_at=None,
            last_finished_at=None,
            last_error_code=None,
            last_counts=None,
        )
        if epic_key:
            self.gateway.add_issue({"summary": "Owner epic", "issuetype": {"name": "Epic"}}, key=epic_key)
        if share is None:
            self.db.install(monkeypatch)
            self.gateway.on_call = self._no_transaction
            self.gateway.now = lambda: self.clock

    def _no_transaction(self, method: str) -> None:
        assert not self.db.in_transaction, f"a transaction is open during the Jira call {method}"

    async def _load_plan(self, _project_id: str) -> LoadedPlan:
        return self.plan

    async def _spec_names(self, _project_id: str, ids: Sequence[str]) -> Mapping[str, SpecRef]:
        return {spec: self.specs[spec] for spec in ids if spec in self.specs}

    def deps(self) -> SyncDeps:
        return SyncDeps(
            gateway=self.gateway,
            transaction=self.db.transaction,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            public_url=self.public_url,
            now=self._now,
            sleep=self._sleep,
        )

    def _now(self) -> datetime:
        self.clock += timedelta(seconds=1)
        return self.clock

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock += timedelta(seconds=seconds)

    async def sync(self) -> SyncResult:
        return await sync_once(str(self.project_id), self.deps())

    def row(self, node_id: str) -> Any:
        return self.db.issues[(self.project_id, node_id)]

    def key(self, node_id: str) -> str:
        return str(self.row(node_id).jira_key)

    def calls_since(self, mark: int) -> list[str]:
        return [name for name, _ in self.gateway.calls[mark:]]

    def set_plan(self, plan: LoadedPlan) -> None:
        self.plan = plan


def apply_moves(order: Sequence[str], moves: Iterable[Move]) -> list[str]:
    """What the rank calls do to ``order`` (the fake's rule): the keys leave, then land before or after the anchor."""

    result = list(order)
    for move in moves:
        result = [node for node in result if node not in move.keys]
        anchor = move.before if move.before is not None else move.after
        at = result.index(str(anchor)) + (0 if move.before is not None else 1)
        result[at:at] = move.keys
    return result


def edit_task(plan: LoadedPlan, task_id: str, **values: Any) -> LoadedPlan:
    tasks = tuple(t.model_copy(update=values) if t.task_id == task_id else t for t in plan.current.tasks)
    return LoadedPlan(plan.current.model_copy(update={"tasks": tasks}))


def edit_story(plan: LoadedPlan, story_id: str, **values: Any) -> LoadedPlan:
    stories = tuple(s.model_copy(update=values) if s.story_id == story_id else s for s in plan.current.stories)
    return LoadedPlan(plan.current.model_copy(update={"stories": stories}))


def with_plan(
    plan: LoadedPlan,
    *,
    tasks: Iterable[CurrentTask] = (),
    stories: Iterable[StoryInfo] = (),
    waits: Iterable[tuple[str, str]] = (),
    unwait: Iterable[tuple[str, str]] = (),
) -> LoadedPlan:
    """``plan`` plus tasks, stories and waits, minus the ``unwait`` pairs."""

    current = plan.current
    drop = {link("WAITS_FOR", waiter, prerequisite).edge_key for waiter, prerequisite in unwait}
    links = tuple(item for item in current.links if item.edge_key not in drop)
    links += tuple(link("WAITS_FOR", waiter, prerequisite) for waiter, prerequisite in waits)
    update = {"tasks": (*current.tasks, *tasks), "stories": (*current.stories, *stories), "links": links}
    return LoadedPlan(current.model_copy(update=update))


def comment_lead(doc: Mapping[str, Any]) -> str:
    return str(doc["content"][0]["content"][0]["text"])


def comment_items(doc: Mapping[str, Any]) -> list[str]:
    lists = [block for block in doc["content"] if block["type"] == "bulletList"]
    if not lists:
        return []
    return [str(item["content"][0]["content"][0]["text"]) for item in lists[0]["content"]]
