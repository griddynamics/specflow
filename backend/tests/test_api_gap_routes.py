"""Tier 1: the gap read routes (``api.routers.gaps``): the list with every filter and the latest run's status.

The runner is a fake on ``Runtime.gaps`` and its ``GapService`` a fake too; handlers never touch the graph store.
The writes are service-layer calls behind the Slack bot and the agent routes, not web routes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from api.gap.state import GapRunState
from api.plan.state import NoGraphBuildError
from db.sources import GraphCorpusRow
from graph.gap import (
    AiAnswerList,
    AnswerInput,
    AnswerInvalidError,
    AnswerResult,
    ApplyResult,
    EditPreview,
    GapNotFoundError,
    GapPage,
    GapRunResult,
    GapView,
    NewGap,
    NoiseReport,
    NoiseRuleRow,
    NoiseSection,
    RaisedGap,
    RevertResult,
)
from logger_harness import events
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
RULE_ID = "NR::0123456789AB"
RULE = NoiseRuleRow(
    rule_id=RULE_ID, scope="section", value="Status", reason="tracking", created_by="someone", created_at=NOW
)


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.refuse: AnswerInvalidError | None = None
        self.fail: dict[str, Exception] = {}  # a write call's name -> the error it raises

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

    # Writes ---------------------------------------------------------------------------------------------------------

    created = True
    immediate = False
    removed = True

    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        error = self.fail.get(name)
        if error is not None:
            raise error

    async def raise_gap(self, project_id: str, new: NewGap) -> RaisedGap:
        self._call("raise_gap", project_id, new)
        return RaisedGap(gap_id="GAP::NEW", created=self.created)

    async def preview_edits(self, project_id: str, gap_id: str, **kwargs: Any) -> EditPreview:
        self._call("preview_edits", project_id, gap_id, **kwargs)
        return EditPreview(gap_id=gap_id, placement="document", edits=(), sections=())

    async def answer_gap(self, project_id: str, answer: AnswerInput) -> AnswerResult:
        self._call("answer_gap", project_id, answer)
        return AnswerResult(
            gap_id=answer.gap_id,
            clr_id="CLR::1",
            placement="document",
            apply_at=NOW if self.immediate else None,
            immediate=self.immediate,
        )

    async def dismiss_gap(self, project_id: str, gap_id: str, **kwargs: Any) -> None:
        self._call("dismiss_gap", project_id, gap_id, **kwargs)

    async def apply_now(self, project_id: str, **kwargs: Any) -> ApplyResult:
        self._call("apply_now", project_id, **kwargs)
        return ApplyResult(gap_ids=("GAP::A",), clr_ids=("CLR::1",), build="started")

    async def noise_rules(self, project_id: str) -> tuple[NoiseRuleRow, ...]:
        self._call("noise_rules", project_id)
        return (RULE,)

    async def add_noise_rule(self, project_id: str, *args: Any) -> str:
        self._call("add_noise_rule", project_id, *args)
        return RULE_ID

    async def remove_noise_rule(self, project_id: str, *args: Any) -> bool:
        self._call("remove_noise_rule", project_id, *args)
        return self.removed

    async def assign_gap(self, project_id: str, gap_id: str, user_id: str | None, **kwargs: Any) -> GapView:
        self._call("assign_gap", project_id, gap_id, user_id, **kwargs)
        return VIEW.model_copy(update={"assigned_to": user_id})

    async def ai_answers(self, project_id: str, **kwargs: Any) -> AiAnswerList:
        self._call("ai_answers", project_id, **kwargs)
        return AiAnswerList()

    async def revert_clarification(self, project_id: str, clr_id: str, **kwargs: Any) -> RevertResult:
        self._call("revert_clarification", project_id, clr_id, **kwargs)
        return RevertResult(clr_id=clr_id, gap_id="GAP::FOLLOW", follow_up=True, build="queued")


class FakeGapRunner:
    def __init__(self) -> None:
        self.gap_service = FakeService()
        self.starts: list[str] = []
        self.built: set[str] = set()
        self.sites: list[tuple[str, str]] = []

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
            step="finish",
            steps_done=7,
            steps_total=7,
            started_at=NOW,
            finished_at=NOW,
            counts={"gaps_new": 2},
            result=RESULT,
        )

    async def noise(self, _project_id: str, run_id: str) -> NoiseReport | None:
        return NOISE if run_id == "run-1" else None

    async def service(self) -> FakeService:
        return self.gap_service

    async def start_site(self, project_id: str, gap_id: str) -> None:
        self.sites.append((project_id, gap_id))


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


def user_id(harness, email: str = "alice@example.com") -> str:
    return str(harness.repo.users[f"user:{email}"].id)


READ_ROUTES = (
    ("GET", "/gaps"),
    ("GET", "/gap/runs/latest"),
)


async def test_the_list_passes_every_filter_and_answers_the_page(harness):
    runner, pid = await gap_project(harness)
    query = (
        "?status=open&status=answered&kind=missing&severity=blocker&origin=analysis&route=ask"
        "&about=REQ-1&about=REQ-2&story_id=STORY::A&document=doc:1&assigned_to=none&cursor=c1"
        "&limit=20"
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
        "assigned_to": "none",
        "cursor": "c1",
        "limit": 20,
    }


async def test_the_assignee_filter_takes_me_as_the_caller_and_a_user_id_as_written(harness):
    runner, pid = await gap_project(harness)
    other = "AAAAAAAA-2222-4333-8444-555555555555"

    mine = await harness.client.get(url(pid, "/gaps?assigned_to=me"), headers=bearer())
    theirs = await harness.client.get(url(pid, f"/gaps?assigned_to={other}"), headers=bearer())

    assert mine.status_code == theirs.status_code == 200
    [(_, _, mine_filters), (_, _, their_filters)] = runner.gap_service.calls
    assert mine_filters["assigned_to"] == user_id(harness)
    assert their_filters["assigned_to"] == other.lower()


async def test_the_document_filter_takes_a_documents_id_as_its_corpus_lineage(harness, monkeypatch):
    runner, pid = await gap_project(harness)
    upload, drive = "11111111-2222-4333-8444-555555555555", "11111111-2222-4333-8444-666666666666"
    connection = uuid.UUID(int=9)

    async def corpus_rows(_session, project_id, document_id):
        assert project_id == pid
        if str(document_id) != drive:
            return []  # not in the corpus yet: its lineage is still doc:<id>
        values = {
            "document_id": document_id,
            "origin": "connection",
            "content_hash": "a" * 64,
            "original_filename": "spec.md",
            "created_at": datetime.now(UTC),
            "source_id": connection,
            "holder_name": "Drive",
            "drive_connection_id": connection,
            "drive_external_id": "file-1",
            "drive_source_name": "Drive",
            "markdown": "# Spec",
            "parser": None,
        }
        return [GraphCorpusRow(**values)]

    monkeypatch.setattr("api.routers.gaps.sources_repo.graph_corpus_document", corpus_rows)

    for document in (upload, drive):
        response = await harness.client.get(url(pid, f"/gaps?document={document}"), headers=bearer())
        assert response.status_code == 200
    [(_, _, uploaded), (_, _, synced)] = runner.gap_service.calls
    assert uploaded["document"] == f"doc:{upload}"  # the graph's document id of an uploaded document
    assert synced["document"] == f"drive:{connection}:file-1"  # a Drive document's lineage


async def test_the_list_defaults_to_no_filter(harness):
    runner, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, "/gaps"), headers=bearer())

    assert response.status_code == 200
    [(_, _, filters)] = runner.gap_service.calls
    assert filters["limit"] == 50
    assert all(value is None for name, value in filters.items() if name != "limit")


@pytest.mark.parametrize(
    "query", ["?status=closed", "?severity=huge", "?limit=0", "?limit=201", "?story_id=", "?assigned_to=bob"]
)
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


async def test_the_latest_run_answers_its_status_and_result(harness):
    _, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, "/gap/runs/latest"), headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["run_id"], body["steps_done"], body["counts"]) == (
        "completed",
        "run-1",
        7,
        {"gaps_new": 2},
    )
    assert body["result"]["counts"] == {"gaps_new": 2} and body["finished_at"] == "2026-11-05T09:00:00Z"


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
