"""Tier 1: ``/api/v1/projects/{pid}/plan`` (start, state), ``/plan/tasks`` and ``/plan/preview``.

The runner is a fake on ``Runtime.plans``; handlers never touch the graph store, so the harness's
``FakeGraphStore`` must see no query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.plan.state import NoGraphBuildError, PlanState
from api.schemas import PlanContent, PlanFeatureOut, PlanPreview, PlanTaskOut
from logger_harness import events, fields
from specflow_api_support import bearer

STARTED = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


class FakeRunner:
    """Records starts; ``state`` is what ``status`` answers when nothing runs."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, bool]] = []
        self.active: set[str] = set()
        self.state = PlanState("idle")
        self.built: set[str] = set()
        self.plan: PlanContent | None = None
        self.preview_value = PlanPreview()

    def running(self, project_id: str) -> bool:
        return project_id in self.active

    async def start(self, project_id: str, *, replan: bool = False) -> PlanState:
        if project_id not in self.built:
            raise NoGraphBuildError(project_id)
        if project_id not in self.active:
            self.starts.append((project_id, replan))
            self.active.add(project_id)
        return await self.status(project_id)

    async def status(self, project_id: str) -> PlanState:
        if project_id in self.active:
            return PlanState("running", step="snapshot", steps_total=10, started_at=STARTED)
        return self.state

    async def content(self, _project_id: str) -> PlanContent | None:
        return self.plan

    async def preview(self, _project_id: str) -> PlanPreview:
        return self.preview_value


class Builds:
    def __init__(self, *, running: bool) -> None:
        self._running = running

    def running(self, _project_id: str) -> bool:
        return self._running


async def project(harness, email: str = "alice@example.com") -> uuid.UUID:
    response = await harness.client.post("/api/v1/projects", json={"name": "Shop"}, headers=bearer(email))
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def url(pid: uuid.UUID, tail: str = "") -> str:
    return f"/api/v1/projects/{pid}/plan{tail}"


async def built_project(harness) -> tuple[FakeRunner, uuid.UUID]:
    runner = harness.runtime.plans = FakeRunner()
    pid = await project(harness)
    runner.built.add(str(pid))
    return runner, pid


async def test_post_starts_one_run_and_answers_202_at_once(harness, caplog):
    runner, pid = await built_project(harness)

    with caplog.at_level("INFO"):
        first = await harness.client.post(url(pid), headers=bearer())
        second = await harness.client.post(url(pid), headers=bearer())

    assert first.status_code == second.status_code == 202
    body = first.json()
    assert (body["status"], body["step"], body["steps_total"]) == ("running", "snapshot", 10)
    assert body["started_at"] == "2026-11-05T09:00:00Z"
    assert first.json() == second.json()
    assert runner.starts == [(str(pid), False)]
    [requested, again] = events(caplog, "api.plan.requested")
    assert fields(requested)["already_running"] is False and fields(again)["already_running"] is True
    assert harness.graph_store.queries == 0


async def test_post_passes_replan(harness):
    runner, pid = await built_project(harness)

    response = await harness.client.post(url(pid), json={"replan": True}, headers=bearer())

    assert response.status_code == 202
    assert runner.starts == [(str(pid), True)]


@pytest.mark.parametrize("payload", [{"replan": "yes"}, {"x": 1}])
async def test_post_with_a_bad_body_gets_422_and_starts_nothing(harness, payload):
    runner, pid = await built_project(harness)

    response = await harness.client.post(url(pid), json=payload, headers=bearer())

    assert response.status_code == 422
    assert runner.starts == []


async def test_a_project_without_a_finished_build_gets_409(harness):
    runner = harness.runtime.plans = FakeRunner()
    pid = await project(harness)

    response = await harness.client.post(url(pid), headers=bearer())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_graph_build"
    assert runner.starts == []


async def test_a_plan_cannot_start_while_the_graph_builds(harness):
    runner, pid = await built_project(harness)
    harness.runtime.graph_builds = Builds(running=True)

    response = await harness.client.post(url(pid), headers=bearer())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "graph_build_running"
    assert runner.starts == []


async def test_get_answers_the_state(harness):
    runner, pid = await built_project(harness)
    runner.state = PlanState(
        "succeeded", has_plan=True, run_id="run-1", mode="first", step="check", steps_done=10, steps_total=10
    )

    response = await harness.client.get(url(pid), headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["has_plan"], body["run_id"], body["steps_done"]) == ("succeeded", True, "run-1", 10)


async def test_tasks_answer_the_plan_or_404(harness):
    runner, pid = await built_project(harness)

    missing = await harness.client.get(url(pid, "/tasks"), headers=bearer())
    runner.plan = PlanContent(
        plan_id="plan-1",
        updated_at=STARTED,
        areas=[],
        stories=[],
        tasks=[
            PlanTaskOut(
                task_id="TASK::1",
                area_id="AREA::A",
                story_id="STORY::A",
                phase="setup",
                title="Set up",
                complexity="low",
                status="pending",
                revision=1,
            )
        ],
    )
    found = await harness.client.get(url(pid, "/tasks"), headers=bearer())

    assert missing.status_code == 404 and missing.json()["error"]["code"] == "no_plan"
    assert found.status_code == 200
    assert [task["task_id"] for task in found.json()["tasks"]] == ["TASK::1"]


async def test_preview_answers_the_features_and_the_estimate(harness):
    runner, pid = await built_project(harness)
    runner.preview_value = PlanPreview(
        build_id="build-1", mode="first", calls=4, input_tokens=900, features=[PlanFeatureOut(spec_id="F", name="Pay")]
    )

    response = await harness.client.get(url(pid, "/preview"), headers=bearer())

    assert response.status_code == 200
    assert response.json()["features"] == [{"spec_id": "F", "name": "Pay"}]
    assert response.json()["calls"] == 4


async def test_a_non_member_gets_404(harness):
    runner, pid = await built_project(harness)
    stranger = bearer("bob@example.com")

    for method, tail in (("POST", ""), ("GET", ""), ("GET", "/tasks"), ("GET", "/preview")):
        response = await harness.client.request(method, url(pid, tail), headers=stranger)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "project_not_found"
    assert runner.starts == []


async def test_without_graph_settings_the_plan_answers_503_and_logs_it(harness, caplog):
    assert harness.runtime.plans is None  # valid_env has no GRAPH_* model
    pid = await project(harness)

    with caplog.at_level("INFO"):
        for method, tail in (("POST", ""), ("GET", ""), ("GET", "/tasks"), ("GET", "/preview")):
            response = await harness.client.request(method, url(pid, tail), headers=bearer())
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "plan_unavailable"

    assert len(events(caplog, "api.plan.unavailable")) == 4
