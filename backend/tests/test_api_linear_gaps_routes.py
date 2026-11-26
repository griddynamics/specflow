"""The Linear gap routes over the REST app (``docs/linear/gaps.md`` "Routes"): read and save the target, and the access
check as the member the project's Linear is connected by. The Linear, the gap service and the rows are the fakes of
``linear_gaps_support``.

Standings as in test_api_jira_gaps_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence of ``PUT``::

    web -> project_member -> resolve team (and project) as the bound member -> transaction: save_target -> commit
        -> channel.after_save (background post of the open questions, then the catch-up read) -> 200 LinearGapsOut
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from atlassian_support import oauth_env
from jira_gaps_support import gap
from linear.errors import LinearRateLimitedError
from linear_gaps_support import ACTING, Rig, build_rig
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
PATH = f"projects/{ALPHA}/linear-gaps"
PROJECT = uuid.UUID(ALPHA)
BODY = {"team_key": "ENG", "grouping": "one", "enabled": True}
EMPTY = {
    "available": True,
    "enabled": False,
    "team_id": None,
    "team_key": None,
    "linear_project_id": None,
    "grouping": None,
    "parent_identifier": None,
    "parent_url": None,
    "posted": 0,
    "answered": 0,
    "error_code": None,
}


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env, **oauth_env())) as served:
        runtime, repo = served.runtime, served.repo
        wired: Rig = build_rig(monkeypatch, runtime, PROJECT)
        wired.db.url_keys[PROJECT] = "graphico-app"
        channel = runtime.linear_gaps = wired.channel()
        await sign_up(repo, BEN, collaborator=True)
        await sign_up(repo, CAROL)
        yield SimpleNamespace(http=served.client, wired=wired, runtime=runtime)
        await channel.aclose()


async def sign_up(repo: Any, email: str, *, collaborator: bool = False) -> None:
    """The person has an account; a collaborator is also a member of ``Alpha``."""

    user = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{email}", email=email, name=None))
    if collaborator:
        repo.members.add((PROJECT, user.id))
        repo.roles[PROJECT, user.id] = "collaborator"


async def settle(rig: Any) -> None:
    tasks = list(rig.runtime.linear_gaps._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


# GET and PUT -------------------------------------------------------------------------------------------------------


async def test_a_project_without_a_target_reads_as_empty(rig) -> None:
    answer = await call(rig, "GET", PATH, BEN)

    assert (answer.status_code, answer.json()) == (200, EMPTY)


async def test_a_member_saves_the_target_and_the_open_questions_are_posted_in_the_background(rig) -> None:
    rig.wired.service.add(gap("G-1"), gap("G-2"))

    saved = await call(rig, "PUT", PATH, BEN, {"team_key": " eng ", "grouping": "one", "enabled": True})
    await settle(rig)
    read = await call(rig, "GET", PATH, ALICE)

    team = rig.wired.team
    assert saved.status_code == 200 and saved.json()["team_key"] == "ENG" and saved.json()["enabled"] is True
    body = read.json()
    assert (body["team_id"], body["linear_project_id"], body["grouping"]) == (team.id, None, "one")
    assert body["parent_identifier"] == "ENG-1"
    assert body["parent_url"] == "https://linear.app/graphico-app/issue/ENG-1"
    assert (body["posted"], body["answered"], body["error_code"]) == (2, 0, None)


async def test_the_chosen_linear_project_is_resolved_by_name_or_link(rig) -> None:
    board = rig.wired.linear.add_project(rig.wired.team.id, "Shop board")

    by_name = await call(rig, "PUT", PATH, ALICE, {**BODY, "linear_project": " shop BOARD "})
    by_link = await call(rig, "PUT", PATH, ALICE, {**BODY, "linear_project": f"{board.url}/overview"})

    assert by_name.json()["linear_project_id"] == board.id == by_link.json()["linear_project_id"]


async def test_saving_again_without_a_change_does_not_post_again(rig) -> None:
    rig.wired.service.add(gap("G-1"))
    await call(rig, "PUT", PATH, ALICE, {**BODY, "grouping": "area"})
    await settle(rig)
    issues = len(rig.wired.issues())

    await call(rig, "PUT", PATH, ALICE, BODY)
    await settle(rig)

    assert len(rig.wired.issues()) == issues


async def test_turning_the_channel_off_needs_no_call_to_linear(rig) -> None:
    await call(rig, "PUT", PATH, ALICE, BODY)
    await settle(rig)
    rig.wired.linear.calls.clear()

    saved = await call(rig, "PUT", PATH, ALICE, {**BODY, "team_key": "XYZ", "enabled": False})

    assert saved.status_code == 200 and saved.json()["enabled"] is False and saved.json()["team_key"] == "ENG"
    assert rig.wired.linear.calls == []


async def test_answered_questions_are_counted(rig) -> None:
    rig.wired.service.add(gap("G-1"))
    await call(rig, "PUT", PATH, ALICE, BODY)
    await settle(rig)
    await rig.wired.db.mark_answered(None, PROJECT, "G-1", now=rig.wired.linear.now())

    body = (await call(rig, "GET", PATH, ALICE)).json()

    assert (body["posted"], body["answered"]) == (1, 1)


@pytest.mark.parametrize(
    "body",
    [
        {"team_key": "ENG", "grouping": "one"},
        {"team_key": "ENG", "grouping": "both", "enabled": True},
        {"team_key": "e-ng", "grouping": "one", "enabled": True},
        {"team_key": "", "grouping": "one", "enabled": True},
        {"team_key": "ENG", "grouping": "one", "enabled": "yes"},
        {"team_key": "ENG", "grouping": "one", "enabled": True, "story": "ENG-1"},
        {"team_key": "ENG", "grouping": "one", "enabled": True, "linear_project": "x" * 301},
    ],
)
async def test_a_bad_body_is_refused(rig, body: dict[str, Any]) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, body)) == (422, "validation_failed")


async def test_a_team_or_project_the_member_cannot_see_is_refused_and_nothing_is_saved(rig) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, {**BODY, "team_key": "NOPE"})) == (404, "linear_team_not_found")
    assert error_of(await call(rig, "PUT", PATH, ALICE, {**BODY, "linear_project": "Nothing"})) == (
        404,
        "linear_project_not_found",
    )
    assert await rig.wired.db.get_target(None, PROJECT) is None


async def test_saving_needs_a_member_who_connected_linear(rig) -> None:
    rig.wired.integrations.rows.clear()  # the project has no Linear binding

    answer = await call(rig, "PUT", PATH, ALICE, BODY)

    assert error_of(answer) == (409, "linear_not_connected")
    assert await rig.wired.db.get_target(None, PROJECT) is None


async def test_saving_with_an_expired_connection_asks_to_connect_again(rig) -> None:
    rig.wired.connections.status[ACTING] = "expired"

    assert error_of(await call(rig, "PUT", PATH, ALICE, BODY)) == (409, "linear_reconnect_required")


async def test_a_linear_refusal_is_a_closed_code(rig) -> None:
    rig.wired.linear.fail_next(LinearRateLimitedError(2.0), method="teams")

    answer = await call(rig, "PUT", PATH, ALICE, BODY)

    assert error_of(answer) == (429, "linear_rate_limited") and answer.headers["Retry-After"] == "2"


async def test_a_non_member_sees_no_project(rig) -> None:
    assert error_of(await call(rig, "GET", PATH, CAROL)) == (404, "project_not_found")
    assert error_of(await call(rig, "PUT", PATH, CAROL, BODY))[0] == 404
    assert error_of(await call(rig, "POST", f"{PATH}/access-check", CAROL, {"team_key": "ENG"}))[0] == 404


async def test_without_linear_or_gap_runs_the_routes_say_so(rig) -> None:
    rig.runtime.linear_gaps = None  # gap runs are off

    assert error_of(await call(rig, "PUT", PATH, ALICE, BODY)) == (503, "gap_unavailable")
    assert (await call(rig, "GET", PATH, ALICE)).json() == {**EMPTY, "available": False}

    rig.runtime.linear = None  # no Linear OAuth app on this server

    assert error_of(await call(rig, "PUT", PATH, ALICE, BODY)) == (503, "linear_unavailable")
    assert error_of(await call(rig, "POST", f"{PATH}/access-check", ALICE, {"team_key": "ENG"})) == (
        503,
        "linear_unavailable",
    )
    assert (await call(rig, "GET", PATH, ALICE)).json() == {**EMPTY, "available": False}


# The access check --------------------------------------------------------------------------------------------------


async def check(rig: Any, body: dict[str, Any] | None = None, who: str = BEN) -> httpx.Response:
    return await call(rig, "POST", f"{PATH}/access-check", who, body or {"team_key": " eng "})


async def test_the_check_runs_as_the_bound_member_and_names_the_team(rig) -> None:
    answer = await check(rig)

    assert answer.json() == {"result": "ok", "team_name": "Engineering", "linear_project_name": None}
    assert rig.wired.linear.writes == []  # a check only reads


async def test_the_check_names_the_linear_project_too(rig) -> None:
    rig.wired.linear.add_project(rig.wired.team.id, "Shop board")

    answer = await check(rig, {"team_key": "ENG", "linear_project": "shop board"})

    assert answer.json() == {"result": "ok", "team_name": "Engineering", "linear_project_name": "Shop board"}


@pytest.mark.parametrize(
    ("body", "reason", "field"),
    [
        ({"team_key": "NOPE"}, "linear_team_not_found", "team_key"),
        ({"team_key": "ENG", "linear_project": "Nothing"}, "linear_project_not_found", "linear_project"),
    ],
)
async def test_a_team_or_project_that_is_not_there_names_the_field(rig, body, reason: str, field: str) -> None:
    assert (await check(rig, body)).json() == {"result": "failed", "reason": reason, "field": field}


async def test_a_blank_project_is_no_project(rig) -> None:
    assert (await check(rig, {"team_key": "ENG", "linear_project": "  "})).json()["result"] == "ok"


@pytest.mark.parametrize(
    ("status", "reason"), [(None, "linear_not_connected"), ("expired", "linear_reconnect_required")]
)
async def test_without_a_usable_bound_member_the_check_says_why_and_calls_nothing(
    rig, status: str | None, reason: str
) -> None:
    if status is None:
        del rig.wired.connections.status[ACTING]
    else:
        rig.wired.connections.status[ACTING] = status

    assert (await check(rig)).json() == {"result": "failed", "reason": reason, "field": None}
    assert rig.wired.linear.calls == []


async def test_a_linear_refusal_is_a_closed_reason_without_a_field(rig) -> None:
    rig.wired.linear.rate_limit_next(method="teams")

    assert (await check(rig)).json() == {"result": "failed", "reason": "linear_rate_limited", "field": None}
