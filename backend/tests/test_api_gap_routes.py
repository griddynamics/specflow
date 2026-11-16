"""Tier 1: the gap routes (``api.routers.gaps``): the read routes, the full run, the run status and the noise report.

The runner is a fake on ``Runtime.gaps`` and its ``GapService`` a fake too; handlers never touch the graph store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from api.gap.state import GapRunState
from api.plan.state import NoGraphBuildError
from graph.gap import AnswerInvalidError, GapNotFoundError, GapPage, GapRunResult, GapView, NoiseReport, NoiseSection
from logger_harness import events, fields
from specflow_api_support import bearer

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
VIEW = GapView(
    gap_id="GAP::A",
    kind="missing",
    severity="major",
    status="open",
    origin="analysis",
    route="ask",
    title="No provider",
    question="Who provides it?",
    detector="MS-PROVIDER",
    signature="s",
    created_by="gap",
    created_at=NOW,
    updated_at=NOW,
)
NOISE = NoiseReport(
    sections=(NoiseSection(lineage="doc:1", heading_path=("Status",), relevance="tracking", by="rules"),)
)
RESULT = GapRunResult(run_id="run-1", mode="update", build_id="build-1", status="completed", counts={"gaps_new": 2})


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.refuse: AnswerInvalidError | None = None

    async def list_gaps(self, project_id: str, **filters: Any) -> GapPage:
        self.calls.append(("list_gaps", (project_id,), filters))
        if self.refuse is not None:
            raise self.refuse
        return GapPage(gaps=(VIEW,), next="cursor-2")

    async def get_gap(self, project_id: str, gap_id: str) -> GapView:
        self.calls.append(("get_gap", (project_id, gap_id), {}))
        if gap_id != VIEW.gap_id:
            raise GapNotFoundError(gap_id=gap_id)
        return VIEW


class FakeGapRunner:
    def __init__(self) -> None:
        self.gap_service = FakeService()
        self.starts: list[str] = []
        self.built: set[str] = set()

    async def start_full(self, project_id: str) -> GapRunState:
        if project_id not in self.built:
            raise NoGraphBuildError(project_id)
        self.starts.append(project_id)
        return GapRunState("queued", queued=1)

    async def status(self, _project_id: str) -> GapRunState:
        return GapRunState(
            "completed",
            run_id="run-1",
            mode="update",
            build_id="build-1",
            step="record",
            steps_done=9,
            steps_total=9,
            started_at=NOW,
            finished_at=NOW,
            counts={"gaps_new": 2},
            result=RESULT,
        )

    async def noise(self, _project_id: str, run_id: str) -> NoiseReport | None:
        return NOISE if run_id == "run-1" else None

    async def service(self) -> FakeService:
        return self.gap_service


async def project(harness, email: str = "alice@example.com") -> uuid.UUID:
    response = await harness.client.post("/api/v1/projects", json={"name": "Shop"}, headers=bearer(email))
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def url(pid: uuid.UUID, tail: str) -> str:
    return f"/api/v1/projects/{pid}{tail}"


async def gap_project(harness) -> tuple[FakeGapRunner, uuid.UUID]:
    runner = harness.runtime.gaps = FakeGapRunner()
    pid = await project(harness)
    runner.built.add(str(pid))
    return runner, pid


READ_ROUTES = (
    ("GET", "/gaps"),
    ("GET", "/gaps/GAP::A"),
    ("POST", "/gap/runs"),
    ("GET", "/gap/runs/latest"),
    ("GET", "/gap/runs/run-1/noise"),
)


async def test_the_list_passes_every_filter_and_answers_the_page(harness):
    runner, pid = await gap_project(harness)
    query = (
        "?status=open&status=answered&kind=missing&severity=blocker&origin=analysis&route=ask"
        "&about=REQ-1&about=REQ-2&story_id=STORY::A&document=doc:1&held_back=false&cursor=c1&limit=20"
    )

    response = await harness.client.get(url(pid, f"/gaps{query}"), headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert [gap["gap_id"] for gap in body["gaps"]] == ["GAP::A"] and body["next"] == "cursor-2"
    [(name, args, filters)] = runner.gap_service.calls
    assert (name, args) == ("list_gaps", (str(pid),))
    assert filters == {
        "status": ["open", "answered"],
        "kind": ["missing"],
        "severity": ["blocker"],
        "origin": ["analysis"],
        "route": ["ask"],
        "about": ["REQ-1", "REQ-2"],
        "story_id": "STORY::A",
        "document": "doc:1",
        "held_back": False,
        "cursor": "c1",
        "limit": 20,
    }


async def test_the_document_filter_takes_an_uploaded_documents_id_as_its_lineage(harness):
    runner, pid = await gap_project(harness)
    document = "11111111-2222-4333-8444-555555555555"

    response = await harness.client.get(url(pid, f"/gaps?document={document}"), headers=bearer())

    assert response.status_code == 200
    [(_, _, filters)] = runner.gap_service.calls
    assert filters["document"] == f"doc:{document}"  # the graph's document id of an uploaded document


async def test_the_list_defaults_to_no_filter(harness):
    runner, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, "/gaps"), headers=bearer())

    assert response.status_code == 200
    [(_, _, filters)] = runner.gap_service.calls
    assert filters["limit"] == 50
    assert all(value is None for name, value in filters.items() if name != "limit")


@pytest.mark.parametrize("query", ["?status=closed", "?severity=huge", "?limit=0", "?limit=201", "?story_id="])
async def test_a_bad_filter_gets_422_and_reads_nothing(harness, query):
    runner, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, f"/gaps{query}"), headers=bearer())

    assert response.status_code == 422
    assert runner.gap_service.calls == []


@pytest.mark.parametrize(("field", "code"), [("cursor", "invalid_cursor"), ("limit", "invalid_arguments")])
async def test_a_refused_cursor_or_argument_gets_400(harness, field, code):
    runner, pid = await gap_project(harness)
    runner.gap_service.refuse = AnswerInvalidError(field=field, reason="invalid")

    response = await harness.client.get(url(pid, "/gaps?cursor=bad"), headers=bearer())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


async def test_one_gap_and_an_unknown_one(harness):
    _, pid = await gap_project(harness)

    found = await harness.client.get(url(pid, "/gaps/GAP::A"), headers=bearer())
    missing = await harness.client.get(url(pid, "/gaps/GAP::NONE"), headers=bearer())

    assert found.status_code == 200 and found.json()["question"] == "Who provides it?"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "gap_not_found"


async def test_post_queues_a_full_run_and_answers_202(harness, caplog):
    runner, pid = await gap_project(harness)

    with caplog.at_level("INFO"):
        response = await harness.client.post(url(pid, "/gap/runs"), headers=bearer())

    assert response.status_code == 202
    assert (response.json()["status"], response.json()["queued"]) == ("queued", 1)
    assert runner.starts == [str(pid)]
    [requested] = events(caplog, "api.gap.requested")
    assert {key: fields(requested)[key] for key in ("project_id", "mode", "status")} == {
        "project_id": str(pid),
        "mode": "full",
        "status": "queued",
    }


async def test_a_full_run_without_a_finished_build_gets_409(harness):
    harness.runtime.gaps = FakeGapRunner()
    pid = await project(harness)

    response = await harness.client.post(url(pid, "/gap/runs"), headers=bearer())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_graph_build"


async def test_the_latest_run_answers_its_status_and_result(harness):
    _, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, "/gap/runs/latest"), headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["run_id"], body["steps_done"], body["counts"]) == (
        "completed",
        "run-1",
        9,
        {"gaps_new": 2},
    )
    assert body["result"]["counts"] == {"gaps_new": 2} and body["finished_at"] == "2026-11-05T09:00:00Z"


async def test_the_noise_report_of_a_run_and_of_an_unknown_one(harness):
    _, pid = await gap_project(harness)

    found = await harness.client.get(url(pid, "/gap/runs/run-1/noise"), headers=bearer())
    missing = await harness.client.get(url(pid, "/gap/runs/run-9/noise"), headers=bearer())
    bad = await harness.client.get(url(pid, "/gap/runs/.hidden/noise"), headers=bearer())

    assert found.status_code == 200 and found.json()["sections"][0]["relevance"] == "tracking"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "gap_run_not_found"
    assert bad.status_code == 422


async def test_a_non_member_gets_404_on_every_route(harness):
    runner, pid = await gap_project(harness)
    stranger = bearer("bob@example.com")

    for method, tail in READ_ROUTES:
        response = await harness.client.request(method, url(pid, tail), headers=stranger)
        assert response.status_code == 404, tail
        assert response.json()["error"]["code"] == "project_not_found"
    assert runner.starts == [] and runner.gap_service.calls == []


async def test_without_a_gap_runner_every_route_answers_503(harness, caplog):
    assert harness.runtime.gaps is None  # valid_env has no GRAPH_* model
    pid = await project(harness)

    with caplog.at_level("INFO"):
        for method, tail in READ_ROUTES:
            response = await harness.client.request(method, url(pid, tail), headers=bearer())
            assert response.status_code == 503, tail
            assert response.json()["error"]["code"] == "gap_unavailable"
    assert len(events(caplog, "api.gap.unavailable")) == len(READ_ROUTES)
