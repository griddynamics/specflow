"""``BoxConnections`` (docs/box/oauth.md "Connect flow", "Tokens") over a scripted Box and an in-memory store. No token,
code, ``state``, display name or e-mail may reach a log line or an error."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from box.client import REFRESH_VALID_SECONDS
from box.codes import BoxCode, BoxError
from box.connections import BoxConnections
from box_support import (
    CLIENT_CANARY,
    CODE_CANARY,
    ENTERPRISE_ID,
    INVALID_GRANT,
    NAME_CANARY,
    T0,
    USER_ID,
    USERS_ME_PERSONAL,
    FakeBox,
    MemoryStore,
    box_settings,
)
from integrations.cipher import TokenCipher
from integrations.state import STATE_TTL_SECONDS

USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
RETURN = "/projects/p/settings?box=1"
LOGIN_CANARY = "login-canary@example.test"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(box: FakeBox, store: MemoryStore, clock: Clock | None = None) -> BoxConnections:
    clock = clock or Clock()
    return BoxConnections(
        box_settings(), store=store.transaction, client=box.client(), now=lambda: T0, monotonic=clock, clock=clock
    )


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connected(box: FakeBox, store: MemoryStore, clock: Clock | None = None) -> BoxConnections:
    connections = make(box, store, clock)
    await connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))
    return connections


async def code_of(awaitable) -> BoxCode:
    with pytest.raises(BoxError) as raised:
        await awaitable
    return raised.value.code


def stored_token(store: MemoryStore) -> str:
    return TokenCipher(box_settings().token_key).decrypt(store.rows[USER].refresh_token).get_secret_value()


# State ---------------------------------------------------------------------------------------------------------------


async def test_the_authorize_url_carries_a_state_bound_to_the_user() -> None:
    connections = make(FakeBox(), MemoryStore())
    query = parse_qs(urlsplit(connections.authorize_url(USER, RETURN)).query)

    assert query["scope"] == ["root_readwrite"] and query["response_type"] == ["code"]
    assert state_of(connections.authorize_url(USER, RETURN)) != state_of(connections.authorize_url(USER, RETURN))


async def test_a_forged_expired_or_foreign_state_is_refused_before_any_call_to_box() -> None:
    box, clock = FakeBox(), Clock()
    connections = make(box, MemoryStore(), clock)
    state = state_of(connections.authorize_url(USER, RETURN))
    forged = state[:-2] + ("AA" if not state.endswith("AA") else "BB")

    assert await code_of(connections.connect(USER, code=CODE_CANARY, state=forged)) is BoxCode.STATE_INVALID
    assert await code_of(connections.connect(USER, code=CODE_CANARY, state="not-a-state")) is BoxCode.STATE_INVALID
    assert await code_of(connections.connect(OTHER, code=CODE_CANARY, state=state)) is BoxCode.STATE_INVALID
    clock.now += STATE_TTL_SECONDS + 1
    assert await code_of(connections.connect(USER, code=CODE_CANARY, state=state)) is BoxCode.STATE_INVALID
    assert box.calls == []


async def test_a_state_of_another_application_key_is_refused() -> None:
    box = FakeBox()
    state = state_of(make(box, MemoryStore()).authorize_url(USER, RETURN))
    clock = Clock()
    settings = box_settings().model_copy(update={"token_key": SecretStr("A" * 43 + "=")})
    elsewhere = BoxConnections(
        settings, store=MemoryStore().transaction, client=box.client(), monotonic=clock, clock=clock
    )

    assert await code_of(elsewhere.connect(USER, code=CODE_CANARY, state=state)) is BoxCode.STATE_INVALID


# Connect -------------------------------------------------------------------------------------------------------------


async def test_connect_stores_the_account_and_an_encrypted_refresh_token_but_never_the_email(caplog) -> None:
    box, store = FakeBox(), MemoryStore()
    connections = make(box, store)
    state = state_of(connections.authorize_url(USER, RETURN))
    with caplog.at_level(logging.DEBUG):
        view, return_to = await connections.connect(USER, code=CODE_CANARY, state=state)

    row = store.rows[USER]
    assert return_to == RETURN
    assert (view.status, view.name, view.enterprise_name) == ("active", NAME_CANARY, "Acme Corp")
    assert (view.workspace_id, view.workspace_name) == (ENTERPRISE_ID, "Acme Corp")
    assert (row.box_user_id, row.name, row.enterprise_id, row.status) == (USER_ID, NAME_CANARY, ENTERPRISE_ID, "active")
    assert b"refresh-1" not in row.refresh_token and stored_token(store) == "refresh-1"
    assert store.refresh_expires[USER] == T0 + timedelta(seconds=REFRESH_VALID_SECONDS)
    assert LOGIN_CANARY not in repr(row) and LOGIN_CANARY not in repr(view) and LOGIN_CANARY not in caplog.text
    for canary in (CODE_CANARY, CLIENT_CANARY, "access-1", "refresh-1", NAME_CANARY, state):
        assert canary not in caplog.text
    assert box.violations == []


async def test_a_personal_account_is_its_own_workspace() -> None:
    box, store = FakeBox(), MemoryStore()
    box.codes[CODE_CANARY] = USERS_ME_PERSONAL
    connections = await connected(box, store)

    view = await connections.get(USER)

    assert view is not None
    assert (view.workspace_id, view.workspace_name, view.enterprise_name) == (
        f"user:{USERS_ME_PERSONAL['id']}",
        "Pat Personal",
        None,
    )


async def test_a_code_box_refuses_is_code_invalid_and_a_down_box_is_unreachable() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = make(box, store)

    wrong = connections.connect(USER, code="wrong", state=state_of(connections.authorize_url(USER, RETURN)))
    assert await code_of(wrong) is BoxCode.CODE_INVALID
    box.status = 503
    unreachable = connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))

    assert await code_of(unreachable) is BoxCode.UNREACHABLE
    assert store.rows == {}


async def test_reconnecting_replaces_the_row_and_the_old_cache() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)
    first = await connections.access_token(USER)
    box.codes[CODE_CANARY] = USERS_ME_PERSONAL
    await connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))

    assert store.rows[USER].box_user_id == USERS_ME_PERSONAL["id"] and store.rows[USER].enterprise_id is None
    assert stored_token(store) == "refresh-2"
    assert await connections.access_token(USER) == "access-2" != first


# Tokens --------------------------------------------------------------------------------------------------------------


async def test_the_access_token_is_cached_until_shortly_before_it_expires() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)

    assert await connections.access_token(USER) == "access-1"
    assert box.refreshes == 0
    clock.now += 3599 - 60 + 1

    assert await connections.access_token(USER) == "access-2"
    assert box.refreshes == 1


async def test_a_refresh_commits_the_rotated_token_before_the_new_access_token_is_used() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    store.events.clear()
    order: list[str] = []

    async def use(token: SecretStr) -> None:
        order.append(f"use {token.get_secret_value()} after {[kind for kind, _ in store.events]}")

    await connections.call(USER, use)

    assert stored_token(store) == "refresh-2"
    assert order == ["use access-2 after ['commit', 'lock', 'rotate', 'commit']"]
    assert store.refresh_expires[USER] == T0 + timedelta(seconds=REFRESH_VALID_SECONDS)


class BrokenStore(MemoryStore):
    async def rotate(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database gone")


async def test_a_failed_commit_of_the_rotated_token_hands_out_no_access_token() -> None:
    box, store, clock = FakeBox(), BrokenStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    used: list[str] = []

    async def use(token: SecretStr) -> None:
        used.append(token.get_secret_value())

    with pytest.raises(RuntimeError):
        await connections.call(USER, use)

    assert used == [] and box.refreshes == 1


async def test_two_concurrent_refreshes_in_one_process_make_one_exchange() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    box.gate = asyncio.Event()

    first = asyncio.create_task(connections.access_token(USER))
    await box.entered.wait()
    second = asyncio.create_task(connections.access_token(USER))
    await asyncio.sleep(0)
    box.gate.set()

    assert await asyncio.gather(first, second) == ["access-2", "access-2"]
    assert box.refreshes == 1


async def test_a_row_another_process_rotated_is_used_as_stored_and_exchanged_once() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    # Another process refreshed with the stored token first: Box killed refresh-1, the row now holds refresh-2.
    other = make(box, store, Clock(clock.now))
    await other.access_token(USER)
    assert stored_token(store) == "refresh-2" and box.refreshes == 1

    fresh = await connections.access_token(USER)  # this process still caches an expired access-1

    assert fresh == "access-3" and stored_token(store) == "refresh-3"
    assert box.refreshes == 2  # one exchange each, no exchange with the dead refresh-1


async def test_a_dead_refresh_token_marks_the_connection_expired_and_it_is_never_tried_again(caplog) -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    box.grant_error = INVALID_GRANT
    with caplog.at_level(logging.INFO):
        assert await code_of(connections.access_token(USER)) is BoxCode.RECONNECT_REQUIRED

    assert store.rows[USER].status == "expired" and USER in store.expired_at
    view = await connections.get(USER)
    assert view is not None and view.status == "expired"
    before = box.refreshes
    assert await code_of(connections.access_token(USER)) is BoxCode.RECONNECT_REQUIRED
    assert box.refreshes == before and "box.expired" in caplog.text
    assert "refresh-1" not in caplog.text


async def test_a_refresh_token_that_no_longer_decrypts_expires_the_connection() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    store.rows[USER] = replace(store.rows[USER], refresh_token=b"not a fernet token")
    clock.now += 4000

    assert await code_of(connections.access_token(USER)) is BoxCode.RECONNECT_REQUIRED
    assert store.rows[USER].status == "expired" and box.refreshes == 0


async def test_the_refusals_of_a_refresh_map_to_closed_codes() -> None:
    box, store, clock = FakeBox(), MemoryStore(), Clock()
    connections = await connected(box, store, clock)
    clock.now += 4000
    box.status = 503
    assert await code_of(connections.access_token(USER)) is BoxCode.UNREACHABLE
    assert store.rows[USER].status == "active"  # a down Box does not end the connection
    box.status = None
    box.grant_error = {"error": "invalid_client"}
    assert await code_of(connections.access_token(USER)) is BoxCode.AUTH_FAILED
    assert store.rows[USER].status == "active"
    assert await code_of(make(box, MemoryStore()).access_token(USER)) is BoxCode.NOT_CONNECTED


async def test_a_401_refreshes_once_and_repeats_the_request() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)
    box.expire_access()

    user = await connections.call(USER, connections.client.user)

    assert user.id == USER_ID and box.refreshes == 1 and stored_token(store) == "refresh-2"


async def test_a_second_401_is_auth_failed() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)
    attempts: list[str] = []

    async def refused(token: SecretStr) -> None:
        attempts.append(token.get_secret_value())
        raise BoxError(BoxCode.ACCESS_REFUSED)

    assert await code_of(connections.call(USER, refused)) is BoxCode.AUTH_FAILED
    assert attempts == ["access-1", "access-2"]


async def test_other_errors_of_a_request_pass_through_without_a_refresh() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)

    async def missing(_token: SecretStr) -> None:
        raise BoxError(BoxCode.NO_ACCESS)

    assert await code_of(connections.call(USER, missing)) is BoxCode.NO_ACCESS
    assert box.refreshes == 0


async def test_a_connection_another_process_deleted_is_not_served_from_the_cache() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)
    store.rows.pop(USER)

    assert await code_of(connections.access_token(USER)) is BoxCode.NOT_CONNECTED


# Disconnect ----------------------------------------------------------------------------------------------------------


async def test_disconnect_revokes_the_access_token_and_deletes_the_row() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)

    await connections.disconnect(USER)

    assert box.revoked == ["access-1"] and store.rows == {} and await connections.get(USER) is None
    assert await code_of(connections.access_token(USER)) is BoxCode.NOT_CONNECTED


class RevokeDownBox(FakeBox):
    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/revoke":
            raise httpx.ConnectError("down")
        return await super().handle(request)


async def test_disconnect_deletes_the_row_even_when_the_revoke_fails() -> None:
    box, store = RevokeDownBox(), MemoryStore()
    connections = await connected(box, store)

    await connections.disconnect(USER)

    assert store.rows == {} and box.revoked == []


async def test_disconnecting_an_expired_or_missing_connection_just_deletes_it() -> None:
    box, store = FakeBox(), MemoryStore()
    connections = await connected(box, store)
    store.rows[USER] = replace(store.rows[USER], status="expired")

    await connections.disconnect(USER)
    await connections.disconnect(USER)

    assert store.rows == {} and box.revoked == []
