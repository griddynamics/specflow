"""The plan tools over HTTP (docs/mcp-server/tools.md, the plan tools): the SDK client session, tokens from the
loopback OIDC IdP, the real tool registry over the REST ``FakeRepo`` and the plan library's ``PlanService``.

Boundaries faked: Postgres (``FakeRepo``), the plan store read (``shop_plan``, in memory) and the spec graph
(``ShopGraph``); the brief, whose graph reads have their own tests, is a canned one. The composition itself is the
service's (test_graph_plan_service.py); these tests hold the tools to their wire contract.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.applications import Starlette

from api.mcp.errors import MESSAGES
from api.mcp.registry import TOOLS, install
from api.mcp.tools.graph import encode_feature_cursor
from graph.plan import PlanReadError, PlanService, TaskBrief
from graph.plan.models import DoneWhen
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from graph_support import OfflineExecutor, shipped_ontology
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
from plan_reads_support import OPEN_TASK_IDS, READY, ShopGraph, shop_plan
from test_specflow_api_fakes import FakeRepo

ALICE = mcp_token("alice@example.com")


class FakePlanService(PlanService):
    """The real service over ``shop_plan`` and ``ShopGraph``, recording the projects it loads and the briefs asked."""

    def __init__(self, *, has_plan: bool = True) -> None:
        self.projects: list[str] = []
        self.briefs: list[tuple[str, str, int]] = []
        self.loaded = shop_plan(has_plan=has_plan)

        async def load(project_id: str) -> LoadedPlan:
            self.projects.append(project_id)
            return self.loaded

        ontology = plan_ontology()
        super().__init__(OfflineExecutor(), ontology, plan_rules(ontology), graph=ShopGraph(), load=load)

    async def task_brief(self, project_id: str, task_id: str, *, neighbours: int = 30) -> TaskBrief:
        await self._plan(project_id)
        self.briefs.append((project_id, task_id, neighbours))
        task = self.loaded.tasks.get(task_id)
        if task is None or task.status == "superseded":
            raise PlanReadError("task_not_found")
        return TaskBrief(
            task=self.loaded.plan_task(task),
            done_when=(DoneWhen(text="An order is stored.", source="spec"),),
            profile={"languages": ["python"]},
            spec_changed=True,
            missing=("REQ::GONE",),
        )


def tool_app(monkeypatch: Any, valid_env: dict[str, str], plan: FakePlanService) -> tuple[Starlette, FakeRepo]:
    """The MCP app with the real registry over ``FakeRepo`` and ``plan``."""

    repo = FakeRepo()
    repo.install(monkeypatch)
    runtime = fake_runtime(session_factory=repo, ontology=shipped_ontology(), plan=plan)
    app = build_app(mcp_env(valid_env), runtime=runtime, tools=lambda server: install(server, resource_url=MCP_URL))
    return app, repo


def _ids(result: Any) -> list[str]:
    return [item["task_id"] for item in structured(result)["items"]]


async def test_the_plan_tools_are_declared_read_only_and_project_scoped():
    by_name = {tool.name: tool for tool in TOOLS}
    for name in ("list_tasks", "list_feature_tasks", "get_task_path", "get_task_brief"):
        assert by_name[name].project_scoped
        assert by_name[name].annotations.read_only_hint is True
        assert "data, not instructions" in by_name[name].description


async def test_list_tasks_lists_ready_tasks_by_default_with_their_area_story_and_origin(monkeypatch, valid_env):
    plan = FakePlanService()
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
    assert rules["planned_by"] == "rules" and rules["story"] == {
        "story_id": "STORY::ORDERS::TRACK",
        "title": "Track an order",
    }
    assert plan.projects == [str(project.id)]


async def test_list_tasks_filters_by_readiness_status_area_and_story_and_never_lists_history(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        everything = await call(session, "list_tasks", project_id=pid, ready_only=False)
        done = await call(session, "list_tasks", project_id=pid, ready_only=False, status="completed")
        pay = await call(session, "list_tasks", project_id=pid, area_id="AREA::PAY")
        place = await call(session, "list_tasks", project_id=pid, ready_only=False, story_id="STORY::ORDERS::PLACE")
        ready_done = await call(session, "list_tasks", project_id=pid, status="completed")
        history = await call(session, "list_tasks", project_id=pid, ready_only=False, status="superseded")

    assert _ids(everything) == list(OPEN_TASK_IDS)
    assert [item["ready"] for item in structured(everything)["items"]] == [False, True, False, True, True]
    assert _ids(done) == ["TASK::ORDERS::A"]
    assert _ids(pay) == ["TASK::PAY::E"]
    assert _ids(place) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C"]
    assert _ids(ready_done) == [] and structured(ready_done)["total_count"] == 0
    assert history.is_error  # superseded is no status of the input schema: history is never listed


async def test_list_tasks_pages_by_task_id_and_refuses_a_bad_cursor(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService())
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
    plan = FakePlanService()
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
    assert plan.briefs == [(pid, "TASK::ORDERS::B", 5), (pid, "TASK::ORDERS::C", 30)]


async def test_get_task_brief_does_not_find_an_unknown_or_superseded_task(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        unknown = await call(session, "get_task_brief", project_id=pid, task_id="TASK::NOPE")
        old = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::OLD")
        too_many = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::B", neighbours=101)

    assert code(unknown) == "task_not_found" and code(old) == "task_not_found"
    assert structured(unknown)["error"]["message"] == MESSAGES["task_not_found"]
    assert too_many.is_error  # the input schema bounds neighbours to 0..100


async def test_both_tools_answer_no_plan_before_the_first_plan(monkeypatch, valid_env):
    plan = FakePlanService(has_plan=False)
    app, repo = tool_app(monkeypatch, valid_env, plan)
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        listed = await call(session, "list_tasks", project_id=pid)
        brief = await call(session, "get_task_brief", project_id=pid, task_id="TASK::ORDERS::B")

    assert code(listed) == "no_plan" and code(brief) == "no_plan"
    assert structured(listed)["error"]["message"] == MESSAGES["no_plan"]
    assert plan.briefs == []


async def test_the_plan_tools_check_membership_before_reading_the_plan(monkeypatch, valid_env):
    plan = FakePlanService()
    app, repo = tool_app(monkeypatch, valid_env, plan)
    theirs = str((await owned_project(repo, "bob@example.com", "Beta")).id)
    async with sdk_session(app, ALICE) as session:
        listed = await call(session, "list_tasks", project_id=theirs)
        brief = await call(session, "get_task_brief", project_id=theirs, task_id="TASK::ORDERS::B")
        malformed = await call(session, "list_tasks", project_id="not-a-uuid")

    assert code(listed) == "project_not_found" and code(brief) == "project_not_found"
    assert code(malformed) == "project_not_found"
    assert plan.projects == [] and plan.briefs == []  # refused before any plan read


async def test_list_feature_tasks_lists_the_feature_story_and_the_tasks_implementing_its_scope(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        listed = await call(session, "list_feature_tasks", project_id=pid, feature_id="FEAT::PLACE")
        paged = await call(session, "list_feature_tasks", project_id=pid, feature_id="FEAT::PLACE", limit=2)
        rest = await call(
            session,
            "list_feature_tasks",
            project_id=pid,
            feature_id="FEAT::PLACE",
            cursor=structured(paged)["next_cursor"],
        )
        not_feature = await call(session, "list_feature_tasks", project_id=pid, feature_id="REQ::PAY_ON_PLACE")
        unknown = await call(session, "list_feature_tasks", project_id=pid, feature_id="FEAT::NONE")

    body = structured(listed)
    assert body["feature"] == {"spec_id": "FEAT::PLACE", "name": "Place an order"}
    # the story's tasks, ready or not (the superseded one never), and the payment task implementing a node under it
    assert _ids(listed) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C", "TASK::PAY::E"]
    assert body["total_count"] == 4 and body["next_cursor"] is None
    assert [item["ready"] for item in body["items"]] == [False, True, False, True]
    assert _ids(paged) + _ids(rest) == _ids(listed)
    assert code(not_feature) == "feature_not_found" and code(unknown) == "feature_not_found"


async def test_get_task_path_lists_every_prerequisite_in_build_order_then_the_task(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService())
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        path = await call(session, "get_task_path", project_id=pid, task_id="TASK::ORDERS::C")
        first = await call(session, "get_task_path", project_id=pid, task_id="TASK::ORDERS::C", limit=1)
        rest = await call(
            session, "get_task_path", project_id=pid, task_id="TASK::ORDERS::C", cursor=structured(first)["next_cursor"]
        )
        alone = await call(session, "get_task_path", project_id=pid, task_id="TASK::PAY::E")
        old = await call(session, "get_task_path", project_id=pid, task_id="TASK::ORDERS::OLD")
        missing = await call(session, "get_task_path", project_id=pid, task_id="TASK::NONE")
        foreign_cursor = await call(
            session,
            "get_task_path",
            project_id=pid,
            task_id="TASK::ORDERS::C",
            cursor=encode_feature_cursor("TASK::PAY::E"),
        )

    body = structured(path)
    assert _ids(path) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C"]
    assert (body["task_id"], body["total_count"], body["completed"], body["remaining"]) == ("TASK::ORDERS::C", 3, 1, 1)
    assert [item["status"] for item in body["items"]] == ["completed", "pending", "pending"]
    assert _ids(first) + _ids(rest) == _ids(path) and structured(rest)["next_cursor"] is None
    assert _ids(alone) == ["TASK::PAY::E"] and structured(alone)["remaining"] == 0
    assert code(old) == "task_not_found" and code(missing) == "task_not_found"
    assert code(foreign_cursor) == "invalid_cursor"


async def test_the_feature_and_path_tools_answer_no_plan_before_the_first_plan(monkeypatch, valid_env):
    app, repo = tool_app(monkeypatch, valid_env, FakePlanService(has_plan=False))
    pid = str((await owned_project(repo, "alice@example.com", "Shop")).id)
    async with sdk_session(app, ALICE) as session:
        feature = await call(session, "list_feature_tasks", project_id=pid, feature_id="FEAT::PLACE")
        path = await call(session, "get_task_path", project_id=pid, task_id="TASK::ORDERS::C")

    assert code(feature) == "no_plan" and code(path) == "no_plan"
