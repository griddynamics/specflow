"""Tier 1: the gap routes (``api.routers.gaps``): the read routes, the full run, the run status, the noise report and
the writes (raise, edits preview, answer, dismiss, apply now, noise rules).

The runner is a fake on ``Runtime.gaps`` and its ``GapService`` a fake too; handlers never touch the graph store.
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
    AnswerInput,
    AnswerInvalidError,
    AnswerResult,
    ApplyResult,
    EditPreview,
    GapNotFoundError,
    GapPage,
    GapRunResult,
    GapStateError,
    GapView,
    NewGap,
    NoiseReport,
    NoiseRuleRow,
    NoiseSection,
    RaisedGap,
    StaleGapError,
)
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
APPLY_AT = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
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
            gap_id=answer.gap_id, clr_id="CLR::1", placement="document", apply_at=APPLY_AT, immediate=self.immediate
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


class FakeGapRunner:
    def __init__(self) -> None:
        self.gap_service = FakeService()
        self.starts: list[str] = []
        self.built: set[str] = set()
        self.sites: list[tuple[str, str]] = []
        self.schedules: list[tuple[str, datetime]] = []
        self.applies: list[str] = []

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

    async def start_site(self, project_id: str, gap_id: str) -> None:
        self.sites.append((project_id, gap_id))

    def schedule_apply(self, project_id: str, at: datetime) -> None:
        self.schedules.append((project_id, at))

    def applied(self, project_id: str) -> None:
        self.applies.append(project_id)


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


# Writes ----------------------------------------------------------------------------------------------------------

ANSWER = {"option_rank": 1, "updated_at": "2026-11-05T09:00:00Z"}
WRITE_ROUTES = (
    ("POST", "/gaps", {"question": "Who provides the payment gateway?", "about": ["REQ-1"]}, 201, "raise_gap"),
    ("POST", "/gaps/GAP::A/edits_preview", {"option_rank": 1}, 200, "preview_edits"),
    ("POST", "/gaps/GAP::A/answer", ANSWER, 200, "answer_gap"),
    ("POST", "/gaps/GAP::A/dismiss", {"reason": "not a gap"}, 204, "dismiss_gap"),
    ("POST", "/gaps/apply", None, 200, "apply_now"),
    ("GET", "/gaps/noise_rules", None, 200, "noise_rules"),
    ("POST", "/gaps/noise_rules", {"scope": "section", "value": "Status", "reason": "tracking"}, 201, "add_noise_rule"),
    ("DELETE", f"/gaps/noise_rules/{RULE_ID}", None, 204, "remove_noise_rule"),
)
WRITE_IDS = [call for *_, call in WRITE_ROUTES]


def user_id(harness, email: str = "alice@example.com") -> str:
    return str(harness.repo.users[f"user:{email}"].id)


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=WRITE_IDS)
async def test_a_member_calls_every_write_route(harness, route):
    method, tail, body, status, call = route
    runner, pid = await gap_project(harness)

    response = await harness.client.request(method, url(pid, tail), json=body, headers=bearer())

    assert response.status_code == status, response.text
    [(name, args, _)] = runner.gap_service.calls
    assert (name, args[0]) == (call, str(pid))


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=WRITE_IDS)
async def test_a_non_member_gets_404_on_every_write_route(harness, route):
    method, tail, body, *_ = route
    runner, pid = await gap_project(harness)

    response = await harness.client.request(method, url(pid, tail), json=body, headers=bearer("bob@example.com"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"
    assert runner.gap_service.calls == []
    assert (runner.sites, runner.schedules, runner.applies) == ([], [], [])


@pytest.mark.parametrize("route", WRITE_ROUTES, ids=WRITE_IDS)
async def test_without_a_gap_runner_every_write_route_answers_503(harness, route):
    method, tail, body, *_ = route
    assert harness.runtime.gaps is None
    pid = await project(harness)

    response = await harness.client.request(method, url(pid, tail), json=body, headers=bearer())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gap_unavailable"


async def test_a_member_who_is_not_the_owner_answers_as_themselves(harness):
    runner, pid = await gap_project(harness)
    assert (await harness.client.get("/api/v1/projects", headers=bearer("bob@example.com"))).status_code == 200
    bob = harness.repo.users["user:bob@example.com"].id
    harness.repo.members.add((pid, bob))  # membership is the only access rule (FR-GANS-13)

    answered = await harness.client.post(
        url(pid, "/gaps/GAP::A/answer"), json=ANSWER, headers=bearer("bob@example.com")
    )
    dismissed = await harness.client.post(
        url(pid, "/gaps/GAP::B/dismiss"), json={"reason": "duplicate"}, headers=bearer("bob@example.com")
    )

    assert answered.status_code == 200 and dismissed.status_code == 204
    [(_, (_, answer), _), (_, dismiss_args, dismiss_kwargs)] = runner.gap_service.calls
    assert answer.answered_by == str(bob) != user_id(harness)
    assert (answer.gap_id, answer.option_rank, answer.updated_at) == ("GAP::A", 1, NOW)
    assert dismiss_args == (str(pid), "GAP::B")
    assert dismiss_kwargs == {"reason": "duplicate", "by": str(bob)}


async def test_an_answer_schedules_the_apply_at_its_time(harness):
    runner, pid = await gap_project(harness)

    response = await harness.client.post(url(pid, "/gaps/GAP::A/answer"), json=ANSWER, headers=bearer())

    assert response.status_code == 200
    assert response.json()["clr_id"] == "CLR::1"
    [(_, (_, answer), _)] = runner.gap_service.calls
    assert answer.answered_by == user_id(harness)
    assert runner.schedules == [(str(pid), APPLY_AT)]


async def test_an_immediate_answer_schedules_no_apply(harness):
    runner, pid = await gap_project(harness)
    runner.gap_service.immediate = True

    response = await harness.client.post(url(pid, "/gaps/GAP::A/answer"), json=ANSWER, headers=bearer())

    assert response.status_code == 200 and response.json()["immediate"] is True
    assert runner.schedules == []


async def test_apply_now_applies_as_the_member_and_clears_the_due_apply(harness):
    runner, pid = await gap_project(harness)

    response = await harness.client.post(url(pid, "/gaps/apply"), headers=bearer())

    assert response.status_code == 200
    assert response.json()["gap_ids"] == ["GAP::A"]
    [(_, _, kwargs)] = runner.gap_service.calls
    assert kwargs == {"by": user_id(harness)}
    assert runner.applies == [str(pid)]


async def test_apply_now_with_nothing_pending_gets_409(harness):
    runner, pid = await gap_project(harness)
    runner.gap_service.fail["apply_now"] = GapStateError(status="none", allowed=("answered",))

    response = await harness.client.post(url(pid, "/gaps/apply"), headers=bearer())

    assert response.status_code == 409 and response.json()["error"]["code"] == "gap_wrong_state"
    # The due apply is cleared before the apply runs (nothing was pending, so nothing is lost).
    assert runner.applies == [str(pid)]


@pytest.mark.parametrize(("created", "sites"), [(True, 1), (False, 0)])
async def test_raising_a_gap_queues_a_site_run_only_when_it_is_new(harness, created, sites):
    runner, pid = await gap_project(harness)
    runner.gap_service.created = created
    body = {"question": "Who provides the payment gateway?", "about": ["REQ-1"], "severity": "blocker"}

    response = await harness.client.post(url(pid, "/gaps"), json=body, headers=bearer())

    assert response.status_code == 201
    assert response.json() == {"gap_id": "GAP::NEW", "created": created}
    [(_, (_, new), _)] = runner.gap_service.calls
    assert (new.origin, new.created_by, new.about, new.severity) == ("person", user_id(harness), ("REQ-1",), "blocker")
    assert runner.sites == [(str(pid), "GAP::NEW")] * sites


async def test_the_edits_preview_passes_the_answer(harness):
    runner, pid = await gap_project(harness)
    body = {"own_text": "Stripe provides it.", "placement": "amend"}

    response = await harness.client.post(url(pid, "/gaps/GAP::A/edits_preview"), json=body, headers=bearer())

    assert response.status_code == 200 and response.json()["gap_id"] == "GAP::A"
    [(_, args, kwargs)] = runner.gap_service.calls
    assert args == (str(pid), "GAP::A")
    assert kwargs == {"option_rank": None, "own_text": "Stripe provides it.", "placement": "amend"}


@pytest.mark.parametrize("path", ["/gaps/GAP::A/edits_preview", "/gaps/GAP::A/answer"])
@pytest.mark.parametrize("answer", [{}, {"option_rank": 1, "own_text": "Both."}], ids=["none", "both"])
async def test_an_answer_needs_exactly_one_of_option_and_text(harness, path, answer):
    runner, pid = await gap_project(harness)
    body = {**answer, "updated_at": ANSWER["updated_at"]} if path.endswith("answer") else answer

    response = await harness.client.post(url(pid, path), json=body, headers=bearer())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "answer_invalid"
    assert response.json()["error"]["details"] == {"field": "answer", "reason": "one_of_option_or_text"}
    assert not runner.gap_service.calls


@pytest.mark.parametrize(
    "route",
    [
        ("/gaps/GAP::A/edits_preview", {"option_rank": 1}, "preview_edits"),
        ("/gaps/GAP::A/answer", ANSWER, "answer_gap"),
        ("/gaps/GAP::A/dismiss", {"reason": "not a gap"}, "dismiss_gap"),
    ],
    ids=["preview", "answer", "dismiss"],
)
@pytest.mark.parametrize(
    "refusal",
    [
        (GapNotFoundError(gap_id="GAP::A"), 404, "gap_not_found", {}),
        (
            GapStateError(status="resolved", allowed=("open", "answered")),
            409,
            "gap_wrong_state",
            {"status": "resolved", "allowed": ["open", "answered"]},
        ),
        (
            AnswerInvalidError(field="own_text", reason="too_long"),
            400,
            "answer_invalid",
            {"field": "own_text", "reason": "too_long"},
        ),
    ],
    ids=["not-found", "wrong-state", "invalid"],
)
async def test_a_refused_write_maps_to_its_error(harness, route, refusal):
    tail, body, call = route
    error, status, code, details = refusal
    runner, pid = await gap_project(harness)
    runner.gap_service.fail[call] = error

    response = await harness.client.post(url(pid, tail), json=body, headers=bearer())

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert (response.json()["error"].get("details") or {}) == details
    assert runner.schedules == []


async def test_a_stale_answer_gets_409_gap_stale(harness):
    runner, pid = await gap_project(harness)
    runner.gap_service.fail["answer_gap"] = StaleGapError(gap_id="GAP::A")

    response = await harness.client.post(url(pid, "/gaps/GAP::A/answer"), json=ANSWER, headers=bearer())

    assert response.status_code == 409 and response.json()["error"]["code"] == "gap_stale"
    assert runner.schedules == []


async def test_a_refused_raise_or_noise_rule_gets_400_answer_invalid(harness):
    runner, pid = await gap_project(harness)
    runner.gap_service.fail["raise_gap"] = AnswerInvalidError(field="about", reason="unknown_id")
    runner.gap_service.fail["add_noise_rule"] = AnswerInvalidError(field="value", reason="invalid_pattern")
    rule = {"scope": "pattern", "value": "(", "reason": "status lines"}

    raised = await harness.client.post(
        url(pid, "/gaps"), json={"question": "Who provides the payment gateway?"}, headers=bearer()
    )
    added = await harness.client.post(url(pid, "/gaps/noise_rules"), json=rule, headers=bearer())

    assert raised.status_code == added.status_code == 400
    assert raised.json()["error"]["details"] == {"field": "about", "reason": "unknown_id"}
    assert added.json()["error"]["details"] == {"field": "value", "reason": "invalid_pattern"}
    assert runner.sites == []


async def test_noise_rules_are_listed_added_and_removed_by_the_member(harness):
    runner, pid = await gap_project(harness)
    rule = {"scope": "section", "value": "Status", "reason": "tracking"}

    listed = await harness.client.get(url(pid, "/gaps/noise_rules"), headers=bearer())
    added = await harness.client.post(url(pid, "/gaps/noise_rules"), json=rule, headers=bearer())
    removed = await harness.client.delete(url(pid, f"/gaps/noise_rules/{RULE_ID}"), headers=bearer())

    assert listed.status_code == 200 and [row["rule_id"] for row in listed.json()["rules"]] == [RULE_ID]
    assert added.status_code == 201 and added.json() == {"rule_id": RULE_ID}
    assert removed.status_code == 204
    me = user_id(harness)
    assert [(name, args) for name, args, _ in runner.gap_service.calls] == [
        ("noise_rules", (str(pid),)),
        ("add_noise_rule", (str(pid), "section", "Status", "tracking", me)),
        ("remove_noise_rule", (str(pid), RULE_ID, me)),
    ]


async def test_removing_an_unknown_noise_rule_gets_404(harness):
    runner, pid = await gap_project(harness)
    runner.gap_service.removed = False

    response = await harness.client.delete(url(pid, f"/gaps/noise_rules/{RULE_ID}"), headers=bearer())
    malformed = await harness.client.delete(url(pid, "/gaps/noise_rules/NR::nope"), headers=bearer())

    assert response.status_code == 404 and response.json()["error"]["code"] == "noise_rule_not_found"
    assert malformed.status_code == 422


async def test_the_noise_rules_route_is_not_read_as_a_gap_id(harness):
    runner, pid = await gap_project(harness)

    response = await harness.client.get(url(pid, "/gaps/noise_rules"), headers=bearer())

    assert response.status_code == 200 and "rules" in response.json()
    assert [name for name, _, _ in runner.gap_service.calls] == ["noise_rules"]
