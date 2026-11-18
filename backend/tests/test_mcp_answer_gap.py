"""``answer_gap`` end to end (docs/mcp-server/apps.md "Terminal dialogs", decision D6): the SDK's client on protocol
2026-07-28 against the MCP app, whose API is the real app over the shop gap corpus after a gap run
(``gap_agent_support``).

The first call of a round trip answers ``input_required`` with the dialog; the test plays the person and calls again
with the dialog's answer and the sealed ``request_state``, as Claude Code does. Only the person's answer is written,
as the token's user, with ``via`` ``elicitation``; the agent sends the gap id only.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ElicitResult
from starlette.applications import Starlette

from gap_agent_support import ALPHA, QUESTION, GapRig, serve_gaps
from mcp_server.registry import NOT_SHOWN, RECORDED, install
from mcp_support import ALICE, MCP_HOST, MCP_URL, build_app, call, mcp_env, sdk_session
from specflow_api_agent_support import FOREIGN, agent_env

WEB = "https://app.example.test"


@dataclasses.dataclass
class Served:
    gaps: GapRig
    app: Starlette


@pytest.fixture
async def served(monkeypatch, valid_env) -> AsyncIterator[Served]:
    async with serve_gaps(monkeypatch, agent_env(valid_env, PUBLIC_URL=WEB)) as gaps:
        transport = httpx.ASGITransport(app=gaps.rig.app, raise_app_exceptions=False)

        def tools(server: MCPServer) -> None:
            install(server, resource_url=MCP_URL)

        yield Served(gaps, build_app(mcp_env(valid_env), transport=transport, tools=tools))


async def _no_standalone_request(*_: Any) -> ElicitResult:
    raise AssertionError("on 2026-07-28 the dialog comes as input_required, never as elicitation/create")


@asynccontextmanager
async def modern(app: Starlette, *, forms: bool = True) -> AsyncIterator[ClientSession]:
    """A client session on protocol 2026-07-28 (``server/discover``); ``forms`` declares form elicitation."""

    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=f"http://{MCP_HOST}",
        headers={"Authorization": f"Bearer {ALICE}"},
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        await stack.enter_async_context(http)
        read, write = await stack.enter_async_context(streamable_http_client(MCP_URL, http_client=http))
        callback = _no_standalone_request if forms else None
        session = await stack.enter_async_context(ClientSession(read, write, elicitation_callback=callback))
        await session.discover()
        yield session


@dataclasses.dataclass
class Asked:
    """Round 1: the one request of the dialog, its wire key and the sealed state."""

    key: str
    message: str
    schema: dict[str, Any]
    state: str | None


async def ask(session: ClientSession, gap_id: str, project_id: str = ALPHA) -> Asked:
    first = await session.call_tool(
        "answer_gap", {"project_id": project_id, "gap_id": gap_id}, allow_input_required=True
    )
    assert not isinstance(first, CallToolResult), first
    ((key, request),) = first.input_requests.items()
    params = request.params
    return Asked(key, params.message, params.requested_schema, first.request_state)


async def reply(session: ClientSession, gap_id: str, asked: Asked, response: ElicitResult) -> CallToolResult:
    return await session.call_tool(
        "answer_gap",
        {"project_id": ALPHA, "gap_id": gap_id},
        input_responses={asked.key: response},
        request_state=asked.state,
    )


def accept(**content: Any) -> ElicitResult:
    return ElicitResult(action="accept", content=content)


async def test_the_dialog_holds_the_question_and_one_line_per_option_and_records_the_ticked_one(served) -> None:
    gap_id = served.gaps.question_gap()
    whole = await served.gaps.gaps.get_gap(ALPHA, gap_id)
    options = sorted(whole.options, key=lambda option: option.rank)

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        fields = asked.schema["properties"]
        result = await reply(session, gap_id, asked, accept(option_a=True, option_b=False))

    assert asked.message.splitlines()[0] == QUESTION
    assert asked.message.splitlines()[2] == "The options' details and the quoted lines are in the chat above."
    assert list(fields) == [*(f"option_{chr(97 + index)}" for index in range(len(options))), "own_text"]
    ticked = [name for name, field in fields.items() if field.get("default") is True]
    recommended = next(index for index, option in enumerate(options) if option.recommended)
    assert ticked == ([f"option_{chr(97 + recommended)}"] if whole.severity == "major" else [])
    outcome = result.structured_content
    assert not result.is_error and outcome is not None
    assert (outcome["answered"], outcome["action"], outcome["note"]) == (True, "accept", RECORDED)
    assert outcome["choice"] == f"A) {options[0].label}" and outcome["web_url"].endswith(f"gaps?gap={gap_id}")
    gap = await served.gaps.gaps.get_gap(ALPHA, gap_id)
    assert gap.status == "answered" and gap.clarification is not None
    assert (gap.clarification.clr_id, gap.clarification.option_label) == (outcome["clr_id"], options[0].label)
    assert gap.history is not None
    assert [event.detail["via"] for event in gap.history.events if event.event == "answered"] == ["elicitation"]


async def test_an_own_answer_with_every_option_unticked_is_recorded_as_written(served) -> None:
    gap_id = served.gaps.question_gap()
    text = "Yes, until the parcel leaves the warehouse."

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        unticked = {name: False for name in asked.schema["properties"] if name.startswith("option_")}
        result = await reply(session, gap_id, asked, accept(**unticked, own_text=text))

    outcome = result.structured_content
    gap = await served.gaps.gaps.get_gap(ALPHA, gap_id)
    assert outcome is not None and (outcome["answered"], outcome["choice"], outcome["placement"]) == (
        True,
        "your own answer",
        "document",
    )
    assert gap.clarification is not None and gap.clarification.text == text


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_a_declined_or_cancelled_dialog_records_nothing_and_gives_the_web_link(served, action) -> None:
    gap_id = served.gaps.question_gap()

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        result = await reply(session, gap_id, asked, ElicitResult(action=action))

    outcome = result.structured_content
    assert outcome is not None and not result.is_error
    assert (outcome["answered"], outcome["action"], outcome["note"]) == (False, action, NOT_SHOWN)
    assert outcome["web_url"] == f"{WEB}/projects/{ALPHA}/gaps?gap={gap_id}"
    assert (await served.gaps.gaps.get_gap(ALPHA, gap_id)).status == "open"


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ({"option_a": True, "option_b": True}, "Tick exactly one option"),
        ({"option_a": False, "option_b": False}, "Tick exactly one option"),
        ({"option_a": True, "own_text": "Mine."}, "untick the options or clear the text"),
    ],
    ids=["two options", "nothing", "option and text"],
)
async def test_anything_but_one_answer_records_nothing_and_says_what_to_change(served, content, problem) -> None:
    gap_id = served.gaps.question_gap()

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        result = await reply(session, gap_id, asked, accept(**content))

    outcome = result.structured_content
    assert outcome is not None and outcome["answered"] is False and problem in outcome["note"]
    assert "Call answer_gap again" in outcome["note"]
    assert (await served.gaps.gaps.get_gap(ALPHA, gap_id)).status == "open"


async def test_a_client_on_an_older_protocol_gets_the_web_link_instead_of_a_dialog(served) -> None:
    gap_id = served.gaps.question_gap()

    async with sdk_session(served.app, ALICE) as session:
        result = await call(session, "answer_gap", project_id=ALPHA, gap_id=gap_id)

    outcome = result.structured_content
    assert outcome is not None and (outcome["answered"], outcome["action"]) == (False, "no_dialog")
    assert "the dialog needs 2026-07-28" in outcome["note"] and outcome["web_url"] is not None


async def test_a_client_without_forms_gets_the_web_link_instead_of_a_dialog(served) -> None:
    gap_id = served.gaps.question_gap()

    async with modern(served.app, forms=False) as session:
        result = await call(session, "answer_gap", project_id=ALPHA, gap_id=gap_id)

    outcome = result.structured_content
    assert outcome is not None and outcome["action"] == "no_dialog" and "shows no forms" in outcome["note"]


CHANGES = {
    "question": {"question": "Can a customer cancel an order that is paid and packed?"},
    "version only": {"updated_at": datetime(2026, 11, 18, 11, 0, tzinfo=UTC)},
}


@pytest.mark.parametrize("change", CHANGES.values(), ids=list(CHANGES))
async def test_a_gap_that_changed_between_the_rounds_is_asked_again(served, change) -> None:
    """The answer counts only for the gap the person saw: the dialog names the gap's version, and the SDK pins the
    answer to the dialog as shown, so any change of the gap (also one the dialog's text does not show) asks again."""

    gap_id = served.gaps.question_gap()

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        node = next(props for props in served.gaps.world.gap_nodes().values() if props.get("gap_id") == gap_id)
        node.update(change)
        again = await session.call_tool(
            "answer_gap",
            {"project_id": ALPHA, "gap_id": gap_id},
            input_responses={asked.key: accept(option_a=True, option_b=False)},
            request_state=asked.state,
            allow_input_required=True,
        )

    assert not isinstance(again, CallToolResult)
    ((_, request),) = again.input_requests.items()
    assert request.params.message != asked.message
    assert (await served.gaps.gaps.get_gap(ALPHA, gap_id)).status == "open"


async def test_only_an_open_gap_of_a_project_of_yours_opens_a_dialog(served) -> None:
    gap_id = served.gaps.question_gap()

    async with modern(served.app) as session:
        asked = await ask(session, gap_id)
        await reply(session, gap_id, asked, accept(option_a=True, option_b=False))
        answered = await call(session, "answer_gap", project_id=ALPHA, gap_id=gap_id)
        unknown = await call(session, "answer_gap", project_id=ALPHA, gap_id="GAP::000000000000")
        foreign = await call(session, "answer_gap", project_id=FOREIGN, gap_id=gap_id)

    errors = [result.structured_content for result in (answered, unknown, foreign)]
    assert all(result.is_error for result in (answered, unknown, foreign))
    assert [error["error"]["code"] for error in errors if error is not None] == [
        "gap_wrong_state",
        "gap_not_found",
        "project_not_found",
    ]


async def test_list_open_gaps_lists_only_the_callers_gaps_when_asked(served) -> None:
    """The flow that starts a dialog on "my gaps": ``assigned`` ``me`` reaches the API as the token's user."""

    gap_id = served.gaps.question_gap()
    alice = served.gaps.rig.alice_id
    await served.gaps.gaps.assign_gap(ALPHA, gap_id, alice, by=alice)

    async with sdk_session(served.app, ALICE) as session:
        mine = await call(session, "list_open_gaps", project_id=ALPHA, assigned="me")

    assert mine.structured_content is not None and not mine.is_error
    items = mine.structured_content["items"]
    assert [item["gap_id"] for item in items] == [gap_id]
    assert items[0]["assignee"]["you"] is True and "**Assigned to:** you" in items[0]["card"]
