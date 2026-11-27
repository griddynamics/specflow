"""The Dropbox connection routes over the REST app (docs/dropbox/oauth.md "Routes"): what W0 froze (auth, the
integration off, the return path rule, the closed error map) and the connect, callback and disconnect flow over a
scripted Dropbox and an in-memory connection store (as ``test_api_box_router.py``)."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from api.dropbox import ERRORS, dropbox_error, dropbox_unavailable
from dropbox.codes import LINK_CODES, DropboxCode, DropboxError
from dropbox.connections import DropboxConnections
from dropbox_support import (
    ACCOUNT_TEAM,
    CLIENT_CANARY,
    CODE_CANARY,
    EMAIL_CANARY,
    NAME_CANARY,
    FakeDropbox,
    MemoryStore,
    dropbox_settings,
)
from specflow_api_agent_support import agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

RETURN = "/projects/p/integrations?dropbox=1"
ALICE, BEN = "alice@example.com", "ben@example.com"
ARCHKIT = Path(__file__).resolve().parent.parent / "archkit.toml"
CONNECTION_CODES = (
    "dropbox_not_connected",
    "dropbox_reconnect_required",
    "dropbox_state_invalid",
    "dropbox_code_invalid",
    "dropbox_unreachable",
    "dropbox_auth_failed",
)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        served.runtime.dropbox = None
        yield SimpleNamespace(http=served.client, runtime=served.runtime)


async def test_every_route_needs_a_signed_in_user(rig) -> None:
    for method, path, body in (
        ("GET", "integrations/dropbox", None),
        ("POST", "integrations/dropbox/authorize", {"return_to": RETURN}),
        ("POST", "integrations/dropbox/callback", {"code": "c", "state": "s"}),
        ("DELETE", "integrations/dropbox", None),
        ("POST", "projects/p/sources/dropbox/check", {"link": "x"}),
        ("POST", "projects/p/sources/dropbox", {"link": "x"}),
    ):
        answer = await rig.http.request(method, f"/api/v1/{path}", json=body)
        assert answer.status_code == 401, path


async def test_with_the_integration_off_get_says_so_and_the_rest_is_503(rig) -> None:
    got = (await call(rig, "GET", "integrations/dropbox")).json()

    assert got == {
        "enabled": False,
        "connected": False,
        "status": None,
        "name": None,
        "team_name": None,
        "connected_at": None,
        "last_used_at": None,
    }
    for method, path, body in (
        ("POST", "integrations/dropbox/authorize", {"return_to": RETURN}),
        ("POST", "integrations/dropbox/callback", {"code": "c", "state": "s"}),
        ("DELETE", "integrations/dropbox", None),
    ):
        assert error_of(await call(rig, method, path, body=body)) == (503, "dropbox_unavailable"), path


@pytest.mark.parametrize("return_to", ["https://evil.com", "//evil.com", "/a/../b", "x"])
async def test_a_return_path_off_this_site_is_refused(rig, return_to: str) -> None:
    answer = await call(rig, "POST", "integrations/dropbox/authorize", body={"return_to": return_to})

    assert error_of(answer) == (422, "validation_failed")


def test_the_api_errors_are_the_link_and_connection_codes_and_match_the_archkit_registry() -> None:
    entries = tomllib.loads(ARCHKIT.read_text("utf-8"))["error_codes"]["codes"]
    registry = {entry["code"]: entry["http"] for entry in entries}
    answered = {code.value: status for code, (status, _message) in ERRORS.items()}

    assert set(answered) == {*LINK_CODES, *CONNECTION_CODES}
    for code, status in {**answered, "dropbox_unavailable": 503}.items():
        assert registry[code] == status, code


def test_an_inside_code_without_a_sentence_is_auth_failed_and_no_message_carries_dropbox_text() -> None:
    inside = dropbox_error(DropboxError(DropboxCode.NOT_FOUND))

    assert (inside.status, inside.code) == (502, "dropbox_auth_failed")
    assert dropbox_unavailable().status == 503
    for _status, message in ERRORS.values():
        assert "{" not in message and "http" not in message


@pytest_asyncio.fixture
async def live(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        dropbox, store = FakeDropbox(), MemoryStore()
        connections = DropboxConnections(dropbox_settings(), store=store.transaction, client=dropbox.client())
        served.runtime.dropbox = connections
        await served.repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        yield SimpleNamespace(http=served.client, runtime=served.runtime, dropbox=dropbox, store=store)
        served.runtime.dropbox = None
        await connections.aclose()


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connect(rig: Any, who: str = ALICE) -> httpx.Response:
    url = (await call(rig, "POST", "integrations/dropbox/authorize", who, {"return_to": RETURN})).json()["url"]
    return await call(rig, "POST", "integrations/dropbox/callback", who, {"code": CODE_CANARY, "state": state_of(url)})


async def test_connect_get_and_disconnect(live, caplog) -> None:
    before = (await call(live, "GET", "integrations/dropbox")).json()
    with caplog.at_level(logging.DEBUG):
        answer = await connect(live)

    assert before["enabled"] is True and before["connected"] is False and before["status"] is None
    assert answer.status_code == 200
    body = answer.json()
    assert body["return_to"] == RETURN
    assert (body["connection"]["connected"], body["connection"]["name"]) == (True, NAME_CANARY)
    now = (await call(live, "GET", "integrations/dropbox")).json()
    assert (now["status"], now["team_name"]) == ("active", None)
    assert "account_id" not in now and EMAIL_CANARY not in str(now) and "@" not in str(now)
    assert (await call(live, "GET", "integrations/dropbox", BEN)).json()["connected"] is False
    for canary in (CODE_CANARY, CLIENT_CANARY, "sl.access-1", "refresh-1", NAME_CANARY, EMAIL_CANARY):
        assert canary not in caplog.text

    assert (await call(live, "DELETE", "integrations/dropbox")).status_code == 204
    assert live.dropbox.revoked == ["sl.access-1"]
    assert (await call(live, "GET", "integrations/dropbox")).json()["connected"] is False
    assert (await call(live, "DELETE", "integrations/dropbox")).status_code == 204  # idempotent


async def test_a_business_member_shows_the_team_name(live) -> None:
    live.dropbox.codes[CODE_CANARY] = ACCOUNT_TEAM

    await connect(live)

    assert (await call(live, "GET", "integrations/dropbox")).json()["team_name"] == "Acme Team"


async def test_a_callback_with_a_foreign_state_or_a_bad_code_gets_a_closed_code(live, caplog) -> None:
    url = (await call(live, "POST", "integrations/dropbox/authorize", BEN, {"return_to": RETURN})).json()["url"]
    mine = (await call(live, "POST", "integrations/dropbox/authorize", body={"return_to": RETURN})).json()["url"]
    with caplog.at_level(logging.INFO):
        foreign = await call(live, "POST", "integrations/dropbox/callback", body={"code": "c", "state": state_of(url)})
        forged = await call(live, "POST", "integrations/dropbox/callback", body={"code": "c", "state": "forged"})
        bad = await call(
            live, "POST", "integrations/dropbox/callback", body={"code": "unknown", "state": state_of(mine)}
        )

    assert error_of(foreign) == error_of(forged) == (400, "dropbox_state_invalid")
    assert error_of(bad) == (400, "dropbox_code_invalid")
    assert "api.dropbox.refused" in caplog.text and "unknown" not in caplog.text
    assert state_of(mine) not in caplog.text


async def test_dropbox_down_during_the_callback_is_502(live) -> None:
    url = (await call(live, "POST", "integrations/dropbox/authorize", body={"return_to": RETURN})).json()["url"]
    live.dropbox.status = 503

    answer = await call(
        live, "POST", "integrations/dropbox/callback", body={"code": CODE_CANARY, "state": state_of(url)}
    )

    assert error_of(answer) == (502, "dropbox_unreachable")


async def test_the_authorize_url_asks_for_offline_access_and_the_read_scopes(live) -> None:
    url = (await call(live, "POST", "integrations/dropbox/authorize", body={"return_to": RETURN})).json()["url"]

    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    assert query["token_access_type"] == ["offline"] and query["client_id"]
    assert set(query["scope"][0].split()) == {
        "account_info.read",
        "files.metadata.read",
        "files.content.read",
        "sharing.read",
    }
