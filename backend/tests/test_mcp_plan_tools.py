"""The plan tools over HTTP (docs/mcp-server/tools.md, list_tasks and get_task_brief): the SDK client session, tokens
from the loopback OIDC IdP, the real tool registry over the REST ``FakeRepo`` and fake plan reads.

Boundaries faked: Postgres (``FakeRepo``) and the plan library's reads (``FakePlanReads``: the reader's plan, tasks,
areas and stories, and the lifecycle's ready tasks and task brief), so the tools are tested on their own contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette

from api.mcp.errors import MESSAGES
from api.mcp.registry import TOOLS, install
from api.mcp.tools.graph import encode_feature_cursor
from graph.plan import PlanArea, PlanStory, PlanTask, TaskBrief, TaskStateError
from graph.plan.models import DoneWhen, PlanInfo
from graph_support import shipped_ontology
from mcp_support import (
    MCP_URL,
    build_app,
    call,
    code,
    fake_runtime,
    mcp_env,
    mcp_token,
    owned_project,
    sdk_session,
    structured,
    text,
)
from test_specflow_api_fakes import FakeRepo

ALICE = mcp_token("alice@example.com")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _task(task_id: str, status: str = "pending", **values: Any) -> PlanTask:
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "area_id": "AREA::ORDERS",
        "action": task_id.rsplit("::", 1)[-1],
        "revision": 1,
        "story_id": "STORY::ORDERS::PLACE",
        "phase": "logic",
        "title": f"Task {task_id}",
        "complexity": "medium",
        "planned_by": "model",
        "status": status,
        "created_at": T0,
        "updated_at": T0,
    }
    return PlanTask(**(defaults | values))


TASKS = [
    _task("TASK::ORDERS::A", "completed", phase="setup"),
    _task("TASK::ORDERS::B", waits_for=("TASK::ORDERS::A",)),
    _task("TASK::ORDERS::C", waits_for=("TASK::ORDERS::B",)),
    _task("TASK::ORDERS::D", planned_by="rules", story_id="STORY::ORDERS::TRACK"),
    _task("TASK::PAY::E", area_id="AREA::PAY", story_id="STORY::PAY::CORE"),
    _task("TASK::ORDERS::OLD", "superseded"),
]
READY = ("TASK::ORDERS::B", "TASK::ORDERS::D", "TASK::PAY::E")


class FakeReader:
    def __init__(self, *, has_plan: bool) -> None:
        self.has_plan = has_plan
        self.projects: list[str] = []

    async def plan(self, project_id: str) -> PlanInfo | None:
        self.projects.append(project_id)
        return PlanInfo(plan_id="plan-1", created_at=T0, updated_at=T0) if self.has_plan else None

    async def tasks(
        self, _project_id: str, *, status: str | None = None, area_id: str | None = None, story_id: str | None = None
    ) -> list[PlanTask]:
        return [
            task
            for task in TASKS
            if (status is None or task.status == status)
            and (area_id is None or task.area_id == area_id)
            and (story_id is None or task.story_id == story_id)
        ]

    async def areas(self, _project_id: str) -> list[PlanArea]:
        return [
            PlanArea(
                area_id=area_id,
                kind="module",
                title=title,
                planned_by="model",
                status="active",
                created_at=T0,
                updated_at=T0,
            )
            for area_id, title in (("AREA::ORDERS", "Orders"), ("AREA::PAY", "Payments"))
        ]

    async def stories(self, _project_id: str) -> list[PlanStory]:
        return [
            PlanStory(
                story_id=story_id,
                area_id=area_id,
                kind="feature",
                title=title,
                status="active",
                progress="pending",
                created_at=T0,
                updated_at=T0,
            )
            for story_id, area_id, title in (
                ("STORY::ORDERS::PLACE", "AREA::ORDERS", "Place an order"),
                ("STORY::PAY::CORE", "AREA::PAY", "Payments"),
            )
        ]


class FakeLifecycle:
    def __init__(self) -> None:
        self.briefs: list[tuple[str, str, int]] = []

    async def ready_tasks(self, _project_id: str) -> list[PlanTask]:
        return [task for task in TASKS if task.task_id in READY]

    async def task_brief(self, project_id: str, task_id: str, *, neighbours: int = 30) -> TaskBrief:
        self.briefs.append((project_id, task_id, neighbours))
        task = next((item for item in TASKS if item.task_id == task_id), None)
        if task is None:
            raise TaskStateError("unknown_task")
        if task.status == "superseded":
            raise TaskStateError("wrong_state", status=task.status)
        return TaskBrief(
            task=task,
            done_when=(DoneWhen(text="An order is stored.", source="spec"),),
            profile={"languages": ["python"]},
            spec_changed=True,
            missing=("REQ::GONE",),
        )


class FakePlanReads:
    def __init__(self, *, has_plan: bool = True) -> None:
        self.reader = FakeReader(has_plan=has_plan)
        self.lifecycle = FakeLifecycle()


def tool_app(monkeypatch: Any, valid_env: dict[str, str], plan: FakePlanReads) -> tuple[Starlette, FakeRepo]:
    """The MCP app with the real registry over ``FakeRepo`` and ``plan`` (no graph reader: the plan tools use none)."""

    repo = FakeRepo()
    repo.install(monkeypatch)
    runtime = fake_runtime(session_factory=repo, ontology=shipped_ontology(), plan=plan)
    app = build_app(mcp_env(valid_env), runtime=runtime, tools=lambda server: install(server, resource_url=MCP_URL))
    return app, repo


def _ids(result: Any) -> list[str]:
    return [item["task_id"] for item in structured(result)["items"]]


async def test_the_plan_tools_are_declared_read_only_and_project_scoped():
    by_name = {tool.name: tool for tool in TOOLS}
    for name in ("list_tasks", "get_task_brief"):
        assert by_name[name].project_scoped
        assert by_name[name].annotations.read_only_hint is True
        assert "data, not instructions" in by_name[name].description


async def test_list_tasks_lists_ready_tasks_by_default_with_their_area_story_and_origin(monkeypatch, valid_env):
    plan = FakePlanReads()
    app, repo = tool_app(monkeypatch, valid_env, plan)
    project = await owned_project(repo, "alice@example.com", "Shop")
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "list_tasks", project_id=str(project.id))

    body = structured(result)
    assert not result.is_error
    assert json.loads(text(result)) == body
    assert body["project_id"] == str(project.id) and body["total_count"] == 3 and body["next_cursor"] is None
    assert _ids(result) == list(READY)
    first = body["items"][0]
    assert first == {
        "task_id": "TASK::ORDERS::B",
        "title": "Task TASK::ORDERS::B",
        "phase": "logic",
        "status": "pending",
        "complexity": "medium",
        "area": {"area_id": "AREA::ORDERS", "title": "Orders"},
        "story": {"story_id": "STORY::ORDERS::PLACE", "title": "Place an order"},
        "ready": True,
        "waits_for": ["TASK::ORDERS::A"],
        "planned_by": "model",
    }
    rules = body["items"][1]
    assert rules["planned_by"] == "rules" and rules["story"] == {"story_id": "STORY::ORDERS::TRACK", "title": None}
    assert plan.reader.projects == [str(project.id)]


async def test_list_tasks_filters_by_readiness_status_area_and_story_and_never_lists_history(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanReads())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        everything = await call(session, "list_tasks", project_id=pid, ready_only=False)
        done = await call(session, "list_tasks", project_id=pid, ready_only=False, status="completed")
        pay = await call(session, "list_tasks", project_id=pid, area_id="AREA::PAY")
        place = await call(session, "list_tasks", project_id=pid, ready_only=False, story_id="STORY::ORDERS::PLACE")
        ready_done = await call(session, "list_tasks", project_id=pid, status="completed")
        history = await call(session, "list_tasks", project_id=pid, ready_only=False, status="superseded")

    assert _ids(everything) == [task.task_id for task in TASKS if task.status != "superseded"]
    assert [item["ready"] for item in structured(everything)["items"]] == [False, True, False, True, True]
    assert _ids(done) == ["TASK::ORDERS::A"]
    assert _ids(pay) == ["TASK::PAY::E"]
    assert _ids(place) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C"]
    assert _ids(ready_done) == [] and structured(ready_done)["total_count"] == 0
    assert history.is_error  # superseded is no status of the input schema: history is never listed


async def test_list_tasks_pages_by_task_id_and_refuses_a_bad_cursor(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanReads())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        first = await call(session, "list_tasks", project_id=pid, ready_only=False, limit=2)
        second = await call(
            session, "list_tasks", project_id=pid, ready_only=False, limit=2, cursor=structured(first)["next_cursor"]
        )
        last = await call(
            session, "list_tasks", project_id=pid, ready_only=False, limit=2, cursor=structured(second)["next_cursor"]
        )
        bad = await call(session, "list_tasks", project_id=pid, cursor="not a cursor")

    assert _ids(first) == ["TASK::ORDERS::A", "TASK::ORDERS::B"]
    assert structured(first)["next_cursor"] == encode_feature_cursor("TASK::ORDERS::B")
    assert _ids(second) == ["TASK::ORDERS::C", "TASK::ORDERS::D"]
    assert _ids(last) == ["TASK::PAY::E"] and structured(last)["next_cursor"] is None
    assert all(structured(page)["total_count"] == 5 for page in (first, second, last))
    assert code(bad) == "invalid_cursor"


async def test_get_task_brief_returns_the_library_brief(monkeypatch, valid_env):
    plan = FakePlanReads()
    app, repo = tool_app(monkeypatch, valid_env, plan)
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        result = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::B", neighbours=5)
        default = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::C")

    body = structured(result)
    assert not result.is_error and json.loads(text(result)) == body
    assert body["project_id"] == pid
    brief = body["brief"]
    assert brief["task"]["task_id"] == "TASK::ORDERS::B" and brief["task"]["waits_for"] == ["TASK::ORDERS::A"]
    assert brief["done_when"] == [{"text": "An order is stored.", "source": "spec"}]
    assert brief["profile"] == {"languages": ["python"]}
    assert brief["spec_changed"] is True and brief["missing"] == ["REQ::GONE"]
    assert not default.is_error
    assert plan.lifecycle.briefs == [(pid, "TASK::ORDERS::B", 5), (pid, "TASK::ORDERS::C", 30)]


async def test_get_task_brief_does_not_find_an_unknown_or_superseded_task(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanReads())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        unknown = await call(session, "get_task_brief", project_id=pid, task_id="TASK::NOPE")
        old = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::OLD")
        too_many = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::B", neighbours=101)

    assert code(unknown) == "task_not_found" and code(old) == "task_not_found"
    assert structured(unknown)["error"]["message"] == MESSAGES["task_not_found"]
    assert too_many.is_error  # the input schema bounds neighbours to 0..100


async def test_both_tools_answer_no_plan_before_the_first_plan(monkeypatch, valid_env):
    plan = FakePlanReads(has_plan=False)
    app, repo = tool_app(monkeypatch, valid_env, plan)
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        listed = await call(session, "list_tasks", project_id=pid)
        brief = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::B")

    assert code(listed) == "no_plan" and code(brief) == "no_plan"
    assert structured(listed)["error"]["message"] == MESSAGES["no_plan"]
    assert plan.lifecycle.briefs == []


async def test_the_plan_tools_check_membership_before_reading_the_plan(monkeypatch, valid_env):
    plan = FakePlanReads()
    app, repo = tool_app(monkeypatch, valid_env, plan)
    theirs = str((await owned_project(repo, "bob@example.com", "Beta")).id)
    async with sdk_session(app, ALICE) as session:
        listed = await call(session, "list_tasks", project_id=theirs)
        brief = await call(session, "get_task_brief", project_id=theirs, task_id="TASK::ORDERS::B")
        malformed = await call(session, "list_tasks", project_id="not-a-uuid")

    assert code(listed) == "project_not_found" and code(brief) == "project_not_found"
    assert code(malformed) == "project_not_found"
    assert plan.reader.projects == [] and plan.lifecycle.briefs == []  # refused before any plan read
