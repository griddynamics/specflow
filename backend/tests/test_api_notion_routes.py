"""The Notion connection routes over the REST app (docs/notion/oauth.md "Routes", docs/notion/codes.md): the caller's
connection on a scripted Notion token endpoint and an in-memory store; the codes a connection can answer come from a
patched ``connect``. Standings as in test_api_atlassian_routes: alice owns ``Alpha``, ben is its collaborator.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from api.notion import notion_error
from notion.connections import NotionConnections
from notion.reasons import NotionCode, NotionError
from specflow_api_agent_support import agent_env, serve_agent
from test_api_atlassian_routes import call, error_of
from test_notion_connections import (
    BOT_ID,
    CLIENT_CANARY,
    CODE_CANARY,
    RETURN,
    WORKSPACE_CANARY,
    WORKSPACE_ID,
    FakeNotion,
    MemoryStore,
    notion_settings,
    state_of,
)

ALICE, BEN = "alice@example.com", "ben@example.com"
BASE = "integrations/notion"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        notion, store = FakeNotion(), MemoryStore()
        connections = NotionConnections(notion_settings(), store=store.transaction, oauth=notion.oauth())
        served.runtime.notion = connections
        yield SimpleNamespace(http=served.client, runtime=served.runtime, notion=notion, store=store)
        served.runtime.notion = None
        await connections.aclose()


async def connect(rig: Any, who: str = ALICE) -> httpx.Response:
    url = (await call(rig, "POST", f"{BASE}/authorize", who, {"return_to": RETURN})).json()["url"]
    return await call(rig, "POST", f"{BASE}/callback", who, {"code": CODE_CANARY, "state": state_of(url)})


async def test_the_connect_flow_keeps_the_connection_and_answers_the_return_path(rig, caplog) -> None:
    before = (await call(rig, "GET", BASE)).json()
    with caplog.at_level(logging.DEBUG):
        answer = await connect(rig)

    assert before == {
        "enabled": True,
        "connected": False,
        "status": None,
        "workspace_name": None,
        "connected_at": None,
        "last_used_at": None,
    }
    assert answer.status_code == 200
    body = answer.json()
    assert body["return_to"] == RETURN
    assert (body["connection"]["connected"], body["connection"]["workspace_name"]) == (True, WORKSPACE_CANARY)
    now = (await call(rig, "GET", BASE)).json()
    assert (now["status"], now["workspace_name"]) == ("active", WORKSPACE_CANARY)
    assert (await call(rig, "GET", BASE, BEN)).json()["connected"] is False  # one connection per user
    for canary in (CODE_CANARY, CLIENT_CANARY, "access-1", "refresh-1", BOT_ID):
        assert canary not in caplog.text and canary not in answer.text


async def test_the_answers_never_carry_a_token_a_bot_or_a_workspace_id(rig) -> None:
    answer = await connect(rig)
    shown = (await call(rig, "GET", BASE)).text + answer.text

    for secret in ("access-1", "refresh-1", BOT_ID, WORKSPACE_ID, CLIENT_CANARY):
        assert secret not in shown


async def test_the_authorize_url_is_notions_consent_page_with_a_signed_state(rig) -> None:
    url = (await call(rig, "POST", f"{BASE}/authorize", body={"return_to": RETURN})).json()["url"]

    assert url.startswith("https://api.notion.com/v1/oauth/authorize?")
    assert "owner=user" in url and "response_type=code" in url and f"client_id={notion_settings().client_id}" in url
    assert rig.notion.requests == []  # building the link calls nobody


async def test_a_callback_with_a_bad_state_or_code_is_refused_with_a_closed_code(rig, caplog) -> None:
    url = (await call(rig, "POST", f"{BASE}/authorize", BEN, {"return_to": RETURN})).json()["url"]
    with caplog.at_level(logging.INFO):
        other = await call(rig, "POST", f"{BASE}/callback", body={"code": "c", "state": state_of(url)})
    own = (await call(rig, "POST", f"{BASE}/authorize", body={"return_to": RETURN})).json()["url"]
    bad_code = await call(rig, "POST", f"{BASE}/callback", body={"code": "unknown", "state": state_of(own)})

    assert error_of(other) == (400, "notion_state_invalid")
    assert error_of(bad_code) == (400, "notion_code_invalid")
    assert "api.notion.refused" in caplog.text and "unknown" not in caplog.text and state_of(own) not in caplog.text
    assert rig.store.rows == {}
    assert len(rig.notion.requests) == 1  # the forged state never reached Notion


async def test_notion_down_in_the_callback_is_502_unreachable(rig) -> None:
    url = (await call(rig, "POST", f"{BASE}/authorize", body={"return_to": RETURN})).json()["url"]
    rig.notion.status = 503

    answer = await call(rig, "POST", f"{BASE}/callback", body={"code": CODE_CANARY, "state": state_of(url)})

    assert error_of(answer) == (502, "notion_unreachable") and rig.store.rows == {}


@pytest.mark.parametrize("return_to", ["https://evil.com", "//evil.com", "/a/../b", "/a%2fb", "/a b", "x"])
async def test_a_return_path_off_this_site_is_refused(rig, return_to: str) -> None:
    assert error_of(await call(rig, "POST", f"{BASE}/authorize", body={"return_to": return_to})) == (
        422,
        "validation_failed",
    )


@pytest.mark.parametrize(
    "body", [{}, {"code": "c"}, {"state": "s"}, {"code": "", "state": "s"}, {"code": "c", "state": "s", "x": 1}]
)
async def test_a_callback_body_without_code_and_state_is_a_validation_failure(rig, body) -> None:
    assert error_of(await call(rig, "POST", f"{BASE}/callback", body=body)) == (422, "validation_failed")


async def test_disconnect_deletes_the_connection_and_revokes_the_grant_and_repeats_as_204(rig) -> None:
    await connect(rig)

    assert (await call(rig, "DELETE", BASE)).status_code == 204
    assert rig.notion.revoked == ["access-1"] and rig.store.rows == {}
    assert (await call(rig, "GET", BASE)).json()["connected"] is False
    assert (await call(rig, "DELETE", BASE)).status_code == 204


async def test_an_expired_connection_shows_as_expired_and_can_be_connected_again(rig) -> None:
    await connect(rig)
    user_id, row = next(iter(rig.store.rows.items()))
    await rig.store.expire(user_id, now=row.connected_at)

    assert (await call(rig, "GET", BASE)).json()["status"] == "expired"
    rig.notion.codes.add(CODE_CANARY)
    assert (await connect(rig)).status_code == 200
    assert (await call(rig, "GET", BASE)).json()["status"] == "active"


async def test_with_the_integration_off_the_routes_answer_503_and_the_connection_says_so(rig) -> None:
    rig.runtime.notion = None

    assert (await call(rig, "GET", BASE)).json() == {
        "enabled": False,
        "connected": False,
        "status": None,
        "workspace_name": None,
        "connected_at": None,
        "last_used_at": None,
    }
    for method, path, body in (
        ("POST", f"{BASE}/authorize", {"return_to": RETURN}),
        ("POST", f"{BASE}/callback", {"code": "c", "state": "s"}),
        ("DELETE", BASE, None),
    ):
        assert error_of(await call(rig, method, path, body=body)) == (503, "notion_unavailable"), path


async def test_the_routes_need_a_signed_in_user(rig) -> None:
    for method, path, body in (
        ("GET", BASE, None),
        ("POST", f"{BASE}/authorize", {"return_to": RETURN}),
        ("POST", f"{BASE}/callback", {"code": "c", "state": "s"}),
        ("DELETE", BASE, None),
    ):
        answer = await rig.http.request(method, f"/api/v1/{path}", json=body)
        assert answer.status_code == 401, path


# What a connection answers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (NotionCode.STATE_INVALID, (400, "notion_state_invalid")),
        (NotionCode.CODE_INVALID, (400, "notion_code_invalid")),
        (NotionCode.UNREACHABLE, (502, "notion_unreachable")),
        (NotionCode.AUTH_FAILED, (502, "notion_auth_failed")),
        (NotionCode.NOT_CONNECTED, (409, "notion_not_connected")),
        (NotionCode.RECONNECT_REQUIRED, (409, "notion_reconnect_required")),
        (NotionCode.GRANT_INVALID, (502, "notion_auth_failed")),  # an inside code never leaks: the default answer
        (NotionCode.REFUSED, (502, "notion_auth_failed")),
    ],
)
async def test_the_callback_answers_every_code_a_connection_can_raise_with_fixed_text(
    rig, monkeypatch, code, expected
) -> None:
    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise NotionError(code)

    monkeypatch.setattr(rig.runtime.notion, "connect", refuse)

    answer = await call(rig, "POST", f"{BASE}/callback", body={"code": "c", "state": "s"})

    assert error_of(answer) == expected
    assert notion_error(NotionError(code)).code == expected[1]
    assert "canary" not in answer.text
