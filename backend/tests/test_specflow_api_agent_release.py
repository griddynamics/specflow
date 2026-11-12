"""``POST /story-release`` and what every people route shares (FR-MCP-STORY-08, -09, access-control.md AC9-AC14):
release and its repeats, the token checks, A71's ``plan_applying``, and the common route behaviour (identity from the
token, a refused call writes nothing, denials, outages) over the real plan library (``people_support``)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES
from graph.core import ConfigurationError, InfrastructureUnavailableError
from people_support import API, UI, X, Y, serve_people
from spec_reads_support import Raising
from specflow_api_agent_support import (
    ALPHA,
    FOREIGN,
    PEOPLE_CALLS,
    UNKNOWN_PROJECT,
    USER_HEADER,
    agent_env,
    bearer_header,
    comparable,
    events,
    mcp_token,
    refuse_database,
)

CLAIM, RELEASE = AGENT_ROUTES["claim_story"], AGENT_ROUTES["release_story"]


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def release_body(token: str, story_id: str = API, **more: object) -> dict[str, Any]:
    return {"story_id": story_id, "claim_token": token, **more}


async def test_release_returns_the_claimed_tasks_to_pending_and_keeps_note_and_branch(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = (await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)).json()["claim_token"]
        await people.plan.report_progress(
            ALPHA, "TASK::A::API_CONTRACT", claimed_by=str(people.alice), claim_token=token, progress="done"
        )
        response = await people.post(
            RELEASE, release_body(token, note="Contract done; logic left", branch="fix/api"), project_id=ALPHA
        )
        tasks, story = people.memory.tasks, people.memory.stories[API]
    assert response.status_code == 200 and response.headers[USER_HEADER] == str(people.alice)
    assert response.json() == {
        "project_id": ALPHA,
        "story_id": API,
        "released": ["TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"],
    }
    assert [tasks[t]["status"] for t in response.json()["released"]] == ["pending", "pending"]
    assert tasks["TASK::A::API_CONTRACT"]["progress"] is None  # cleared by the release
    assert (story["note"], story["branch"], story["claimed_by"]) == ("Contract done; logic left", "fix/api", None)
    assert story["claim_token"] == token  # kept until the next claim: what makes the repeat work
    written = events(caplog, "api.agent.written")
    assert [(e.route, e.outcome, e.repeat) for e in written] == [
        ("story-claim", "ok", False),
        ("story-release", "ok", False),
    ]
    assert "Contract done" not in caplog.text and token not in caplog.text and "fix/api" not in caplog.text


async def test_a_second_release_with_the_same_token_answers_empty_and_changes_nothing(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = (await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)).json()["claim_token"]
        await people.post(RELEASE, release_body(token), project_id=ALPHA)
        statements = len(people.memory.statements)
        again = await people.post(RELEASE, release_body(token, note="late"), project_id=ALPHA)
        assert people.memory.stories[API].get("note") is None
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert again.status_code == 200 and again.json()["released"] == []
    assert [e.repeat for e in events(caplog, "api.agent.written")][-1] is True


async def test_another_members_token_a_wrong_token_or_a_free_story_is_claim_mismatch_and_writes_nothing(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben)
        token = (await people.post(CLAIM, {"story_id": X}, project_id=ALPHA)).json()["claim_token"]
        before = {key: dict(row) for key, row in people.memory.stories.items()}
        tasks = {key: dict(row) for key, row in people.memory.tasks.items()}
        foreign = await people.post(RELEASE, release_body(token), project_id=ALPHA)  # alice's token, ben's story
        wrong = await people.post(RELEASE, release_body("nope", X), project_id=ALPHA)
        free = await people.post(RELEASE, release_body(token, UI), project_id=ALPHA)
        assert before == people.memory.stories and tasks == people.memory.tasks
    for refused in (foreign, wrong, free):
        assert refused.status_code == 409
        assert refused.json()["error"] == {
            "code": "claim_mismatch",
            "message": "The claim token does not match the claim.",
            "details": {},
        }


async def test_releasing_one_story_of_a_cycle_releases_every_task_of_the_unit(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = (await people.post(CLAIM, {"story_id": X}, project_id=ALPHA)).json()["claim_token"]
        response = await people.post(RELEASE, release_body(token, Y), project_id=ALPHA)
        stories = people.memory.stories
    assert len(response.json()["released"]) == 4
    assert stories[X]["claimed_by"] is None and stories[Y]["claimed_by"] is None


async def test_a_release_during_a_plan_persist_is_plan_applying(monkeypatch, env):
    """A71: the non-forced release refuses while a plan persists, and writes nothing."""

    async with serve_people(monkeypatch, env) as people:
        token = (await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)).json()["claim_token"]
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        response = await people.post(RELEASE, release_body(token), project_id=ALPHA)
        assert people.memory.stories[API]["claimed_by"] == str(people.alice)
    assert response.status_code == 409 and response.json()["error"]["code"] == "plan_applying"


async def test_release_of_an_unknown_story_is_story_not_found_and_without_a_plan_no_plan(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        unknown = await people.post(RELEASE, release_body("t", "STORY::NOPE"), project_id=ALPHA)
        people.memory.plan = None
        no_plan = await people.post(RELEASE, release_body("t"), project_id=ALPHA)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")
    assert (no_plan.status_code, no_plan.json()["error"]["code"]) == (404, "no_plan")


@pytest.mark.parametrize("branch", ["", "b" * 201, "x\ty", "a\u0007"])
async def test_a_branch_that_breaks_the_format_releases_nothing(monkeypatch, env, branch):
    async with serve_people(monkeypatch, env) as people:
        token = (await people.post(CLAIM, {"story_id": API}, project_id=ALPHA)).json()["claim_token"]
        response = await people.post(RELEASE, release_body(token, branch=branch), project_id=ALPHA)
        assert people.memory.stories[API]["claimed_by"] == str(people.alice)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"argument": "branch"}


# --- what every people route shares -----------------------------------------------------------------------------


def request_of(people: Any, name: str, **over: Any) -> Any:
    method, path, params, body = PEOPLE_CALLS[name]
    params = {**params, **over}
    if method == "GET":
        return people.get(path, **params)
    return people.post(path, body, **params)


@pytest.mark.parametrize("name", list(PEOPLE_CALLS))
async def test_a_foreign_unknown_or_malformed_project_is_the_same_404_and_nothing_is_read_or_written(
    monkeypatch, env, name
):
    async with serve_people(monkeypatch, env) as people:
        before = (dict(people.memory.stories), dict(people.memory.tasks), len(people.memory.statements))
        denied = [await request_of(people, name, project_id=pid) for pid in (FOREIGN, UNKNOWN_PROJECT, "x/../y", "")]
        assert (dict(people.memory.stories), dict(people.memory.tasks), len(people.memory.statements)) == before
    assert {r.status_code for r in denied} == {404} and len({comparable(r) for r in denied}) == 1
    assert denied[0].json()["error"]["code"] == "project_not_found" and denied[0].headers[USER_HEADER]


@pytest.mark.parametrize("name", list(PEOPLE_CALLS))
async def test_a_call_without_a_token_or_with_a_web_token_is_401_and_a_postgres_outage_503(monkeypatch, env, name):
    async with serve_people(monkeypatch, env) as people:
        method, path, params, body = PEOPLE_CALLS[name]
        bare = (
            await people.rig.client.get(path, params=params)
            if method == "GET"
            else await people.rig.client.post(path, params=params, json=body)
        )
        refuse_database(monkeypatch)
        down = await request_of(people, name)
    assert bare.status_code == 401 and USER_HEADER not in bare.headers
    assert down.status_code == 503 and down.json()["error"]["code"] == "database_unavailable"


@pytest.mark.parametrize("name", list(PEOPLE_CALLS))
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InfrastructureUnavailableError("neo4j_unreachable"), 503, "graph_unavailable"),
        (ConfigurationError("unknown_label", label="X"), 500, "internal_error"),
    ],
)
async def test_a_graph_outage_or_a_bug_below_the_route_is_a_coded_error_with_no_text(
    monkeypatch, env, name, *, error, status, code
):
    async with serve_people(monkeypatch, env) as people:
        reads = people.rig.runtime.agent_reads
        people.rig.runtime.agent_reads = dataclasses.replace(reads, plan=Raising(error), findings=Raising(error))
        response = await request_of(people, name)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "neo4j_unreachable" not in response.text and "unknown_label" not in response.text


async def test_the_caller_is_the_tokens_user_whatever_the_request_says(monkeypatch, env):
    """Identity only from the token (D10): a user id in the body, the query or a header changes nothing."""

    async with serve_people(monkeypatch, env) as people:
        ben_token = bearer_header(mcp_token("ben@example.com"))
        response = await people.rig.client.post(
            CLAIM,
            params={"project_id": ALPHA, "user_id": str(people.alice)},
            json={"story_id": API, "claimed_by": str(people.alice), "user_id": str(people.alice)},
            headers={**ben_token, USER_HEADER: str(people.alice)},
        )
        story = people.memory.stories[API]
    assert response.status_code == 200
    assert story["claimed_by"] == str(people.ben) != str(people.alice)
    assert response.headers[USER_HEADER] == str(people.ben)


async def test_a_body_out_of_schema_is_422_and_writes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = dict(people.memory.stories)
        empty = await people.post(CLAIM, {"story_id": ""}, project_id=ALPHA)
        missing = await people.post(RELEASE, {"story_id": API}, project_id=ALPHA)
        long_note = await people.post(RELEASE, release_body("t", note="n" * 501), project_id=ALPHA)
        assert people.memory.stories == before
    assert [r.status_code for r in (empty, missing, long_note)] == [422, 422, 422]


async def test_alice_presenting_bens_valid_token_releases_nothing_and_is_claim_mismatch(monkeypatch, env, caplog):
    # A107: the token alone is not enough; the library checks the holder under the story lock.
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await people.hold(API, people.ben)
        before = (
            {k: dict(v) for k, v in people.memory.stories.items()},
            {k: dict(v) for k, v in people.memory.tasks.items()},
        )
        statements = len(people.memory.statements)
        response = await people.post(RELEASE, release_body(token), project_id=ALPHA)
        after = (
            {k: dict(v) for k, v in people.memory.stories.items()},
            {k: dict(v) for k, v in people.memory.tasks.items()},
        )
        assert after == before and people.memory.stories[API]["claimed_by"] == str(people.ben)
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert (response.status_code, response.json()["error"]["code"]) == (409, "claim_mismatch")
    assert [e.outcome for e in events(caplog, "api.agent.written")] == ["claim_mismatch"]


async def test_a_release_repeated_by_another_member_with_the_last_token_is_claim_mismatch(monkeypatch, env):
    # Ben holds and releases API; Alice then presents Ben's last token: not her repeat.
    async with serve_people(monkeypatch, env) as people:
        token = await people.hold(API, people.ben)
        await people.plan.release_story(ALPHA, API, claimed_by=str(people.ben), claim_token=token)
        statements = len(people.memory.statements)
        response = await people.post(RELEASE, release_body(token), project_id=ALPHA)
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert (response.status_code, response.json()["error"]["code"]) == (409, "claim_mismatch")
