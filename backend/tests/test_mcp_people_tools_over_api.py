"""The people tools (``list_areas``, ``list_stories``, ``preview_story``, ``claim_story``, ``release_story``) through
the MCP router over the REAL API app, in process (the pattern of ``test_mcp_spec_tools_over_api.py``): the story plan of
``people_support`` behind the API, a recording transport between the two services, the MCP SDK client in front.

Covers the Markdown text (equal to the render of the structured content), the write calls (``POST``, ``project_id`` in
the query, the other arguments as the JSON body), the typed error details end to end, the audit (ids and codes only,
never a claim token, label, branch, note or name) and the input schemas the SDK enforces before any call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES, AreaList, ErrorDetails, StoryList, StoryPreview
from mcp_server import tables
from mcp_server.errors import MESSAGES
from mcp_server.registry import install
from mcp_support import ALICE, MCP_URL, build_app, call, mcp_env, sdk_session, structured, text
from people_support import API, LATE, UI, PeopleRig, X, serve_people
from specflow_api_agent_support import ALPHA, FOREIGN, agent_env
from test_mcp_spec_tools_over_api import Recorder

READS = {
    "list_areas": ({"project_id": ALPHA}, tables.render_areas, AreaList),
    "list_stories": ({"project_id": ALPHA, "limit": 3}, tables.render_stories, StoryList),
    "preview_story": ({"project_id": ALPHA, "story_id": API}, tables.render_preview, StoryPreview),
}


@dataclass
class Stack:
    people: PeopleRig
    api: Recorder
    env: dict[str, str]

    def app(self) -> Any:
        return build_app(self.env, transport=self.api, tools=lambda server: install(server, resource_url=MCP_URL))


@asynccontextmanager
async def over_people(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> AsyncIterator[Stack]:
    async with serve_people(monkeypatch, agent_env(valid_env)) as people:
        recorder = Recorder(httpx.ASGITransport(app=people.rig.app, raise_app_exceptions=False))
        yield Stack(people, recorder, mcp_env(valid_env))


@pytest.mark.parametrize("tool", READS)
async def test_a_read_tool_returns_the_structured_content_of_its_route_and_the_table_as_its_only_text(
    monkeypatch, valid_env, tool
):
    arguments, render, model = READS[tool]
    async with over_people(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, tool, **arguments)
        (request, answer), *_ = stack.api.agent_calls()

    assert not result.is_error and answer.status_code == 200
    assert (request.method, request.url.path) == ("GET", AGENT_ROUTES[tool])
    assert structured(result) == answer.json()
    assert len(result.content) == 1 and text(result) == render(model.model_validate(structured(result)))
    assert text(result).startswith("| ") and not text(result).startswith("{")  # a table, not JSON


async def test_the_stories_text_ends_with_the_next_cursor_line_when_more_exist(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        first = await call(session, "list_stories", project_id=ALPHA, limit=3)
        last = await call(session, "list_stories", project_id=ALPHA, limit=3, cursor=structured(first)["next_cursor"])
    assert text(first).splitlines()[-1] == f"3 of 6 · next_cursor {structured(first)['next_cursor']}"
    assert structured(last)["next_cursor"] is None and "next_cursor" not in text(last)


async def test_a_title_with_a_pipe_and_a_line_break_is_one_escaped_cell(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack:
        stack.people.memory.set_story(API, title="Pay | later\nnow")
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, "list_stories", project_id=ALPHA)
    row = next(line for line in text(result).splitlines() if API in line)
    assert "Pay \\| later now" in row and structured(result)["items"][0]["title"] == "Pay | later\nnow"
    assert row.replace("\\|", "").count("|") == len(text(result).splitlines()[0].split("|")) - 1


async def test_claim_posts_the_project_in_the_query_and_the_other_arguments_as_the_json_body(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            claimed = await call(
                session, "claim_story", project_id=ALPHA, story_id=API, agent_label="Claude Code", branch="fix/api"
            )
            bare = await call(session, "claim_story", project_id=ALPHA, story_id=X)
        (first, answer), (second, _) = stack.api.agent_calls()

    assert (first.method, first.url.path, dict(first.url.params)) == (
        "POST",
        AGENT_ROUTES["claim_story"],
        {"project_id": ALPHA},
    )
    assert json.loads(first.content) == {"story_id": API, "agent_label": "Claude Code", "branch": "fix/api"}
    assert json.loads(second.content) == {"story_id": X}  # None is left out, project_id is not in the body
    assert not claimed.is_error and structured(claimed) == answer.json() and bare.is_error is False
    assert json.loads(text(claimed)) == structured(claimed)  # a write keeps the JSON text
    assert structured(claimed)["claim_token"] == stack.people.memory.stories[API]["claim_token"]


async def test_release_posts_the_token_note_and_branch_and_answers_released(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        token = structured(await call(session, "claim_story", project_id=ALPHA, story_id=API))["claim_token"]
        released = await call(
            session, "release_story", project_id=ALPHA, story_id=API, claim_token=token, note="stop", branch="b"
        )
        again = await call(session, "release_story", project_id=ALPHA, story_id=API, claim_token=token)
        (_, _), (request, _), _ = stack.api.agent_calls()

    assert request.method == "POST" and json.loads(request.content) == {
        "story_id": API,
        "claim_token": token,
        "note": "stop",
        "branch": "b",
    }
    assert structured(released)["released"] == ["TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"]
    assert structured(again)["released"] == []  # the repeat


async def test_story_claimed_carries_the_typed_details_and_the_fixed_message(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack:
        await stack.people.hold(API, stack.people.ben, "Claude Code · laptop")
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, "claim_story", project_id=ALPHA, story_id=API)

    error = structured(result)["error"]
    assert result.is_error and (error["code"], error["message"]) == ("story_claimed", MESSAGES["story_claimed"])
    assert set(error["details"]) == {"holder", "since", "alternatives"}
    assert error["details"]["holder"] == {"name": "Ben", "you": False, "agent_label": "Claude Code · laptop"}
    assert [a["story_id"] for a in error["details"]["alternatives"]] == [X, "STORY::A::Y"]
    assert json.loads(text(result)) == structured(result)
    assert ErrorDetails.model_validate(error["details"])  # the contract model accepts what the tool answers


async def test_not_available_and_invalid_arguments_carry_state_blockers_and_the_argument(monkeypatch, valid_env):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        waiting = await call(session, "claim_story", project_id=ALPHA, story_id=LATE)
        done = await call(session, "claim_story", project_id=ALPHA, story_id="STORY::P::SETUP")
        bad = await call(session, "claim_story", project_id=ALPHA, story_id=API, agent_label="a\nb")
    assert structured(waiting)["error"]["details"]["state"] == "waiting"
    assert [b["story_id"] for b in structured(waiting)["error"]["details"]["blockers"]] == [API, UI]
    assert structured(done)["error"]["details"] == {"state": "completed", "blockers": []}
    assert structured(bad)["error"] == {
        "code": "invalid_arguments",
        "message": MESSAGES["invalid_arguments"],
        "details": {"argument": "agent_label"},
    }


async def test_an_error_without_details_has_no_details_key_and_details_that_do_not_validate_are_internal_error(
    monkeypatch, valid_env
):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        plain = await call(session, "preview_story", project_id=ALPHA, story_id="STORY::NOPE")
        foreign = await call(session, "list_areas", project_id=FOREIGN)
    assert structured(plain) == {"error": {"code": "story_not_found", "message": MESSAGES["story_not_found"]}}
    assert structured(foreign)["error"]["code"] == "project_not_found" and "details" not in structured(foreign)["error"]

    def api(request: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "story_claimed", "message": "x", "details": {"holder": {"name": 1}, "since": "x"}}}
        return httpx.Response(409, json=body, headers={"X-SpecFlow-User-Id": "11111111-1111-4111-8111-111111111111"})

    app = build_app(
        mcp_env(valid_env),
        transport=httpx.MockTransport(api),
        tools=lambda server: install(server, resource_url=MCP_URL),
    )
    async with sdk_session(app, ALICE) as session:
        invalid = await call(session, "claim_story", project_id=ALPHA, story_id=API)
    assert structured(invalid) == {"error": {"code": "internal_error", "message": MESSAGES["internal_error"]}}


async def test_the_audit_holds_ids_and_codes_only_never_a_token_label_branch_note_or_name(
    monkeypatch, valid_env, central_log
):
    central_log.configure()
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        claimed = await call(
            session, "claim_story", project_id=ALPHA, story_id=API, agent_label="SecretLabel", branch="secret/branch"
        )
        token = structured(claimed)["claim_token"]
        await call(session, "release_story", project_id=ALPHA, story_id=API, claim_token=token, note="SecretNote")
        await call(session, "claim_story", project_id=ALPHA, story_id="STORY::NOPE")
        await call(session, "list_stories", project_id=ALPHA)
    events = central_log.events("mcp.tool.completed")
    assert [(e["tool"], e["outcome"]) for e in events] == [
        ("claim_story", "ok"),
        ("release_story", "ok"),
        ("claim_story", "story_not_found"),
        ("list_stories", "ok"),
    ]
    assert events[3]["item_count"] == 6 and all(e["project_id"] == ALPHA and e["user_id"] for e in events)
    written = central_log.events("api.agent.written")
    assert [(e["route"], e["outcome"], e["repeat"]) for e in written] == [
        ("story-claim", "ok", False),
        ("story-release", "ok", False),
        ("story-claim", "story_not_found", False),
    ]
    everything = json.dumps(central_log.records(), default=str)
    for private in (token, "SecretLabel", "secret/branch", "SecretNote", "Alice", "Ben", "currencies"):
        assert private not in everything, private


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_stories", {"project_id": ALPHA, "limit": 0}),
        ("list_stories", {"project_id": ALPHA, "limit": 101}),
        ("list_stories", {"project_id": ALPHA, "view": "all"}),
        ("list_stories", {"project_id": ALPHA, "area_id": ""}),
        ("preview_story", {"project_id": ALPHA}),
        ("preview_story", {"project_id": ALPHA, "story_id": ""}),
        ("claim_story", {"project_id": ALPHA, "story_id": "S" * 201}),
        ("claim_story", {"project_id": ALPHA}),
        ("release_story", {"project_id": ALPHA, "story_id": API}),
        ("release_story", {"project_id": ALPHA, "story_id": API, "claim_token": ""}),
        ("release_story", {"project_id": ALPHA, "story_id": API, "claim_token": "t", "note": "n" * 501}),
    ],
)
async def test_arguments_outside_the_input_schema_are_refused_before_any_api_call(
    monkeypatch, valid_env, tool, arguments
):
    async with over_people(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        result = await call(session, tool, **arguments)
    assert result.is_error
    assert stack.api.agent_calls() == []
    assert result.structured_content is None  # the SDK's own refusal, not a tool error with a code
