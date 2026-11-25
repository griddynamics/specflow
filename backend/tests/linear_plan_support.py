"""A harness for the Linear plan pass (no tests in here): the pass on ``linear.fake.FakeLinear`` and in-memory rows. The
plans are the Jira sync tests' plans."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from api.linear_plan.gateway import NotConnectedError
from api.linear_plan.store import TargetChangedError
from api.linear_plan.sync import PushDeps, PushResult, push_once
from db import linear_plan as plan_db
from db.linear_plan import LinearTargetView, ProjectChoice, TargetSaved
from db.models import LinearIssue
from graph.plan.reader import LoadedPlan
from jira_sync_support import T0, basic_plan, make_plan, story, task
from linear.errors import LinearError
from linear.fake import FakeLinear
from linear.ids import KIND_STORY, KIND_TASK, node_uuid
from linear.models import Issue, Team

PUBLIC_URL = "https://app.example.test"
STORIES = ["STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"]


def crossing_plan(*, title: str | None = None, drop: str | None = None, release: str | None = None) -> LoadedPlan:
    """Three stories; ``TASK::A::2`` and ``TASK::A::3`` wait for ``TASK::A::1`` (so story TWO waits for story ONE)."""

    tasks = [
        task("TASK::A::1", complexity="low", done_when=({"text": "it works", "source": "planner"},)),
        task("TASK::A::2", description="Second.\n\nMore."),
        task("TASK::A::3", "STORY::A::TWO", complexity="high", title=title or "Task TASK::A::3"),
        task("TASK::A::4", "STORY::A::TWO"),
        task("TASK::A::5", "STORY::A::THREE", "completed"),
    ]
    stories = [story(STORIES[0], release=release), story(STORIES[1], release="R2"), story(STORIES[2])]
    waits = [("TASK::A::2", "TASK::A::1"), ("TASK::A::3", "TASK::A::1")]
    return make_plan(stories, [t for t in tasks if t.task_id != drop], waits=waits)


class FakeStore:
    """``PlanStore`` over a dict; ``changed`` makes the next write raise ``TargetChangedError``."""

    def __init__(self, target: LinearTargetView | None) -> None:
        self.target = target
        self.items: dict[str, LinearIssue] = {}
        self.changed = False

    async def get_target(self, project_id: uuid.UUID) -> LinearTargetView | None:
        return self.target if self.target is not None and self.target.project_id == project_id else None

    async def list_items(self, project_id: uuid.UUID) -> list[LinearIssue]:
        found = [item for item in self.items.values() if item.project_id == project_id]
        return sorted(found, key=lambda item: item.node_id)

    async def add_pending(self, target: LinearTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        if self.changed:
            raise TargetChangedError
        if node_id not in self.items:
            self.items[node_id] = LinearIssue(
                project_id=target.project_id, node_id=node_id, kind=kind, requested_at=requested_at
            )

    async def update_item(self, target: LinearTargetView, node_id: str, **columns: Any) -> None:
        if self.changed or self.target is None or target.project_id != self.target.project_id:
            raise TargetChangedError
        for name, value in columns.items():
            setattr(self.items[node_id], name, value)


def target_view(
    project_id: uuid.UUID, team: Team, linear_project: str, *, enabled: bool = True, url: str | None = None
) -> LinearTargetView:
    return LinearTargetView(
        project_id=project_id,
        team_id=team.id,
        team_key=team.key,
        linear_project_id=linear_project,
        linear_project_name="Shop",
        linear_project_url=url,
        created_by_graphico=True,
        enabled=enabled,
        updated_at=T0,
        last_status=None,
        last_trigger=None,
        last_started_at=None,
        last_finished_at=None,
        last_error_code=None,
        last_counts=None,
    )


class Harness:
    """A project with an enabled target on a fake Linear team, and the fake rows; ``push`` runs one pass."""

    def __init__(self, plan: LoadedPlan | None = None, *, enabled: bool = True, **fake: Any) -> None:
        self.project_id = uuid.uuid4()
        self.plan = plan or basic_plan()
        self.linear = FakeLinear(**fake)
        self.team = self.linear.add_team("ENG")
        self.linear_project = self.linear.add_project(self.team.id, "Shop")
        self.store = FakeStore(
            target_view(
                self.project_id, self.team, self.linear_project.id, enabled=enabled, url=self.linear_project.url
            )
        )
        self.gateway_error: Exception | None = None
        self.public_url: str | None = PUBLIC_URL

    @asynccontextmanager
    async def _gateway_for(self, _project_id: uuid.UUID) -> AsyncIterator[FakeLinear]:
        if self.gateway_error is not None:
            raise self.gateway_error
        yield self.linear

    async def _load_plan(self, _project_id: str) -> LoadedPlan:
        return self.plan

    async def _spec_names(self, _project_id: str, _ids: Sequence[str]) -> dict[str, Any]:
        return {}

    def deps(self) -> PushDeps:
        return PushDeps(
            store=self.store,
            gateway_for=self._gateway_for,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            now=lambda: datetime.now(UTC),
            public_url=self.public_url,
        )

    async def push(self) -> PushResult:
        return await push_once(str(self.project_id), self.deps())

    # What the tests read ---------------------------------------------------------------------------------------------

    def issue_uuid(self, node_id: str) -> str:
        kind = KIND_STORY if node_id.startswith("STORY") else KIND_TASK
        return str(node_uuid(self.project_id, kind, node_id))

    def issue(self, node_id: str) -> Issue:
        return next(issue for issue in self.linear.all_issues() if issue.id == self.issue_uuid(node_id))

    def writes(self) -> list[str]:
        return [name for name, _ in self.linear.writes]

    def fail(self, error: LinearError | NotConnectedError) -> None:
        self.gateway_error = error


class FakePlanDb:
    """The target functions of ``db.linear_plan`` over one dict."""

    def __init__(self) -> None:
        self.targets: dict[uuid.UUID, LinearTargetView] = {}
        self.items: dict[uuid.UUID, list[LinearIssue]] = {}
        self.started: list[tuple[str, datetime]] = []  # (trigger, queued at) of every ``record_start``
        self.ended: list[tuple[str, str | None, dict[str, int]]] = []  # (status, error code, counts) of every end
        self.running: list[uuid.UUID] = []  # what ``running_targets`` answers

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("get_target", "save_target", "record_start", "record_end", "running_targets", "list_items"):
            monkeypatch.setattr(plan_db, name, getattr(self, name))

    async def get_target(self, _session: Any, project_id: uuid.UUID) -> LinearTargetView | None:
        return self.targets.get(project_id)

    def _set(self, project_id: uuid.UUID, **changes: Any) -> None:
        self.targets[project_id] = replace(self.targets[project_id], **changes)

    async def save_target(self, _session: Any, project_id: uuid.UUID, **given: Any) -> TargetSaved:
        project: ProjectChoice = given["project"]
        keys = (given["team_id"], project.linear_project_id)
        old = self.targets.get(project_id)
        self.targets[project_id] = target_view(
            project_id, SimpleNamespace(id=keys[0], key=given["team_key"]), keys[1], enabled=given["enabled"]
        )
        self._set(
            project_id,
            linear_project_name=project.name,
            linear_project_url=project.url,
            created_by_graphico=project.created_by_graphico,
            updated_at=given["now"],
        )
        moved = old is None or (old.team_id, old.linear_project_id) != keys
        return TargetSaved(
            destination_changed=old is not None and moved,
            turned_on=given["enabled"] and not (old is not None and old.enabled),
            keys_changed=moved,
        )

    async def record_start(self, _session: Any, project_id: uuid.UUID, **given: Any) -> None:
        self.started.append((given["trigger"], given["queued_at"]))
        self._set(project_id, last_status="running", last_trigger=given["trigger"], last_started_at=given["queued_at"])

    async def record_end(self, _session: Any, project_id: uuid.UUID, **values: Any) -> None:
        self.ended.append((values["status"], values["error_code"], values["counts"]))
        names = {"status": "last_status", "error_code": "last_error_code", "counts": "last_counts"}
        self._set(project_id, **{column: values[key] for key, column in names.items()})

    async def running_targets(self, _session: Any) -> list[uuid.UUID]:
        return self.running

    async def list_items(self, _session: Any, project_id: uuid.UUID) -> list[LinearIssue]:
        return list(self.items.get(project_id, []))
