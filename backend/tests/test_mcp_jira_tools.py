"""``sync_to_jira`` and ``jira_sync_status`` through the MCP router over the REAL API app, in process: a recording
transport between the two services, the MCP SDK client in front, the Jira runner of ``test_api_jira_routes`` (fake Jira,
fake tables, a stub ``sync_once``) behind the API.

Covers the requests the tools send (FR-JIRA-MCP-01, -02: ``POST`` with the project in the query and ``{}`` as the body;
``GET`` with ``limit`` and ``cursor``), the answers equal to the routes', any member (a collaborator) syncing, and the
errors reaching the agent as their codes with the fixed ``MESSAGES`` text (FR-JIRA-MCP-03).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from agent_contracts import AGENT_ROUTES, JiraSyncReport, JiraSyncStatus
from mcp_server.errors import MESSAGES
from mcp_server.registry import install
from mcp_support import ALICE, MCP_URL, build_app, call, code, mcp_env, sdk_session, structured, suite_token, text
from specflow_api_agent_support import ALPHA, FOREIGN
from test_api_jira_routes import BEN, rig, seed_items, settle, target_view
from test_mcp_spec_tools_over_api import Recorder

BEN_TOKEN = suite_token(BEN)
__all__ = ["rig"]  # the fixture of the route tests, used by name below


def stack(rig: Any, valid_env: dict[str, str]) -> tuple[Any, Recorder]:
    """The MCP app whose API client is the recording transport over the API app of ``rig``."""

    recorder = Recorder(httpx.ASGITransport(app=rig.app, raise_app_exceptions=False))
    app = build_app(mcp_env(valid_env), transport=recorder, tools=lambda server: install(server, resource_url=MCP_URL))
    return app, recorder


async def test_sync_to_jira_posts_the_project_in_the_query_and_an_empty_body_and_answers_the_status(rig, valid_env):
    """FR-JIRA-MCP-01: a collaborator (any member) starts a sync; the answer is the API's ``JiraSyncStatus``; a repeat
    while the pass runs answers ``queued``."""

    rig.db.targets[rig.alpha] = target_view(rig.alpha)
    rig.stub.gate = asyncio.Event()
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, BEN_TOKEN) as session:
        first = await call(session, "sync_to_jira", project_id=ALPHA)
        repeat = await call(session, "sync_to_jira", project_id=ALPHA)
    rig.stub.gate.set()
    await settle(rig)

    (sent, answer), (_, merged) = recorder.agent_calls()
    assert (sent.method, sent.url.path, dict(sent.url.params)) == (
        "POST",
        AGENT_ROUTES["sync_to_jira"],
        {"project_id": ALPHA},
    )
    assert json.loads(sent.content) == {}
    assert not first.is_error and answer.status_code == 200
    assert structured(first) == answer.json() and JiraSyncStatus.model_validate(structured(first))
    assert json.loads(text(first)) == structured(first)
    assert (structured(first)["status"], structured(first)["queued"]) == ("running", False)
    assert (structured(repeat)["queued"], merged.json()["queued"]) == (True, True)


async def test_jira_sync_status_sends_limit_and_cursor_and_pages_the_items(rig, valid_env):
    """FR-JIRA-MCP-02: ``GET`` with ``limit`` and ``cursor`` as query parameters; two pages cover the six items."""

    await seed_items(rig)
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        first = await call(session, "jira_sync_status", project_id=ALPHA, limit=4)
        second = await call(
            session, "jira_sync_status", project_id=ALPHA, limit=4, cursor=structured(first)["next_cursor"]
        )

    (one, one_answer), (two, two_answer) = recorder.agent_calls()
    assert (one.method, one.url.path) == ("GET", AGENT_ROUTES["jira_sync_status"])
    assert dict(one.url.params) == {"project_id": ALPHA, "limit": "4"}
    assert dict(two.url.params) == {"project_id": ALPHA, "limit": "4", "cursor": structured(first)["next_cursor"]}
    assert structured(first) == one_answer.json() and structured(second) == two_answer.json()
    assert JiraSyncReport.model_validate(structured(first)) and json.loads(text(second)) == structured(second)
    assert [i["node_id"] for i in structured(first)["items"] + structured(second)["items"]] == [
        "EPIC",
        "STORY::A",
        "STORY::B",
        "TASK::A",
        "TASK::B",
        "TASK::C",
    ]
    assert structured(second)["next_cursor"] is None


async def test_a_missing_or_disabled_target_reaches_the_agent_as_jira_not_configured(rig, valid_env):
    """FR-JIRA-MCP-03: the API's 409 is the code with the fixed message, never the API's own text."""

    app, recorder = stack(rig, valid_env)
    async with sdk_session(app, ALICE) as session:
        no_target = await call(session, "sync_to_jira", project_id=ALPHA)
        no_status = await call(session, "jira_sync_status", project_id=ALPHA)
        rig.db.targets[rig.alpha] = target_view(rig.alpha, enabled=False)
        disabled = await call(session, "sync_to_jira", project_id=ALPHA)
        shown = await call(session, "jira_sync_status", project_id=ALPHA)

    assert [code(r) for r in (no_target, no_status, disabled)] == ["jira_not_configured"] * 3
    assert structured(no_target)["error"] == {"code": "jira_not_configured", "message": MESSAGES["jira_not_configured"]}
    assert [answer.status_code for _, answer in recorder.agent_calls()] == [409, 409, 409, 200]
    assert not shown.is_error and structured(shown)["enabled"] is False
    assert rig.stub.calls == []


async def test_no_atlassian_account_reaches_the_agent_as_jira_unavailable(rig, valid_env):
    rig.runtime.jira = None
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        synced = await call(session, "sync_to_jira", project_id=ALPHA)
        read = await call(session, "jira_sync_status", project_id=ALPHA)

    assert [code(r) for r in (synced, read)] == ["jira_unavailable"] * 2
    assert structured(read)["error"]["message"] == MESSAGES["jira_unavailable"]
    assert [answer.status_code for _, answer in recorder.agent_calls()] == [503, 503]
    assert "service@example.com" not in text(synced) + text(read)


async def test_a_foreign_project_and_a_forged_cursor_keep_their_codes(rig, valid_env):
    await seed_items(rig)
    app, _ = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        foreign = await call(session, "sync_to_jira", project_id=FOREIGN)
        forged = await call(session, "jira_sync_status", project_id=ALPHA, cursor="forged")

    assert (code(foreign), code(forged)) == ("project_not_found", "invalid_cursor")
