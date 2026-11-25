"""``sync_to_linear`` and ``linear_sync_status`` through the MCP router over the REAL API app, in process: a recording
transport between the two services, the MCP SDK client in front, the Linear plan runner of
``test_api_linear_plan_routes`` (fake Linear, fake tables, a stub pass) behind the API. The twin of
``test_mcp_jira_tools``.

Covers the requests the tools send (``POST`` with the project in the query and ``{}`` as the body; ``GET`` with
``limit`` and ``cursor``), the answers equal to the routes', any member (a collaborator) syncing, and the errors
reaching the agent as their codes with the fixed ``MESSAGES`` text.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from agent_contracts import AGENT_ROUTES, LinearSyncReport, LinearSyncStatus
from db.models import LinearIssue
from linear_plan_support import target_view
from mcp_server.errors import MESSAGES
from mcp_server.registry import install
from mcp_support import ALICE, MCP_URL, build_app, call, code, mcp_env, sdk_session, structured, suite_token, text
from specflow_api_agent_support import ALPHA, FOREIGN
from test_api_linear_plan_routes import BEN, PID, bind, rig, settle
from test_mcp_spec_tools_over_api import Recorder

BEN_TOKEN = suite_token(BEN)
__all__ = ["rig"]  # the fixture of the route tests, used by name below
NOW = datetime(2026, 11, 25, tzinfo=UTC)
ITEMS = (  # node id, kind, identifier, removed, error code
    ("STORY::A", "story", "ENG-1", False, None),
    ("STORY::B", "story", "ENG-2", False, "linear_invalid"),
    ("TASK::A", "subtask", "ENG-3", False, None),
    ("TASK::B", "subtask", "ENG-4", True, None),
    ("TASK::C", "subtask", None, False, None),
)


def stack(rig: Any, valid_env: dict[str, str]) -> tuple[Any, Recorder]:
    """The MCP app whose API client is the recording transport over the API app of ``rig``."""

    recorder = Recorder(httpx.ASGITransport(app=rig.app, raise_app_exceptions=False))
    app = build_app(mcp_env(valid_env), transport=recorder, tools=lambda server: install(server, resource_url=MCP_URL))
    return app, recorder


def seed(rig: Any, *, enabled: bool = True) -> None:
    bind(rig)
    team = rig.team
    rig.db.targets[PID] = target_view(PID, team, rig.project.id, enabled=enabled, url=rig.project.url)
    rig.db.items[PID] = [
        LinearIssue(
            project_id=PID,
            node_id=node,
            kind=kind,
            identifier=identifier,
            issue_id=None if identifier is None else uuid.uuid4(),
            requested_at=NOW,
            removed_at=NOW if removed else None,
            error_code=error,
        )
        for node, kind, identifier, removed, error in ITEMS
    ]


async def test_sync_to_linear_posts_the_project_in_the_query_and_an_empty_body_and_answers_the_status(rig, valid_env):
    """A collaborator (any member) starts a sync; the answer is the API's ``LinearSyncStatus``; a repeat while the pass
    runs answers ``queued``."""

    seed(rig)
    rig.stub.gate = asyncio.Event()
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, BEN_TOKEN) as session:
        first = await call(session, "sync_to_linear", project_id=ALPHA)
        repeat = await call(session, "sync_to_linear", project_id=ALPHA)
    rig.stub.gate.set()
    await settle(rig)

    (sent, answer), (_, merged) = recorder.agent_calls()
    assert (sent.method, sent.url.path, dict(sent.url.params)) == (
        "POST",
        AGENT_ROUTES["sync_to_linear"],
        {"project_id": ALPHA},
    )
    assert json.loads(sent.content) == {}
    assert not first.is_error and answer.status_code == 200
    assert structured(first) == answer.json() and LinearSyncStatus.model_validate(structured(first))
    assert json.loads(text(first)) == structured(first)
    assert (structured(first)["status"], structured(first)["queued"], structured(first)["trigger"]) == (
        "running",
        False,
        "mcp",
    )
    assert (structured(first)["team_key"], structured(first)["linear_project"]) == ("ENG", "Shop")
    assert (structured(repeat)["queued"], merged.json()["queued"]) == (True, True)


async def test_linear_sync_status_sends_limit_and_cursor_and_pages_the_items(rig, valid_env):
    seed(rig)
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        first = await call(session, "linear_sync_status", project_id=ALPHA, limit=3)
        second = await call(
            session, "linear_sync_status", project_id=ALPHA, limit=3, cursor=structured(first)["next_cursor"]
        )

    (one, one_answer), (two, two_answer) = recorder.agent_calls()
    assert (one.method, one.url.path) == ("GET", AGENT_ROUTES["linear_sync_status"])
    assert dict(one.url.params) == {"project_id": ALPHA, "limit": "3"}
    assert dict(two.url.params) == {"project_id": ALPHA, "limit": "3", "cursor": structured(first)["next_cursor"]}
    assert structured(first) == one_answer.json() and structured(second) == two_answer.json()
    assert LinearSyncReport.model_validate(structured(first)) and json.loads(text(second)) == structured(second)
    items = structured(first)["items"] + structured(second)["items"]
    assert [i["node_id"] for i in items] == ["STORY::A", "STORY::B", "TASK::A", "TASK::B", "TASK::C"]
    assert structured(second)["next_cursor"] is None
    story, task = items[0], items[2]
    assert story["url"] == "https://linear.app/graphico-app/issue/ENG-1" and story["identifier"] == "ENG-1"
    assert (items[1]["error_code"], items[3]["removed"], items[4]["url"]) == ("linear_invalid", True, None)
    assert task["kind"] == "subtask"


async def test_a_missing_or_disabled_target_reaches_the_agent_as_linear_not_configured(rig, valid_env):
    app, recorder = stack(rig, valid_env)
    async with sdk_session(app, ALICE) as session:
        no_target = await call(session, "sync_to_linear", project_id=ALPHA)
        no_status = await call(session, "linear_sync_status", project_id=ALPHA)
        seed(rig, enabled=False)
        disabled = await call(session, "sync_to_linear", project_id=ALPHA)
        shown = await call(session, "linear_sync_status", project_id=ALPHA)

    assert [code(r) for r in (no_target, no_status, disabled)] == ["linear_not_configured"] * 3
    assert structured(no_target)["error"] == {
        "code": "linear_not_configured",
        "message": MESSAGES["linear_not_configured"],
    }
    assert [answer.status_code for _, answer in recorder.agent_calls()] == [409, 409, 409, 200]
    assert not shown.is_error and structured(shown)["enabled"] is False
    assert rig.passes == []


async def test_no_linear_on_the_server_reaches_the_agent_as_linear_unavailable(rig, valid_env):
    rig.runtime.linear_plan = None
    app, recorder = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        synced = await call(session, "sync_to_linear", project_id=ALPHA)
        read = await call(session, "linear_sync_status", project_id=ALPHA)

    assert [code(r) for r in (synced, read)] == ["linear_unavailable"] * 2
    assert structured(read)["error"]["message"] == MESSAGES["linear_unavailable"]
    assert [answer.status_code for _, answer in recorder.agent_calls()] == [503, 503]


async def test_a_foreign_project_and_a_forged_cursor_keep_their_codes(rig, valid_env):
    seed(rig)
    app, _ = stack(rig, valid_env)

    async with sdk_session(app, ALICE) as session:
        foreign = await call(session, "sync_to_linear", project_id=FOREIGN)
        forged = await call(session, "linear_sync_status", project_id=ALPHA, cursor="forged")

    assert (code(foreign), code(forged)) == ("project_not_found", "invalid_cursor")
