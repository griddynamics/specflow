"""The Box connection routes over the REST app (docs/box/oauth.md "Routes"): auth, the closed codes and the log
content, on a scripted Box and an in-memory connection store. Standings as in test_api_github_routes."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from box.connections import BoxConnections
from box_support import CLIENT_CANARY, CODE_CANARY, NAME_CANARY, FakeBox, MemoryStore, box_settings
from specflow_api_agent_support import agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN = "alice@example.com", "ben@example.com"
RETURN = "/projects/p/settings?box=1"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        box, store = FakeBox(), MemoryStore()
        connections = BoxConnections(box_settings(), store=store.transaction, client=box.client())
        served.runtime.box = connections
        await served.repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        yield SimpleNamespace(http=served.client, runtime=served.runtime, box=box, store=store)
        served.runtime.box = None
        await connections.aclose()


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connect(rig: Any, who: str = ALICE) -> httpx.Response:
    url = (await call(rig, "POST", "integrations/box/authorize", who, {"return_to": RETURN})).json()["url"]
    return await call(rig, "POST", "integrations/box/callback", who, {"code": CODE_CANARY, "state": state_of(url)})


async def test_every_route_needs_a_signed_in_user(rig) -> None:
    for method, path, body in (
        ("GET", "integrations/box", None),
        ("POST", "integrations/box/authorize", {"return_to": RETURN}),
        ("POST", "integrations/box/callback", {"code": "c", "state": "s"}),
    ):
        answer = await rig.http.request(method, f"/api/v1/{path}", json=body)
        assert answer.status_code == 401, path


async def test_connect_and_get_show_the_connection(rig, caplog) -> None:
    before = (await call(rig, "GET", "integrations/box")).json()
    with caplog.at_level(logging.DEBUG):
        answer = await connect(rig)

    assert before["enabled"] is True and before["connected"] is False and before["status"] is None
    assert answer.status_code == 200
    body = answer.json()
    assert body["return_to"] == RETURN
    assert (body["connection"]["connected"], body["connection"]["name"]) == (True, NAME_CANARY)
    now = (await call(rig, "GET", "integrations/box")).json()
    assert (now["status"], now["enterprise_name"]) == ("active", "Acme Corp")
    assert "login" not in str(now) and "@" not in str(now)
    assert (await call(rig, "GET", "integrations/box", BEN)).json()["connected"] is False
    for canary in (CODE_CANARY, CLIENT_CANARY, "access-1", "refresh-1", NAME_CANARY):
        assert canary not in caplog.text


async def test_a_callback_with_a_foreign_state_or_a_bad_code_gets_a_closed_code(rig, caplog) -> None:
    url = (await call(rig, "POST", "integrations/box/authorize", BEN, {"return_to": RETURN})).json()["url"]
    mine = (await call(rig, "POST", "integrations/box/authorize", body={"return_to": RETURN})).json()["url"]
    with caplog.at_level(logging.INFO):
        foreign = await call(rig, "POST", "integrations/box/callback", body={"code": "c", "state": state_of(url)})
        forged = await call(rig, "POST", "integrations/box/callback", body={"code": "c", "state": "forged"})
        bad = await call(rig, "POST", "integrations/box/callback", body={"code": "unknown", "state": state_of(mine)})

    assert error_of(foreign) == error_of(forged) == (400, "box_state_invalid")
    assert error_of(bad) == (400, "box_code_invalid")
    assert "api.box.refused" in caplog.text and "unknown" not in caplog.text and state_of(mine) not in caplog.text


@pytest.mark.parametrize("return_to", ["https://evil.com", "//evil.com", "/a/../b", "x"])
async def test_a_return_path_off_this_site_is_refused(rig, return_to: str) -> None:
    answer = await call(rig, "POST", "integrations/box/authorize", body={"return_to": return_to})

    assert error_of(answer) == (422, "validation_failed")


async def test_box_down_during_the_callback_is_502(rig) -> None:
    url = (await call(rig, "POST", "integrations/box/authorize", body={"return_to": RETURN})).json()["url"]
    rig.box.status = 503

    answer = await call(rig, "POST", "integrations/box/callback", body={"code": CODE_CANARY, "state": state_of(url)})

    assert error_of(answer) == (502, "box_unreachable")


async def test_the_authorize_url_asks_for_the_one_scope(rig) -> None:
    url = (await call(rig, "POST", "integrations/box/authorize", body={"return_to": RETURN})).json()["url"]

    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://account.box.com/api/oauth2/authorize?")
    assert query["scope"] == ["root_readwrite"] and query["client_id"]


async def test_with_the_integration_off_get_says_so_and_the_rest_is_503(rig) -> None:
    rig.runtime.box = None

    got = (await call(rig, "GET", "integrations/box")).json()
    assert got["enabled"] is False and got["connected"] is False
    for method, path, body in (
        ("POST", "integrations/box/authorize", {"return_to": RETURN}),
        ("POST", "integrations/box/callback", {"code": "c", "state": "s"}),
    ):
        assert error_of(await call(rig, method, path, body=body)) == (503, "box_unavailable"), path
