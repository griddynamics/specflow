"""The ``/integrations/linear`` routes over the REST app: status, authorize and callback, on a scripted
Linear and an in-memory connection store. Without the Linear app configured they answer 503 ``linear_unavailable``."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from linear_oauth_support import CLIENT_CANARY, CODE_CANARY, NAME_CANARY, FakeLinear, MemoryStore, consent_of
from linear_oauth_support import make_connections as connections_over
from specflow_api_agent_support import agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import ALICE, BEN, call, error_of

RETURN = "/projects/p/integrations?app=linear"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        linear, store = FakeLinear(), MemoryStore()
        connections = connections_over(linear, store)
        served.runtime.linear = connections
        served.linear, served.store, served.http = linear, store, served.client
        yield served
        served.runtime.linear = None
        await connections.aclose()


async def connect(rig: Any, who: str = ALICE) -> httpx.Response:
    url = (await call(rig, "POST", "integrations/linear/authorize", who, {"return_to": RETURN})).json()["url"]
    query = consent_of(url)
    rig.linear.allow(CODE_CANARY, query["code_challenge"])
    return await call(rig, "POST", "integrations/linear/callback", who, {"code": CODE_CANARY, "state": query["state"]})


async def test_get_without_a_connection_says_none_and_available(rig) -> None:
    answer = await call(rig, "GET", "integrations/linear")

    assert answer.status_code == 200
    assert answer.json() == {"available": True, "status": "none", "user_name": None, "workspace": None}


async def test_authorize_answers_the_consent_url_with_pkce_and_a_same_site_return(rig, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        answer = await call(rig, "POST", "integrations/linear/authorize", ALICE, {"return_to": RETURN})

    query = consent_of(answer.json()["url"])
    assert answer.status_code == 200 and answer.json()["url"].startswith("https://linear.app/oauth/authorize?")
    assert (query["code_challenge_method"], query["actor"], query["scope"]) == ("S256", "user", "read,write")
    assert query["state"] not in log_haystack(caplog)


@pytest.mark.parametrize("return_to", ["https://evil.example/x", "//evil.example", "/a/../b", ""])
async def test_authorize_refuses_a_return_that_is_not_a_path_of_this_site(rig, return_to: str) -> None:
    answer = await call(rig, "POST", "integrations/linear/authorize", ALICE, {"return_to": return_to})

    assert answer.status_code == 422


async def test_the_callback_connects_the_caller_and_get_shows_the_workspace_without_a_token(rig, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        answer = await connect(rig)
    shown = await call(rig, "GET", "integrations/linear")

    workspace = {"id": "org-1", "name": "Acme Workspace", "url_key": "acme"}
    expected = {"available": True, "status": "active", "user_name": NAME_CANARY, "workspace": workspace}
    assert answer.status_code == 200 and answer.json() == {"connection": expected, "return_to": RETURN}
    assert shown.json() == expected
    for canary in ("access-1", "refresh-1", CLIENT_CANARY, CODE_CANARY):
        assert canary not in answer.text and canary not in shown.text and canary not in log_haystack(caplog)


async def test_a_connection_belongs_to_its_member(rig) -> None:
    await connect(rig)

    other = await call(rig, "GET", "integrations/linear", BEN)

    assert other.json()["status"] == "none"


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("other_user", 400, "linear_state_invalid"),
        ("used_code", 400, "linear_code_invalid"),
        ("down", 502, "linear_unreachable"),
    ],
)
async def test_the_callback_refuses_with_a_closed_code(rig, case: str, status: int, code: str, caplog) -> None:
    url = (await call(rig, "POST", "integrations/linear/authorize", ALICE, {"return_to": RETURN})).json()["url"]
    query = consent_of(url)
    if case != "used_code":
        rig.linear.allow(CODE_CANARY, query["code_challenge"])
    rig.linear.status = 503 if case == "down" else None
    who = BEN if case == "other_user" else ALICE

    with caplog.at_level(logging.DEBUG):
        answer = await call(
            rig, "POST", "integrations/linear/callback", who, {"code": CODE_CANARY, "state": query["state"]}
        )

    assert error_of(answer) == (status, code)
    assert query["state"] not in answer.text and CODE_CANARY not in answer.text
    assert query["state"] not in log_haystack(caplog) and CODE_CANARY not in log_haystack(caplog)
    assert (await call(rig, "GET", "integrations/linear", who)).json()["status"] == "none"


async def test_a_callback_body_with_an_unknown_field_is_refused(rig) -> None:
    answer = await call(rig, "POST", "integrations/linear/callback", ALICE, {"code": "c", "state": "s", "x": 1})

    assert answer.status_code == 422


async def test_get_shows_a_connection_the_workspace_revoked_as_revoked(rig) -> None:
    await connect(rig)
    for user_id, row in list(rig.store.rows.items()):
        rig.store.rows[user_id] = replace(row, status="revoked")

    shown = await call(rig, "GET", "integrations/linear")

    assert shown.status_code == 200 and shown.json()["status"] == "revoked"


async def test_the_routes_answer_503_when_linear_is_not_configured(rig) -> None:
    rig.runtime.linear = None

    answers = [
        await call(rig, "GET", "integrations/linear"),
        await call(rig, "POST", "integrations/linear/authorize", ALICE, {"return_to": RETURN}),
        await call(rig, "POST", "integrations/linear/callback", ALICE, {"code": "c", "state": "s"}),
    ]

    assert answers[0].json() == {"available": False, "status": "none", "user_name": None, "workspace": None}
    assert [error_of(a) for a in answers[1:]] == [(503, "linear_unavailable")] * 2
