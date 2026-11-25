"""The Jira gap routes over the REST app (``docs/jira-sync/gaps.md`` "Routes"): read and save the target, and the access
check as the member the project's Atlassian is connected by. The Jira, the gap service and the rows are the fakes of
``jira_gaps_support``.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence of ``PUT``::

    web -> project_member -> bound member exists? -> transaction: save_target -> commit
        -> channel.after_save (background post of the open questions) -> 200 JiraGapsOut
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from api.jira_gaps.poller import POLL_SECONDS
from atlassian_support import oauth_env
from jira.gateway import JiraError, JiraProject
from jira.reasons import Reason
from jira_gaps_support import ACTING, ANA_ACCOUNT, Rig, build_rig, gap
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
PATH = f"projects/{ALPHA}/jira-gaps"
PROJECT = uuid.UUID(ALPHA)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env, **oauth_env())) as served:
        runtime, repo = served.runtime, served.repo
        wired: Rig = build_rig(monkeypatch, runtime, PROJECT)
        channel = wired.channel()
        wired.runtime.jira_gaps = channel
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        repo.members.add((PROJECT, ben.id))
        repo.roles[(PROJECT, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        yield SimpleNamespace(http=served.client, wired=wired, runtime=runtime)
        await channel.aclose()


async def settle(rig: Any) -> None:
    tasks = list(rig.runtime.jira_gaps._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


# GET and PUT -------------------------------------------------------------------------------------------------------


async def test_a_project_without_a_target_reads_as_empty_and_says_how_often_answers_are_read(rig) -> None:
    answer = await call(rig, "GET", PATH, BEN)

    assert answer.status_code == 200
    assert answer.json() == {
        "available": True,
        "jira_project": None,
        "grouping": None,
        "enabled": False,
        "story_key": None,
        "story_url": None,
        "posted": 0,
        "answered": 0,
        "last_polled_at": None,
        "error_code": None,
        "poll_seconds": POLL_SECONDS,
    }


async def test_a_member_saves_the_target_and_the_open_questions_are_posted_in_the_background(rig) -> None:
    rig.wired.service.add(gap("G-1"), gap("G-2"))

    saved = await call(rig, "PUT", PATH, BEN, {"jira_project": " shop ", "grouping": "one", "enabled": True})
    await settle(rig)
    read = await call(rig, "GET", PATH, ALICE)

    assert saved.status_code == 200 and saved.json()["jira_project"] == "SHOP" and saved.json()["enabled"] is True
    body = read.json()
    assert body["story_key"] == "SHOP-1" and body["story_url"] == "https://example.atlassian.net/browse/SHOP-1"
    assert (body["posted"], body["answered"], body["grouping"]) == (2, 0, "one")


async def test_saving_again_without_a_change_does_not_post_again(rig) -> None:
    rig.wired.service.add(gap("G-1"))
    body = {"jira_project": "SHOP", "grouping": "area", "enabled": True}
    await call(rig, "PUT", PATH, ALICE, body)
    await settle(rig)
    created = len(rig.wired.jira.created())

    await call(rig, "PUT", PATH, ALICE, {**body, "grouping": "one"})
    await settle(rig)

    assert len(rig.wired.jira.created()) == created


async def test_answered_questions_are_counted(rig) -> None:
    rig.wired.service.add(gap("G-1"))
    await call(rig, "PUT", PATH, ALICE, {"jira_project": "SHOP", "grouping": "one", "enabled": True})
    await settle(rig)
    rig.wired.jira.say("SHOP-2", ANA_ACCOUNT, "A")
    await rig.wired.poller().poll_once(ALPHA)

    body = (await call(rig, "GET", PATH, ALICE)).json()

    assert (body["posted"], body["answered"]) == (1, 1)
    assert body["last_polled_at"] is not None and body["error_code"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"jira_project": "SHOP", "grouping": "one"},
        {"jira_project": "SHOP", "grouping": "both", "enabled": True},
        {"jira_project": "shop-1", "grouping": "one", "enabled": True},
        {"jira_project": "SHOP", "grouping": "one", "enabled": "yes"},
        {"jira_project": "SHOP", "grouping": "one", "enabled": True, "epic_key": "SHOP-1"},
    ],
)
async def test_a_bad_body_is_refused(rig, body: dict[str, Any]) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, body)) == (422, "validation_failed")


async def test_saving_needs_a_member_who_connected_atlassian(rig) -> None:
    rig.wired.integrations.rows.clear()  # the project has no Atlassian binding

    answer = await call(rig, "PUT", PATH, ALICE, {"jira_project": "SHOP", "grouping": "one", "enabled": True})

    assert error_of(answer) == (409, "jira_not_connected")
    assert await rig.wired.db.get_target(None, PROJECT) is None


async def test_a_non_member_sees_no_project(rig) -> None:
    assert error_of(await call(rig, "GET", PATH, CAROL)) == (404, "project_not_found")
    assert (
        error_of(await call(rig, "PUT", PATH, CAROL, {"jira_project": "S", "grouping": "one", "enabled": True}))[0]
        == 404
    )


async def test_without_atlassian_oauth_or_gap_runs_the_routes_say_so(rig) -> None:
    body = {"jira_project": "SHOP", "grouping": "one", "enabled": True}
    rig.runtime.jira = SimpleNamespace(connections=None)  # token mode

    assert error_of(await call(rig, "PUT", PATH, ALICE, body)) == (503, "atlassian_oauth_unavailable")
    assert error_of(await call(rig, "POST", f"{PATH}/access-check", ALICE, {"jira_project": "SHOP"})) == (
        503,
        "atlassian_oauth_unavailable",
    )
    assert (await call(rig, "GET", PATH, ALICE)).json()["available"] is False

    rig.runtime.jira = SimpleNamespace(connections=object())
    rig.runtime.jira_gaps = None  # gap runs are off

    assert error_of(await call(rig, "PUT", PATH, ALICE, body)) == (503, "gap_unavailable")
    assert (await call(rig, "GET", PATH, ALICE)).json() == {
        "available": False,
        "jira_project": None,
        "grouping": None,
        "enabled": False,
        "story_key": None,
        "story_url": None,
        "posted": 0,
        "answered": 0,
        "last_polled_at": None,
        "error_code": None,
        "poll_seconds": POLL_SECONDS,
    }


# The access check --------------------------------------------------------------------------------------------------


async def check(rig: Any, who: str = BEN) -> httpx.Response:
    return await call(rig, "POST", f"{PATH}/access-check", who, {"jira_project": " shop "})


async def test_the_check_runs_as_the_bound_member_and_names_the_project(rig) -> None:
    answer = await check(rig)

    assert answer.json() == {"result": "ok", "project_name": "Shop", "epic_summary": None}
    assert ("project", "SHOP") in rig.wired.jira.calls  # the project key was normalised


@pytest.mark.parametrize("missing", ["CREATE_ISSUES", "ADD_COMMENTS", "BROWSE_PROJECTS"])
async def test_a_missing_permission_fails_the_check_on_the_project_field(rig, missing: str) -> None:
    rig.wired.jira.missing = frozenset({missing, "EDIT_ISSUES"})

    assert (await check(rig)).json() == {"result": "failed", "reason": "jira_forbidden", "field": "jira_project"}


async def test_permissions_the_channel_does_not_need_do_not_fail_the_check(rig) -> None:
    rig.wired.jira.missing = frozenset({"EDIT_ISSUES", "LINK_ISSUES", "SCHEDULE_ISSUES"})

    assert (await check(rig)).json()["result"] == "ok"


async def test_a_project_that_cannot_hold_stories_and_subtasks_fails_the_check(rig, monkeypatch) -> None:
    async def project(_key: str) -> JiraProject:
        return JiraProject("Shop", "1", "10", None)

    monkeypatch.setattr(rig.wired.jira, "project", project)

    assert (await check(rig)).json() == {
        "result": "failed",
        "reason": "jira_issue_types_missing",
        "field": "jira_project",
    }


@pytest.mark.parametrize(
    ("error", "reason", "field"),
    [
        (Reason.NOT_FOUND, "jira_not_found", "jira_project"),
        (Reason.AUTH_FAILED, "jira_auth_failed", None),
        (Reason.UNREACHABLE, "jira_unreachable", None),
        (Reason.INVALID_FIELD, "jira_unexpected", None),
    ],
)
async def test_a_jira_refusal_is_a_closed_reason_and_names_the_field_only_when_the_input_is_at_fault(
    rig, monkeypatch, error: Reason, reason: str, field: str | None
) -> None:
    async def refuse(_key: str) -> None:
        raise JiraError(error)

    monkeypatch.setattr(rig.wired.jira, "project", refuse)

    assert (await check(rig)).json() == {"result": "failed", "reason": reason, "field": field}


async def test_without_a_usable_bound_member_the_check_says_why_and_calls_nothing(rig) -> None:
    rig.wired.connections.status[ACTING] = "expired"

    assert (await check(rig)).json() == {"result": "failed", "reason": "jira_reconnect_required", "field": None}
    del rig.wired.connections.status[ACTING]
    assert (await check(rig)).json() == {"result": "failed", "reason": "jira_not_connected", "field": None}
    assert rig.wired.jira.calls == []
