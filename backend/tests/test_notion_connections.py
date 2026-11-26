"""The Notion connections (docs/notion/oauth.md "Connect flow", "Tokens"): the OAuth client against a scripted Notion
token endpoint (``httpx.MockTransport``), ``NotionConnections`` over an in-memory store, and the refreshing gateway
wrapper over a fake inner gateway. No token, code, ``state`` or workspace name may reach a log line or an error."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from integrations.state import STATE_TTL_SECONDS, StateInvalidError, StateSigner
from notion.connections import (
    NO_EXPIRY_SECONDS,
    NotionConnections,
    NotionOAuthClient,
    RefreshingNotionGateway,
    StoredConnection,
    TokenGrant,
)
from notion.gateway import NOTION_VERSION, PAGE_SIZE, NotionGateway
from notion.limiter import RateLimiter, WaitBudget
from notion.reasons import NotionCode, NotionError
from settings import NotionSettings

CLIENT_ID = "notion-client-1"
CLIENT_CANARY = "notion-client-secret-canary-91a"
TOKEN_KEY = Fernet.generate_key().decode()
CODE_CANARY = "notion-code-canary-3b7"
WORKSPACE_CANARY = "Workspace Canary Inc"
WORKSPACE_ID = "ws-canary-1"
BOT_ID = "bot-canary-1"
CALLBACK = "http://localhost:3001/api/integrations/notion/callback"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
RETURN = "/projects/p/settings?tab=notion"


def notion_settings() -> NotionSettings:
    return NotionSettings(
        NOTION_OAUTH_CLIENT_ID=CLIENT_ID,
        NOTION_OAUTH_CLIENT_SECRET=CLIENT_CANARY,
        NOTION_TOKEN_KEY=TOKEN_KEY,
        PUBLIC_URL="http://localhost:3001",
    )


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeNotion:
    """A scripted Notion token endpoint. Tokens are ``access-N`` / ``refresh-N``; a refresh rotates the refresh token
    (the old one dies). Notion answers a refusal with 400 ``{"error": "invalid_grant"}`` and a wrong client with 401."""

    def __init__(self) -> None:
        self.codes: set[str] = {CODE_CANARY}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.revoked: list[str] = []
        self.refreshes = 0
        self.expires_in: int | None = 3600  # None: the answer carries no expires_in
        self.with_refresh = True
        self.with_identity = True
        self.status: int | None = None  # every call answers this status (503: Notion down)
        self.error: tuple[int, str] | None = None  # the next token call answers this refusal
        self.gate: asyncio.Event | None = None  # a refresh waits for it
        self.entered = asyncio.Event()
        self._serial = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def oauth(self) -> NotionOAuthClient:
        return NotionOAuthClient(CLIENT_ID, notion_settings().client_secret, CALLBACK, transport=self.transport())

    def _issue(self) -> dict[str, Any]:
        self._serial += 1
        access, refresh = f"access-{self._serial}", f"refresh-{self._serial}"
        self.access.add(access)
        answer: dict[str, Any] = {
            "access_token": access,
            "token_type": "bearer",
            "owner": {"type": "user", "user": {"id": "u-1", "person": {"email": "who@example.com"}}},
            "duplicated_template_id": None,
        }
        if self.with_identity:
            answer |= {"bot_id": BOT_ID, "workspace_id": WORKSPACE_ID, "workspace_name": WORKSPACE_CANARY}
        if self.with_refresh:
            self.refresh.add(refresh)
            answer["refresh_token"] = refresh
        if self.expires_in is not None:
            answer["expires_in"] = self.expires_in
        return answer

    def expire_access(self) -> None:
        """Notion revokes every access token early (a 401 next)."""

        self.access.clear()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        self.bodies.append(body)
        if request.url.path == "/v1/oauth/revoke":
            self.revoked.append(body["token"])
            return httpx.Response(200, json={})
        if request.url.path != "/v1/oauth/token":
            return httpx.Response(404)
        return await self._token(request, body)

    async def _token(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        wanted = base64.b64encode(f"{CLIENT_ID}:{CLIENT_CANARY}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {wanted}":
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "text canary"})
        if self.error is not None:
            (status, error), self.error = self.error, None
            return httpx.Response(status, json={"error": error, "error_description": "text canary from notion"})
        if body.get("grant_type") == "refresh_token":
            return await self._refresh(body)
        return self._exchange(body)

    async def _refresh(self, body: dict[str, Any]) -> httpx.Response:
        if self.gate is not None:
            self.entered.set()
            await self.gate.wait()
        self.refreshes += 1
        if body.get("refresh_token") not in self.refresh:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refresh.discard(body["refresh_token"])
        return httpx.Response(200, json=self._issue())

    def _exchange(self, body: dict[str, Any]) -> httpx.Response:
        if body.get("grant_type") != "authorization_code" or body.get("redirect_uri") != CALLBACK:
            return httpx.Response(400, json={"error": "invalid_request"})
        if body.get("code") not in self.codes:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.codes.discard(body["code"])
        return httpx.Response(200, json=self._issue())


class MemoryStore:
    """The ``ConnectionStore`` over a dict; ``transaction()`` keeps nothing of a failed block. ``events`` records every
    write and each commit or rollback."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, StoredConnection] = {}
        self.refresh_expires: dict[uuid.UUID, datetime | None] = {}
        self.expired_at: dict[uuid.UUID, datetime] = {}
        self.events: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryStore]:
        committed = self.rows.copy()
        try:
            yield self
        except BaseException:
            self.rows = committed
            self._note("rollback")
            raise
        self._note("commit")

    def _note(self, event: str, subject: Any = None) -> None:
        self.events.append((event, subject))

    def _change(self, event: str, user_id: uuid.UUID, **fields: Any) -> None:
        self.rows[user_id] = replace(self.rows[user_id], **fields)
        self._note(event, user_id)

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        return self.rows.get(user_id)

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        self._note("lock", user_id)
        return self.rows.get(user_id)

    async def save(
        self,
        user_id: uuid.UUID,
        grant: TokenGrant,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
        access_token: bytes | None = None,
    ) -> None:
        workspace = (grant.workspace_id, grant.workspace_name, grant.bot_id)
        self.rows[user_id] = StoredConnection(user_id, *workspace, refresh_token, "active", now, now, access_token)
        self.refresh_expires[user_id] = refresh_expires_at
        self._note("save", user_id)

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None:
        self.refresh_expires[user_id] = refresh_expires_at
        self._change("rotate", user_id, refresh_token=refresh_token, last_used_at=now)

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        self.expired_at[user_id] = now
        self._change("expire", user_id, status="expired")

    async def delete(self, user_id: uuid.UUID) -> bool:
        self._note("delete", user_id)
        return self.rows.pop(user_id, None) is not None


def make(
    notion: FakeNotion, store: MemoryStore, clock: Clock | None = None, *, now: datetime = T0
) -> NotionConnections:
    clock = clock or Clock()
    return NotionConnections(
        notion_settings(), store=store.transaction, oauth=notion.oauth(), now=lambda: now, monotonic=clock, clock=clock
    )


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connected(notion: FakeNotion, store: MemoryStore, clock: Clock | None = None) -> NotionConnections:
    connections = make(notion, store, clock)
    notion.codes.add(CODE_CANARY)
    await connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))
    return connections


# The client ----------------------------------------------------------------------------------------------------------


def test_the_consent_url_asks_for_the_user_owner_the_callback_and_the_state() -> None:
    query = parse_qs(urlsplit(FakeNotion().oauth().authorize_url("s-1")).query)

    assert urlsplit(FakeNotion().oauth().authorize_url("s-1")).path == "/v1/oauth/authorize"
    assert query == {
        "client_id": [CLIENT_ID],
        "response_type": ["code"],
        "owner": ["user"],
        "redirect_uri": [CALLBACK],
        "state": ["s-1"],
    }


async def test_a_code_is_exchanged_with_basic_auth_the_redirect_uri_and_the_api_version() -> None:
    notion = FakeNotion()

    grant = await notion.oauth().exchange(CODE_CANARY)

    request = notion.requests[0]
    assert (request.method, str(request.url)) == ("POST", "https://api.notion.com/v1/oauth/token")
    assert (
        request.headers["Authorization"]
        == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_CANARY}".encode()).decode()
    )
    assert request.headers["Notion-Version"] == NOTION_VERSION
    assert notion.bodies == [{"grant_type": "authorization_code", "code": CODE_CANARY, "redirect_uri": CALLBACK}]
    assert (grant.access_token.get_secret_value(), grant.refresh_token.get_secret_value()) == ("access-1", "refresh-1")
    assert (grant.expires_in, grant.bot_id, grant.workspace_id, grant.workspace_name) == (
        3600,
        BOT_ID,
        WORKSPACE_ID,
        WORKSPACE_CANARY,
    )
    assert "access-1" not in repr(grant) and "refresh-1" not in repr(grant)  # SecretStr


@pytest.mark.parametrize("scenario", ["unknown_code", "wrong_client", "no_identity", "other_refusal", "not_json"])
async def test_any_refused_exchange_is_code_invalid(scenario: str) -> None:
    notion = FakeNotion()
    code = CODE_CANARY
    if scenario == "unknown_code":
        code = "unknown"
    elif scenario == "wrong_client":
        notion.error = (401, "invalid_client")
    elif scenario == "no_identity":
        notion.with_identity = False
    elif scenario == "other_refusal":
        notion.error = (403, "restricted_resource")
    client = notion.oauth()
    if scenario == "not_json":
        html = httpx.MockTransport(lambda _request: httpx.Response(400, text="<html>nope</html>"))
        client = NotionOAuthClient(CLIENT_ID, SecretStr(CLIENT_CANARY), CALLBACK, transport=html)

    with pytest.raises(NotionError) as raised:
        await client.exchange(code)

    assert raised.value.code is NotionCode.CODE_INVALID
    assert "canary" not in str(raised.value)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_an_outage_or_a_limit_is_unreachable_and_so_is_a_transport_error(status: int) -> None:
    notion = FakeNotion()
    notion.status = status

    with pytest.raises(NotionError) as raised:
        await notion.oauth().exchange(CODE_CANARY)
    assert raised.value.code is NotionCode.UNREACHABLE
    with pytest.raises(NotionError) as refreshed:
        await notion.oauth().refresh("refresh-x")
    assert refreshed.value.code is NotionCode.UNREACHABLE

    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom secret canary")

    client = NotionOAuthClient(CLIENT_ID, SecretStr(CLIENT_CANARY), CALLBACK, transport=httpx.MockTransport(broken))
    with pytest.raises(NotionError) as down:
        await client.exchange(CODE_CANARY)
    assert down.value.code is NotionCode.UNREACHABLE and "canary" not in str(down.value)
    assert await client.revoke(SecretStr("access-1")) is False


async def test_a_refresh_names_a_dead_token_a_refusal_and_may_leave_out_the_identity_and_expiry() -> None:
    notion = FakeNotion()
    client = notion.oauth()
    grant = await client.exchange(CODE_CANARY)

    notion.with_identity, notion.expires_in = False, None
    rotated = await client.refresh(grant.refresh_token.get_secret_value())
    assert notion.bodies[-1] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert (rotated.refresh_token.get_secret_value(), rotated.expires_in, rotated.workspace_id) == (
        "refresh-2",
        NO_EXPIRY_SECONDS,
        "",
    )
    with pytest.raises(NotionError) as dead:
        await client.refresh("refresh-1")  # rotated away
    assert dead.value.code is NotionCode.GRANT_INVALID
    notion.error = (401, "invalid_client")
    with pytest.raises(NotionError) as refused:
        await client.refresh("refresh-2")
    assert refused.value.code is NotionCode.REFUSED
    notion.expires_in, notion.with_refresh = 0, False  # an explicit 0 is use once; no refresh token is accepted
    last = await client.refresh("refresh-2")
    assert (last.expires_in, last.refresh_token.get_secret_value()) == (0, "")


async def test_revoke_is_best_effort() -> None:
    notion = FakeNotion()
    client = notion.oauth()

    assert await client.revoke(SecretStr("access-1")) is True
    assert notion.requests[0].url.path == "/v1/oauth/revoke" and notion.revoked == ["access-1"]
    notion.status = 503
    assert await client.revoke(SecretStr("access-1")) is False


def test_nothing_secret_is_in_a_repr() -> None:
    connections = make(FakeNotion(), MemoryStore())

    text = repr(connections._oauth) + repr(connections._cipher) + repr(connections._states) + repr(connections._tokens)
    assert CLIENT_CANARY not in text and TOKEN_KEY not in text


# The connect flow ----------------------------------------------------------------------------------------------------


async def test_connect_checks_the_state_stores_the_encrypted_refresh_token_and_logs_ids_only(caplog) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = make(notion, store)
    state = state_of(connections.authorize_url(USER, RETURN))

    with caplog.at_level(logging.DEBUG):
        view, return_to = await connections.connect(USER, code=CODE_CANARY, state=state)

    assert (view.status, view.workspace_id, view.workspace_name, return_to) == (
        "active",
        WORKSPACE_ID,
        WORKSPACE_CANARY,
        RETURN,
    )
    row = store.rows[USER]
    assert (row.bot_id, row.workspace_name, row.status) == (BOT_ID, WORKSPACE_CANARY, "active")
    assert b"refresh-" not in row.refresh_token and store.refresh_expires[USER] is None
    assert await connections.access_token(USER) == "access-1"  # the exchange's token, no refresh
    assert notion.refreshes == 0
    assert "notion.connected" in caplog.text
    for canary in (CODE_CANARY, state, WORKSPACE_CANARY, WORKSPACE_ID, BOT_ID, "access-1", "refresh-1", CLIENT_CANARY):
        assert canary not in caplog.text


def test_the_state_is_the_connections_own_and_holds_user_return_path_and_expiry() -> None:
    connections = make(FakeNotion(), MemoryStore())
    state = state_of(connections.authorize_url(USER, RETURN))

    assert connections._states.verify(state, str(USER), "authorize") == RETURN
    other = StateSigner(SecretStr(TOKEN_KEY), b"specflow github oauth state")  # the same key, another integration
    assert other.verify(other.sign(str(USER), RETURN, "authorize"), str(USER), "authorize") == RETURN
    with pytest.raises(StateInvalidError):
        connections._states.verify(other.sign(str(USER), RETURN, "authorize"), str(USER), "authorize")


@pytest.mark.parametrize("case", ["other_user", "other_purpose", "expired", "garbage"])
async def test_a_bad_state_is_refused_before_notion_is_called(case: str) -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = make(notion, store, clock)
    signer = connections._states
    state = {
        "other_user": state_of(connections.authorize_url(OTHER, RETURN)),
        "other_purpose": signer.sign(str(USER), RETURN, "install"),
        "expired": state_of(connections.authorize_url(USER, RETURN)),
        "garbage": "a.b.c",
    }[case]
    if case == "expired":
        clock.now += STATE_TTL_SECONDS + 1

    with pytest.raises(NotionError) as raised:
        await connections.connect(USER, code=CODE_CANARY, state=state)

    assert raised.value.code is NotionCode.STATE_INVALID
    assert notion.requests == [] and store.rows == {}


async def test_a_refused_code_stores_nothing_and_an_outage_is_unreachable() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = make(notion, store)
    state = state_of(connections.authorize_url(USER, RETURN))

    with pytest.raises(NotionError) as refused:
        await connections.connect(USER, code="unknown", state=state)
    notion.status = 503
    with pytest.raises(NotionError) as down:
        await connections.connect(USER, code=CODE_CANARY, state=state)

    assert (refused.value.code, down.value.code) == (NotionCode.CODE_INVALID, NotionCode.UNREACHABLE)
    assert store.rows == {} and store.events == []


async def test_connecting_again_replaces_the_row_with_the_new_grant() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    await store.expire(USER, now=T0)
    notion.codes.add(CODE_CANARY)
    later = make(notion, store, now=T0 + timedelta(hours=1))
    later._tokens = connections._tokens

    view, _ = await later.connect(USER, code=CODE_CANARY, state=state_of(later.authorize_url(USER, RETURN)))

    assert view.status == "active" and store.rows[USER].status == "active"
    assert store.rows[USER].connected_at == T0 + timedelta(hours=1)
    assert await later.access_token(USER) == "access-2"


# Tokens --------------------------------------------------------------------------------------------------------------


async def test_an_expired_access_token_is_refreshed_and_the_rotated_refresh_token_is_committed_first() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    clock.now += 3600

    assert await connections.access_token(USER) == "access-2"
    assert store.events[-3:] == [("lock", USER), ("rotate", USER), ("commit", None)]
    assert notion.refresh == {"refresh-2"}  # the old refresh token is dead


async def test_a_token_with_expires_in_zero_is_used_once_and_never_cached() -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.expires_in = 0
    connections = await connected(notion, store)

    assert await connections.access_token(USER) == "access-2"
    assert await connections.access_token(USER) == "access-3"
    assert notion.refreshes == 2


async def test_a_token_without_expires_in_never_expires_and_is_replaced_only_after_a_401() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    notion.expires_in = None
    connections = await connected(notion, store, clock)
    clock.now += 5 * 365 * 24 * 3600  # years later

    assert await connections.access_token(USER) == "access-1" and notion.refreshes == 0
    assert await connections.refresh(USER, used="access-1") == "access-2"  # what a 401 does
    assert await connections.access_token(USER) == "access-2" and notion.refreshes == 1


async def test_a_grant_with_a_refresh_token_and_an_expiry_is_refreshable() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)

    assert connections._cipher.decrypt(store.rows[USER].refresh_token).get_secret_value() == "refresh-1"
    assert await connections.refresh(USER, used="access-1") == "access-2"


async def test_a_grant_with_an_access_token_alone_connects_and_cannot_be_refreshed(caplog) -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.with_refresh = False
    connections = await connected(notion, store)

    assert connections._cipher.decrypt(store.rows[USER].refresh_token).get_secret_value() == ""
    assert await connections.access_token(USER) == "access-1" and store.rows[USER].status == "active"
    calls = len(notion.requests)
    with caplog.at_level(logging.INFO), pytest.raises(NotionError) as raised:
        await connections.refresh(USER, used="access-1")  # a 401 on this connection

    assert raised.value.code is NotionCode.RECONNECT_REQUIRED
    assert store.rows[USER].status == "expired" and len(notion.requests) == calls  # Notion was not called
    assert "notion.expired" in caplog.text


async def test_a_grant_with_no_refresh_token_survives_a_restart_and_a_second_replica(caplog) -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.with_refresh = False
    first = await connected(notion, store)
    stored = store.rows[USER].access_token
    assert stored is not None and b"access-1" not in stored  # Fernet ciphertext
    assert first._cipher.decrypt(stored).get_secret_value() == "access-1"
    calls = len(notion.requests)

    with caplog.at_level(logging.DEBUG):
        for restarted in (make(notion, store), make(notion, store)):  # cache miss: a restart, another replica
            assert await restarted.access_token(USER) == "access-1"
            assert await restarted.access_token(USER) == "access-1"  # now cached
            assert "access-1" not in repr(restarted) and "access-1" not in repr(restarted._tokens)

    assert store.rows[USER].status == "active" and ("expire", USER) not in store.events
    assert len(notion.requests) == calls  # Notion was never called
    assert "access-1" not in caplog.text


async def test_a_401_on_the_stored_token_after_a_restart_expires_the_connection_and_another_token_does_not() -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.with_refresh = False
    await connected(notion, store)
    restarted = make(notion, store)
    assert await restarted.access_token(USER) == "access-1"

    assert await restarted.refresh(USER, used="some-older-token") == "access-1"  # not the token in use: no expiry
    assert store.rows[USER].status == "active"
    with pytest.raises(NotionError) as raised:
        await restarted.refresh(USER, used="access-1")

    assert raised.value.code is NotionCode.RECONNECT_REQUIRED and store.rows[USER].status == "expired"


async def test_a_refresh_answer_without_a_refresh_token_keeps_the_stored_one() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    before = store.rows[USER].refresh_token
    notion.with_refresh = False
    clock.now += 3600

    assert await connections.access_token(USER) == "access-2"

    assert store.rows[USER].refresh_token == before
    assert connections._cipher.decrypt(before).get_secret_value() == "refresh-1"
    assert "rotate" not in [event for event, _ in store.events]


async def test_a_401_through_the_gateway_of_a_non_refreshable_connection_asks_to_reconnect(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.with_refresh = False
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inner.built[0].accepted = set()

    with pytest.raises(NotionError) as raised:
        await gateway.page("p")

    assert raised.value.code is NotionCode.RECONNECT_REQUIRED
    assert len(inner.built[0].calls) == 1 and store.rows[USER].status == "expired"


async def test_callers_waiting_on_one_refresh_share_it() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    clock.now += 3600
    notion.gate = asyncio.Event()

    waiting = [asyncio.ensure_future(connections.access_token(USER)) for _ in range(4)]
    await notion.entered.wait()
    notion.gate.set()

    assert set(await asyncio.gather(*waiting)) == {"access-2"}
    assert notion.refreshes == 1


async def test_a_refused_token_is_never_handed_out_again() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)

    assert await connections.refresh(USER, used="access-1") == "access-2"
    assert await connections.refresh(USER, used="access-1") == "access-2"  # already replaced: no second rotation
    assert notion.refreshes == 1


@pytest.mark.parametrize("cause", ["invalid_grant", "rotated_key"])
async def test_a_dead_refresh_token_expires_the_connection_once(cause: str, caplog) -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    clock.now += 3600
    if cause == "invalid_grant":
        notion.error = (400, "invalid_grant")
    else:
        connections = make(notion, store, clock)
        connections._cipher = type(connections._cipher)(SecretStr(Fernet.generate_key().decode()))

    with caplog.at_level(logging.INFO), pytest.raises(NotionError) as raised:
        await connections.access_token(USER)
    assert raised.value.code is NotionCode.RECONNECT_REQUIRED
    assert store.rows[USER].status == "expired" and store.expired_at[USER] == T0
    view = await connections.get(USER)
    assert view is not None and view.status == "expired"

    calls = len(notion.requests)
    with pytest.raises(NotionError) as again:
        await connections.access_token(USER)
    assert again.value.code is NotionCode.RECONNECT_REQUIRED and len(notion.requests) == calls
    assert "notion.expired" in caplog.text and WORKSPACE_CANARY not in caplog.text


@pytest.mark.parametrize(("status", "code"), [(503, NotionCode.UNREACHABLE), (None, NotionCode.AUTH_FAILED)])
async def test_a_refresh_that_fails_for_another_reason_keeps_the_connection_active(status, code) -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    clock.now += 3600
    if status is None:
        notion.error = (401, "invalid_client")  # a wrong client secret
    else:
        notion.status = status

    with pytest.raises(NotionError) as raised:
        await connections.access_token(USER)

    assert raised.value.code is code and store.rows[USER].status == "active"
    assert ("rotate", USER) not in store.events


@pytest.mark.parametrize(
    ("change", "code"), [("delete", NotionCode.NOT_CONNECTED), ("expire", NotionCode.RECONNECT_REQUIRED)]
)
async def test_a_cached_token_is_not_served_after_another_process_ends_the_connection(change: str, code) -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    worker = await connected(notion, store, clock)  # this process holds "access-1" in its cache
    assert await worker.access_token(USER) == "access-1"
    elsewhere = make(notion, store, clock)  # another api replica
    if change == "delete":
        await elsewhere.disconnect(USER)
    else:
        await store.expire(USER, now=T0)

    with pytest.raises(NotionError) as raised:
        await worker.access_token(USER)

    assert raised.value.code is code
    assert worker._tokens._good(USER, used=None) is None  # the cache entry is gone too


async def test_a_reconnect_in_another_process_replaces_the_cached_token() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    worker = await connected(notion, store, clock)
    assert await worker.access_token(USER) == "access-1"
    later = make(notion, store, clock, now=T0 + timedelta(hours=1))
    notion.codes.add(CODE_CANARY)
    await later.connect(USER, code=CODE_CANARY, state=state_of(later.authorize_url(USER, RETURN)))  # access-2

    # Refreshed from the new row, not the old grant's cached "access-1"; then cached again.
    assert await worker.access_token(USER) == "access-3"
    assert notion.refreshes == 1
    assert await worker.access_token(USER) == "access-3"


async def test_no_connection_is_not_connected() -> None:
    with pytest.raises(NotionError) as raised:
        await make(FakeNotion(), MemoryStore()).access_token(USER)
    assert raised.value.code is NotionCode.NOT_CONNECTED
    with pytest.raises(NotionError) as none:
        await make(FakeNotion(), MemoryStore()).refresh(USER, used=None)
    assert none.value.code is NotionCode.NOT_CONNECTED


async def test_disconnect_deletes_the_row_and_revokes_the_grant_and_works_without_a_connection(caplog) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)

    with caplog.at_level(logging.INFO):
        await connections.disconnect(USER)

    assert store.rows == {} and notion.revoked == ["access-1"]
    assert "notion.disconnected" in caplog.text and "access-1" not in caplog.text
    with pytest.raises(NotionError) as raised:
        await connections.access_token(USER)
    assert raised.value.code is NotionCode.NOT_CONNECTED
    await connections.disconnect(USER)  # nothing to delete, nothing to revoke
    assert notion.revoked == ["access-1"]


async def test_disconnecting_an_expired_connection_deletes_it_without_a_revoke() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    await store.expire(USER, now=T0)

    await connections.disconnect(USER)

    assert store.rows == {} and notion.revoked == [] and notion.refreshes == 0


async def test_disconnect_never_refreshes_just_to_revoke_and_revokes_the_stored_refresh_token() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    await connected(notion, store, clock)
    clock.now += 3600  # the cached token is no longer good
    other_process = make(notion, store, clock)  # nothing cached at all (a restart, another replica)

    await other_process.disconnect(USER)

    assert store.rows == {} and notion.revoked == ["refresh-1"] and notion.refreshes == 0


async def test_a_stale_cached_token_is_not_revoked_but_the_stored_refresh_token_is() -> None:
    notion, store, clock = FakeNotion(), MemoryStore(), Clock()
    connections = await connected(notion, store, clock)
    clock.now += 3600

    await connections.disconnect(USER)

    assert store.rows == {} and notion.revoked == ["refresh-1"] and notion.refreshes == 0


async def test_disconnect_after_a_restart_revokes_the_stored_access_token_of_a_grant_with_no_refresh_token() -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.with_refresh = False
    await connected(notion, store)

    await make(notion, store).disconnect(USER)

    assert store.rows == {} and notion.revoked == ["access-1"]


async def test_a_revoke_that_cannot_decrypt_or_fails_never_blocks_the_delete(caplog) -> None:
    notion, store = FakeNotion(), MemoryStore()
    await connected(notion, store)
    store.rows[USER] = replace(store.rows[USER], refresh_token=b"not-a-fernet-token")  # a rotated token key
    with caplog.at_level(logging.INFO):
        await make(notion, store).disconnect(USER)

    assert store.rows == {} and notion.revoked == []
    assert "notion.disconnected" in caplog.text and "not-a-fernet-token" not in caplog.text


async def test_a_failed_revoke_still_deletes_the_connection() -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    notion.status = 503

    await connections.disconnect(USER)

    assert store.rows == {}


# The gateway ---------------------------------------------------------------------------------------------------------


class InnerGateway:
    """A fake of the REST gateway: every call asks the token source once and answers ``notion_auth_failed`` (as
    ``HttpNotionGateway`` does for a ``401``) unless the token is one the fake Notion still accepts."""

    built: ClassVar[list[InnerGateway]] = []

    def __init__(
        self,
        token: Any,
        limiter: RateLimiter,
        *,
        budget: WaitBudget | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token, self.limiter, self.budget, self.client = token, limiter, budget, client
        self.tokens_used: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.accepted: set[str] | None = None
        self.fail_with: NotionCode | None = None
        InnerGateway.built.append(self)

    async def _do(self, name: str, *args: Any, **kwargs: Any) -> str:
        token = await self.token()
        self.tokens_used.append(token)
        await asyncio.sleep(0)  # a call yields while it is "on the wire"
        self.calls.append((name, args, kwargs))
        if self.fail_with is not None:
            raise NotionError(self.fail_with)
        if self.accepted is not None and token not in self.accepted:
            raise NotionError(NotionCode.AUTH_FAILED)
        return name

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> str:
            return await self._do(name, *args, **kwargs)

        return call


@pytest.fixture
def inner(monkeypatch) -> type[InnerGateway]:
    InnerGateway.built = []
    monkeypatch.setattr("notion.connections.HttpNotionGateway", InnerGateway)
    return InnerGateway


async def test_the_gateway_is_a_wrapper_over_the_http_gateway_on_the_limiter_of_the_connections_bot(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)

    first = await connections.gateway(USER)
    second = await connections.gateway(USER)

    assert isinstance(first, RefreshingNotionGateway)
    one, two = inner.built
    assert one.limiter is two.limiter is connections._limiters.for_bot(BOT_ID)
    assert connections._limiters.for_bot("another-bot") is not one.limiter
    assert isinstance(one.budget, WaitBudget) and one.budget is not two.budget  # a fresh budget per gateway
    assert one.client is connections._http
    assert second is not first


async def test_the_gateway_of_a_user_without_a_usable_connection_is_refused(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = make(notion, store)
    with pytest.raises(NotionError) as missing:
        await connections.gateway(USER)
    connections = await connected(notion, store)
    await store.expire(USER, now=T0)
    with pytest.raises(NotionError) as expired:
        await connections.gateway(USER)

    assert (missing.value.code, expired.value.code) == (NotionCode.NOT_CONNECTED, NotionCode.RECONNECT_REQUIRED)
    assert inner.built == []


async def test_a_401_refreshes_with_the_token_it_handed_out_and_repeats_the_call_once(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inside = inner.built[0]
    notion.expire_access()  # "access-1" is dead on Notion's side
    inside.accepted = {"access-2"}

    await gateway.create_comment("page-1", "hello")

    assert inside.tokens_used == ["access-1", "access-2"]
    assert [call[0] for call in inside.calls] == ["create_comment"] * 2
    assert inside.calls[0] == inside.calls[1] == ("create_comment", ("page-1", "hello"), {})
    assert notion.refreshes == 1 and store.rows[USER].status == "active"
    inside.accepted = {"access-2"}
    await gateway.page("page-1")  # the refreshed token is cached: no second refresh
    assert notion.refreshes == 1 and inside.tokens_used[-1] == "access-2"


async def test_the_repeat_uses_the_refreshed_token_even_when_it_is_not_cached(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    notion.expires_in = 0  # use once: nothing is cached
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inside = inner.built[0]
    inside.accepted = {"access-3"}  # access-1 is the exchange's, access-2 the first call's; the retry's must be fresh

    await gateway.page("p")

    assert inside.tokens_used == ["access-2", "access-3"] and notion.refreshes == 2


async def test_concurrent_calls_each_refresh_with_the_token_they_were_handed(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inside = inner.built[0]
    inside.accepted = {"access-2"}

    answers = await asyncio.gather(gateway.page("a"), gateway.page("b"), gateway.page("c"))

    assert answers == ["page"] * 3
    assert notion.refreshes == 1  # the later 401s were of access-1, already replaced: no second rotation


async def test_a_second_401_is_the_answer_after_one_refresh_and_one_repeat(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inside = inner.built[0]
    inside.accepted = set()  # Notion refuses even a fresh token

    with pytest.raises(NotionError) as raised:
        await gateway.page("p")

    assert raised.value.code is NotionCode.AUTH_FAILED
    assert len(inside.calls) == 2 and notion.refreshes == 1


@pytest.mark.parametrize("code", [NotionCode.NOT_SHARED, NotionCode.RATE_LIMITED, NotionCode.UNREACHABLE])
async def test_any_other_failure_passes_through_without_a_refresh(inner, code: NotionCode) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inner.built[0].fail_with = code

    with pytest.raises(NotionError) as raised:
        await gateway.database("d")

    assert raised.value.code is code and len(inner.built[0].calls) == 1 and notion.refreshes == 0


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ({"error": (400, "invalid_grant")}, NotionCode.RECONNECT_REQUIRED),
        ({"status": 503}, NotionCode.UNREACHABLE),
    ],
)
async def test_a_refresh_that_fails_during_the_repeat_is_its_own_code_and_the_call_is_not_repeated(
    inner, refusal, code
) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    inside = inner.built[0]
    inside.accepted = {"never"}
    notion.error, notion.status = refusal.get("error"), refusal.get("status")

    with pytest.raises(NotionError) as raised:
        await gateway.update_page("p", properties={"a": 1})

    assert raised.value.code is code and len(inside.calls) == 1
    assert store.rows[USER].status == ("expired" if code is NotionCode.RECONNECT_REQUIRED else "active")


async def test_every_gateway_method_forwards_its_arguments(inner) -> None:
    notion, store = FakeNotion(), MemoryStore()
    connections = await connected(notion, store)
    gateway = await connections.gateway(USER)
    props: dict[str, Any] = {"Name": {}}

    await gateway.page("a")
    await gateway.database("b")
    await gateway.data_source("c")
    await gateway.page_markdown("d")
    await gateway.query_data_source("e")
    await gateway.query_data_source("e", start_cursor="cur", page_size=5, where={"f": 1})
    await gateway.create_database("f", "Title", props)
    await gateway.update_data_source("g", props)
    await gateway.create_page("h", properties=props, markdown="# x")
    await gateway.update_page("i", properties=props)
    await gateway.replace_markdown("j", "body")
    await gateway.create_comment("k", "note")

    assert inner.built[0].calls == [
        ("page", ("a",), {}),
        ("database", ("b",), {}),
        ("data_source", ("c",), {}),
        ("page_markdown", ("d",), {}),
        ("query_data_source", ("e",), {"start_cursor": None, "page_size": PAGE_SIZE, "where": None}),
        ("query_data_source", ("e",), {"start_cursor": "cur", "page_size": 5, "where": {"f": 1}}),
        ("create_database", ("f", "Title", props), {}),
        ("update_data_source", ("g", props), {}),
        ("create_page", ("h",), {"properties": props, "markdown": "# x"}),
        ("update_page", ("i",), {"properties": props}),
        ("replace_markdown", ("j", "body"), {}),
        ("create_comment", ("k", "note"), {}),
    ]


async def test_the_wrapper_follows_the_gateway_protocol_method_for_method() -> None:
    names = [name for name in vars(NotionGateway) if not name.startswith("_")]

    assert len(names) == 11
    assert set(names) <= set(vars(RefreshingNotionGateway))
