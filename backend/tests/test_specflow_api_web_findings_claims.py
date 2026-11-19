"""The web routes of the findings list and the claims board (session 10; access-control.md AC12): list findings by node
and status, resolve and dismiss with a note (A20: both unblock), list the claims oldest first, force a story release.
Membership is the only rule, as on every web route; a refused call writes nothing. Called with the web token, not the
agent one, over the REAL ``GapService`` and ``PlanService`` of ``people_support``.

The ``/findings`` routes are aliases of the development gaps' routes (docs/graph/gap/api.md "Web routes"): a finding
is a development gap (its id the gap id, a migrated finding's ``FND::`` id an alias), and resolve and dismiss both
dismiss the open gap with the note as its reason.

Sequence of a resolve (the people's answer to a blocking question)::

    web -> route: POST /projects/{pid}/findings/{id}/resolve {note}
    route -> GapService: get_gap (id or alias), dismiss_gap(reason=note, by=user)
    GapService -> PlanService: release_gap (the tasks blocked by the gap or its alias go back to pending)
    route -> web: finding (status resolved), unblocked
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES
from graph.gap.gaps import gap_uuid
from people_support import API, API_NODE, STUCK, UI, PeopleRig, serve_people
from specflow_api_agent_support import (
    ALPHA,
    FOREIGN,
    UNKNOWN_PROJECT,
    agent_env,
    bearer_header,
    events,
    mcp_token,
    web_alice,
)

ALIAS = "FND::1"  # the first-slice id of Alice's blocking gap: the plan's STUCK task is blocked_by it
CONTRACT_TASK = "TASK::A::X_CONTRACT"
STUCK_TASK = "TASK::A::STUCK_BLOCKED"


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def blocking_id(people: PeopleRig) -> str:
    """Alice's blocking open question raised from STUCK."""

    return people.seeded["alice"]


def other_id(people: PeopleRig) -> str:
    """Ben's open ``missing`` gap: blocks nothing."""

    return people.seeded["ben"]


async def get(people: PeopleRig, path: str, *, token: str | None = None, **params: Any) -> httpx.Response:
    return await people.rig.client.get(path, params=params, headers=bearer_header(token or web_alice()))


async def post(
    people: PeopleRig, path: str, body: dict[str, Any] | None = None, *, token: str | None = None
) -> httpx.Response:
    return await people.rig.client.post(path, json=body or {}, headers=bearer_header(token or web_alice()))


def findings_path(project: str = ALPHA, *more: str) -> str:
    return "/".join([f"/api/v1/projects/{project}/findings", *more])


def claims_path(project: str = ALPHA, *more: str) -> str:
    return "/".join([f"/api/v1/projects/{project}/plan/claims", *more])


def state(people: PeopleRig) -> tuple[Any, ...]:
    return (
        {key: dict(row) for key, row in people.memory.stories.items()},
        {key: dict(row) for key, row in people.memory.tasks.items()},
        {key: dict(row) for key, row in people.gap.gaps().items()},
        {key: dict(rows) for key, rows in people.records.suppressions.items()},
    )


# --- the findings list ---------------------------------------------------------------------------------------------


async def test_the_open_findings_are_the_development_gaps_with_names_and_never_an_id_or_email(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await get(people, findings_path())
    assert response.status_code == 200
    body = response.json()
    assert [item["finding_id"] for item in body["findings"]] == [other_id(people), blocking_id(people)]  # gaps' order
    other, blocking = body["findings"]
    assert (blocking["kind"], blocking["blocking"], blocking["story_id"], blocking["about"]) == (
        "question",  # the gap kind open_question reads as the finding kind question
        True,
        STUCK,
        ["FEAT::API"],
    )
    assert (other["kind"], other["blocking"], other["text"]) == ("gap", False, "The API is slow | very.\nReally.")
    assert (blocking["story_title"], other["story_title"]) == (f"Title of {STUCK}", None)
    assert blocking["author"] == {"name": "Alice", "you": True, "agent_label": None}
    assert other["author"] == {"name": "Ben", "you": False, "agent_label": None}
    assert (body["next"], blocking["resolved_by"], blocking["status"]) == (None, None, "open")
    assert (blocking["refers_to"], blocking["resolution_note"]) == ([], None)  # refs stay in the gap's first event
    assert "@" not in response.text and str(people.alice) not in response.text


async def test_a_removed_members_finding_stays_listed_with_a_null_author_name(monkeypatch, env):
    """FR-PRJ-CLM-04: removing Ben keeps his gap; it is listed on, its author name now ``null`` (he is no member)."""

    async with serve_people(monkeypatch, env) as people:
        removed = await people.rig.client.delete(
            f"/api/v1/projects/{ALPHA}/members/{people.ben}", headers=bearer_header(web_alice())
        )
        listed = await get(people, findings_path())
    assert removed.status_code == 200
    by_id = {item["finding_id"]: item for item in listed.json()["findings"]}
    assert set(by_id) == {blocking_id(people), other_id(people)}
    assert by_id[other_id(people)]["author"] == {"name": None, "you": False, "agent_label": None}
    assert by_id[blocking_id(people)]["author"]["name"] == "Alice"
    assert "Ben" not in listed.text and "@" not in listed.text


async def test_the_list_filters_by_node_and_status_and_pages(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        other = await get(people, findings_path(), node="FEAT::API", status="resolved")
        about = await get(people, findings_path(), node="FEAT::API")
        everything = await get(people, findings_path(), status="all", limit=1)
        second = await get(people, findings_path(), status="all", limit=1, after=everything.json()["next"])
        dismissed = await get(people, findings_path(), status="dismissed")
        unknown = await get(people, findings_path(), node="FEAT::NOPE")
        bad_status = await get(people, findings_path(), status="done")
        bad_after = await get(people, findings_path(), after="!!")
    assert other.json() == {"findings": [], "next": None}
    assert len(about.json()["findings"]) == 2
    assert [len(page.json()["findings"]) for page in (everything, second)] == [1, 1]
    assert everything.json()["next"] is not None and second.json()["next"] is None
    assert dismissed.json() == {"findings": [], "next": None}  # a dismissed gap is gone
    assert (unknown.status_code, unknown.json()) == (200, {"findings": [], "next": None})  # an unknown node has none
    assert bad_status.status_code == 422
    assert bad_after.status_code == 400 and bad_after.json()["error"]["details"] == {"argument": "after"}


async def test_a_project_the_caller_is_not_a_member_of_and_an_unknown_one_are_the_same_404_and_no_token_is_401(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        calls = [
            ("get", findings_path(FOREIGN)),
            ("get", findings_path(UNKNOWN_PROJECT)),
            ("post", findings_path(FOREIGN, blocking_id(people), "resolve")),
            ("post", findings_path(FOREIGN, blocking_id(people), "dismiss")),
            ("get", claims_path(FOREIGN)),
            ("post", claims_path(FOREIGN, API, "release")),
        ]
        before = state(people)
        answers = [await (get if verb == "get" else post)(people, path) for verb, path in calls]
        anonymous = await people.rig.client.get(findings_path())
        assert state(people) == before
    assert {answer.status_code for answer in answers} == {404}
    assert {answer.json()["error"]["code"] for answer in answers} == {"project_not_found"}
    assert anonymous.status_code == 401


async def test_without_a_gap_runner_the_findings_routes_answer_gap_unavailable(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.rig.runtime.gaps = None
        listed = await get(people, findings_path())
        resolved = await post(people, findings_path(ALPHA, blocking_id(people), "resolve"))
    for refused in (listed, resolved):
        assert (refused.status_code, refused.json()["error"]["code"]) == (503, "gap_unavailable")


# --- resolve and dismiss -------------------------------------------------------------------------------------------


async def test_resolving_a_blocking_finding_dismisses_its_gap_with_the_note_and_returns_its_tasks_to_pending(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        gap_id = blocking_id(people)
        response = await post(people, findings_path(ALPHA, gap_id, "resolve"), {"note": "Euro and dollar only."})
        task = people.memory.tasks[STUCK_TASK]
        listed = await get(people, findings_path(), status="all")
        (dismissed,) = [row for row in people.records.event_rows[ALPHA] if row.event == "dismissed"]
    assert response.status_code == 200
    body = response.json()
    assert body["unblocked"] == 1
    assert (body["finding"]["finding_id"], body["finding"]["status"]) == (gap_id, "resolved")
    assert (body["finding"]["resolution_note"], body["finding"]["resolved_by"]) == (None, None)  # no clarification
    assert (task["status"], task.get("blocked_by")) == ("pending", None)  # blocked_by the alias FND::1: released
    assert gap_id not in people.gap.gaps() and gap_id not in [i["finding_id"] for i in listed.json()["findings"]]
    assert (dismissed.gap_id, dismissed.actor, dismissed.detail["reason"]) == (
        gap_id,
        str(people.alice),
        "Euro and dollar only.",
    )
    logged = events(caplog, "api.findings.closed")
    assert [(e.action, e.outcome, e.finding_id) for e in logged] == [("resolve", "ok", gap_id)]
    assert "Euro and dollar" not in caplog.text and "Which currencies" not in caplog.text


async def test_a_finding_is_closed_by_its_migrated_first_slice_id_too(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await post(people, findings_path(ALPHA, ALIAS, "resolve"))
        task = people.memory.tasks[STUCK_TASK]
        (dismissed,) = [row for row in people.records.event_rows[ALPHA] if row.event == "dismissed"]
    assert response.status_code == 200 and response.json()["finding"]["finding_id"] == blocking_id(people)
    assert (response.json()["unblocked"], task["status"]) == (1, "pending")
    assert dismissed.detail["reason"] == "Resolved"  # the default reason of a resolve without a note


async def test_dismissing_unblocks_too_and_a_plain_finding_unblocks_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        dismissed = await post(people, findings_path(ALPHA, blocking_id(people), "dismiss"), {"note": "Out of scope."})
        plain = await post(people, findings_path(ALPHA, other_id(people), "dismiss"))
        task = people.memory.tasks[STUCK_TASK]
        reasons = [row.detail["reason"] for row in people.records.event_rows[ALPHA] if row.event == "dismissed"]
    assert dismissed.json()["unblocked"] == 1 and dismissed.json()["finding"]["status"] == "dismissed"
    assert (plain.status_code, plain.json()["unblocked"], plain.json()["finding"]["resolution_note"]) == (200, 0, None)
    assert plain.json()["finding"]["status"] == "dismissed"
    assert task["status"] == "pending"
    assert reasons == ["Out of scope.", "Dismissed"]
    assert people.gap.gaps() == {}


async def test_a_closed_unknown_not_open_or_oversized_call_is_refused_and_writes_nothing(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        gap_id, other = blocking_id(people), other_id(people)
        await post(people, findings_path(ALPHA, gap_id, "resolve"))
        people.gap.nodes[("Gap", gap_uuid(ALPHA, other))]["status"] = "answered"  # a person answered it: not open
        before = state(people)
        again = await post(people, findings_path(ALPHA, gap_id, "dismiss"), {"note": "late"})
        answered = await post(people, findings_path(ALPHA, other, "dismiss"))
        unknown = await post(people, findings_path(ALPHA, "GAP::999999999999", "resolve"))
        long_note = await post(people, findings_path(ALPHA, other, "resolve"), {"note": "x" * 501})
        extra = await post(people, findings_path(ALPHA, other, "resolve"), {"who": "me"})
        assert state(people) == before
    assert (again.status_code, again.json()["error"]["code"]) == (404, "finding_not_found")  # a dismissed gap is gone
    assert (answered.status_code, answered.json()["error"]["code"]) == (409, "finding_closed")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "finding_not_found")
    assert long_note.status_code == 422 and extra.status_code == 422
    assert [e.outcome for e in events(caplog, "api.findings.closed")] == [
        "ok",
        "unknown_finding",
        "wrong_state",
        "unknown_finding",
    ]


async def test_a_resolve_during_a_plan_persist_answers_plan_applying_and_the_retry_heals(monkeypatch, env):
    """Releasing the gap's tasks needs the plan, which a plan persist holds: the dismissal releases first, so the
    refusal changes nothing (the gap stays open, the task blocked) and a retry after the persist closes it."""

    async with serve_people(monkeypatch, env) as people:
        plan = people.memory.plan
        people.memory.plan = {**plan, "applying": "run-1"}
        busy = await post(people, findings_path(ALPHA, blocking_id(people), "resolve"))
        still_blocked = people.memory.tasks[STUCK_TASK]["status"]
        still_open = (await people.gaps.get_gap(ALPHA, blocking_id(people))).status
        people.memory.plan = plan
        retry = await post(people, findings_path(ALPHA, blocking_id(people), "resolve"))
        released = people.memory.tasks[STUCK_TASK]["status"]
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "plan_applying")
    assert (still_blocked, still_open) == ("blocked", "open")
    assert (retry.status_code, retry.json()["unblocked"], released) == (200, 1, "pending")


# --- the claims board ----------------------------------------------------------------------------------------------


async def test_a_project_without_held_work_has_an_empty_board(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await get(people, claims_path())
    assert (response.status_code, response.json()) == (200, {"claims": []})


async def test_the_board_lists_story_and_task_claims_oldest_first_with_names_label_and_branch(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.claim_task(CONTRACT_TASK, people.ben)
        await people.hold(API, people.alice, "claude-code", branch="fix/api")
        response = await get(people, claims_path())
    assert response.status_code == 200
    first, second = response.json()["claims"]
    assert (first["kind"], first["story_id"], first["task_id"], first["tasks"]) == ("story", API, None, 2)
    assert first["holder"] == {"name": "Alice", "you": True, "agent_label": "claude-code"}
    assert (second["kind"], second["story_id"], second["task_id"], second["tasks"]) == (
        "task",
        "STORY::A::X",
        CONTRACT_TASK,
        1,
    )
    assert second["holder"] == {"name": "Ben", "you": False, "agent_label": None}
    assert first["branch"] == "fix/api" and second["branch"] is None
    assert first["claimed_at"] < second["claimed_at"]
    assert "@" not in response.text and "token" not in response.text


async def test_a_story_with_a_story_claim_is_one_row_not_one_per_task(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.alice)
        rows = (await get(people, claims_path())).json()["claims"]
    assert [(row["kind"], row["story_id"], row["tasks"]) for row in rows] == [("story", API, 2)]


async def test_a_project_without_a_plan_answers_no_plan(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.plan = None
        response = await get(people, claims_path())
    assert (response.status_code, response.json()["error"]["code"]) == (404, "no_plan")


# --- the forced release --------------------------------------------------------------------------------------------


async def test_any_member_frees_a_forgotten_story_claim_and_the_holders_token_stops_working(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await people.hold(API, people.alice, "claude-code")
        released = await post(people, claims_path(ALPHA, API, "release"))
        board = await get(people, claims_path())
        stale = await people.rig.post(
            AGENT_ROUTES["release_story"], {"story_id": API, "claim_token": token}, project_id=ALPHA
        )
        repeat = await post(people, claims_path(ALPHA, API, "release"))
        statuses = [people.memory.tasks[t]["status"] for t in ("TASK::A::API_CONTRACT", "TASK::A::API_LOGIC")]
    assert (released.status_code, released.json()) == (200, {"released": 2})
    assert statuses == ["pending", "pending"] and board.json() == {"claims": []}
    assert stale.status_code == 200 and stale.json()["released"] == []  # the holder's own repeat of a release
    assert (repeat.status_code, repeat.json()) == (200, {"released": 0})
    logged = events(caplog, "api.claims.released")
    assert [(e.story_id, e.tasks, e.outcome) for e in logged] == [(API, 2, "ok"), (API, 0, "ok")]
    assert "claude-code" not in caplog.text and token not in caplog.text


async def test_an_unknown_story_is_404_and_a_story_nobody_holds_is_left_alone(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = state(people)
        unknown = await post(people, claims_path(ALPHA, "STORY::A::NOPE", "release"))
        idle = await post(people, claims_path(ALPHA, UI, "release"))
        assert state(people) == before
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")
    assert (idle.status_code, idle.json()) == (200, {"released": 0})


# --- audience, limits and edges (polish) --------------------------------------------------------------------------


async def test_an_mcp_audience_token_is_401_on_all_five_web_routes_and_changes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = state(people)
        token = mcp_token()
        answers = [
            await get(people, findings_path(), token=token),
            await post(people, findings_path(ALPHA, blocking_id(people), "resolve"), token=token),
            await post(people, findings_path(ALPHA, blocking_id(people), "dismiss"), token=token),
            await get(people, claims_path(), token=token),
            await post(people, claims_path(ALPHA, API, "release"), token=token),
        ]
        assert state(people) == before
    assert [answer.status_code for answer in answers] == [401] * 5


async def test_a_blocking_finding_resolved_after_a_plan_run_reset_its_task_unblocks_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.set_task(STUCK_TASK, status="pending", blocked_by=None)  # D13: the run reset it
        response = await post(people, findings_path(ALPHA, blocking_id(people), "resolve"))
        status = people.memory.tasks[STUCK_TASK]["status"]
    assert (response.status_code, response.json()["unblocked"], status) == (200, 0, "pending")
    assert response.json()["finding"]["status"] == "resolved"


async def test_ids_over_200_characters_and_a_limit_out_of_range_are_422(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = state(people)
        long_id = "X" * 201
        answers = [
            await post(people, findings_path(ALPHA, long_id, "resolve")),
            await post(people, findings_path(ALPHA, long_id, "dismiss")),
            await post(people, claims_path(ALPHA, long_id, "release")),
            await get(people, findings_path(), node=long_id),
            await get(people, findings_path(), limit=0),
            await get(people, findings_path(), limit=201),
        ]
        assert state(people) == before
    assert [answer.status_code for answer in answers] == [422] * 6


async def test_releasing_a_story_that_has_only_a_task_claim_frees_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.claim_task(CONTRACT_TASK, people.ben)
        response = await post(people, claims_path(ALPHA, "STORY::A::X", "release"))
        task = people.memory.tasks[CONTRACT_TASK]
    assert (response.status_code, response.json()) == (200, {"released": 0})
    assert (task["status"], task["claimed_by"]) == ("in_progress", str(people.ben))


async def test_a_merged_node_id_filters_through_its_alias(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.gap.spec("FEAT::OLD")
        people.gap.merge_spec("FEAT::OLD", API_NODE)
        response = await get(people, findings_path(), node="FEAT::OLD")
    assert [item["finding_id"] for item in response.json()["findings"]] == [other_id(people), blocking_id(people)]


async def test_a_retired_story_with_a_claim_is_not_on_the_board(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.alice)
        people.memory.set_story(API, status="retired")
        response = await get(people, claims_path())
    assert response.json() == {"claims": []}
