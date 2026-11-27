"""``DropboxConnections`` (docs/dropbox/oauth.md "Connect flow", "Tokens") over a scripted Dropbox and an in-memory
store. No token, code, ``state``, display name or e-mail may reach a log line or an error."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from dropbox.codes import DropboxCode, DropboxError
from dropbox.connections import DropboxConnections
from dropbox_support import (
    ACCOUNT_ID,
    ACCOUNT_TEAM,
    API_HOST,
    CLIENT_CANARY,
    CODE_CANARY,
    EMAIL_CANARY,
    INVALID_GRANT,
    NAME_CANARY,
    T0,
    TEAM_ID,
    FakeDropbox,
    MemoryStore,
    dropbox_settings,
)
from integrations.cipher import TokenCipher
from integrations.state import STATE_TTL_SECONDS

USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
RETURN = "/projects/p/integrations?dropbox=1"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(dropbox: FakeDropbox, store: MemoryStore, clock: Clock | None = None) -> DropboxConnections:
    clock = clock or Clock()
    return DropboxConnections(
        dropbox_settings(),
        store=store.transaction,
        client=dropbox.client(),
        now=lambda: T0,
        monotonic=clock,
        clock=clock,
    )


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connected(dropbox: FakeDropbox, store: MemoryStore, clock: Clock | None = None) -> DropboxConnections:
    connections = make(dropbox, store, clock)
    await connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))
    return connections


async def code_of(awaitable) -> DropboxCode:
    with pytest.raises(DropboxError) as raised:
        await awaitable
    return raised.value.code


def stored_token(store: MemoryStore) -> str:
    return TokenCipher(dropbox_settings().token_key).decrypt(store.rows[USER].refresh_token).get_secret_value()


# State ---------------------------------------------------------------------------------------------------------------


async def test_the_authorize_url_carries_offline_access_and_a_state_bound_to_the_user() -> None:
    connections = make(FakeDropbox(), MemoryStore())
    url = connections.authorize_url(USER, RETURN)
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).netloc == "www.dropbox.com"
    assert query["token_access_type"] == ["offline"] and query["response_type"] == ["code"]
    assert state_of(url) != state_of(connections.authorize_url(USER, RETURN))


async def test_a_forged_expired_or_foreign_state_is_refused_before_any_call_to_dropbox() -> None:
    dropbox, clock = FakeDropbox(), Clock()
    connections = make(dropbox, MemoryStore(), clock)
    state = state_of(connections.authorize_url(USER, RETURN))
    forged = state[:-2] + ("AA" if not state.endswith("AA") else "BB")

    assert await code_of(connections.connect(USER, code=CODE_CANARY, state=forged)) is DropboxCode.STATE_INVALID
    assert await code_of(connections.connect(USER, code=CODE_CANARY, state="x")) is DropboxCode.STATE_INVALID
    assert await code_of(connections.connect(OTHER, code=CODE_CANARY, state=state)) is DropboxCode.STATE_INVALID
    clock.now += STATE_TTL_SECONDS + 1
    assert await code_of(connections.connect(USER, code=CODE_CANARY, state=state)) is DropboxCode.STATE_INVALID
    assert dropbox.calls == []


async def test_a_state_of_another_application_key_is_refused() -> None:
    dropbox = FakeDropbox()
    state = state_of(make(dropbox, MemoryStore()).authorize_url(USER, RETURN))
    clock = Clock()
    settings = dropbox_settings().model_copy(update={"token_key": SecretStr("A" * 43 + "=")})
    elsewhere = DropboxConnections(
        settings, store=MemoryStore().transaction, client=dropbox.client(), monotonic=clock, clock=clock
    )

    assert await code_of(elsewhere.connect(USER, code=CODE_CANARY, state=state)) is DropboxCode.STATE_INVALID


# Connect -------------------------------------------------------------------------------------------------------------


async def test_connect_stores_the_account_and_an_encrypted_refresh_token_but_never_the_email(caplog) -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = make(dropbox, store)
    state = state_of(connections.authorize_url(USER, RETURN))
    with caplog.at_level(logging.DEBUG):
        view, return_to = await connections.connect(USER, code=CODE_CANARY, state=state)

    row = store.rows[USER]
    assert return_to == RETURN
    assert (view.status, view.name, view.team_name) == ("active", NAME_CANARY, None)
    assert (view.workspace_id, view.workspace_name) == (f"user:{ACCOUNT_ID}", NAME_CANARY)
    assert (row.account_id, row.name, row.team_id, row.status) == (ACCOUNT_ID, NAME_CANARY, None, "active")
    assert b"refresh-1" not in row.refresh_token and stored_token(store) == "refresh-1"
    assert EMAIL_CANARY not in repr(row) and EMAIL_CANARY not in repr(view) and EMAIL_CANARY not in caplog.text
    for canary in (CODE_CANARY, CLIENT_CANARY, "sl.access-1", "refresh-1", NAME_CANARY, ACCOUNT_ID, state):
        assert canary not in caplog.text
    assert "dropbox.connected" in caplog.text and dropbox.violations == []


async def test_a_business_member_is_the_team_workspace() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    dropbox.codes[CODE_CANARY] = ACCOUNT_TEAM
    connections = await connected(dropbox, store)

    view = await connections.get(USER)

    assert view is not None
    assert (view.workspace_id, view.workspace_name, view.team_name) == (f"team:{TEAM_ID}", "Acme Team", "Acme Team")
    assert store.rows[USER].team_id == TEAM_ID


async def test_a_code_dropbox_refuses_is_code_invalid_and_a_down_dropbox_is_unreachable() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = make(dropbox, store)

    wrong = connections.connect(USER, code="wrong", state=state_of(connections.authorize_url(USER, RETURN)))
    assert await code_of(wrong) is DropboxCode.CODE_INVALID
    dropbox.status = 503
    url = connections.authorize_url(USER, RETURN)
    assert await code_of(connections.connect(USER, code=CODE_CANARY, state=state_of(url))) is DropboxCode.UNREACHABLE
    assert store.rows == {}


class AccountDownDropbox(FakeDropbox):
    def __init__(self, status: int) -> None:
        super().__init__()
        self.account_status = status

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/users/get_current_account":
            return httpx.Response(self.account_status)
        return await super().handle(request)


async def test_an_account_call_that_fails_after_the_exchange_stores_nothing() -> None:
    for status, code in ((500, DropboxCode.UNREACHABLE), (403, DropboxCode.CODE_INVALID)):
        store = MemoryStore()
        connections = make(AccountDownDropbox(status), store)
        url = connections.authorize_url(USER, RETURN)

        assert await code_of(connections.connect(USER, code=CODE_CANARY, state=state_of(url))) is code
        assert store.rows == {}


async def test_reconnecting_replaces_the_row_and_the_old_cache() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)
    first = await connections.access_token(USER)
    dropbox.codes[CODE_CANARY] = ACCOUNT_TEAM
    await connections.connect(USER, code=CODE_CANARY, state=state_of(connections.authorize_url(USER, RETURN)))

    assert store.rows[USER].team_id == TEAM_ID and stored_token(store) == "refresh-2"
    assert await connections.access_token(USER) == "sl.access-2" != first


# Tokens --------------------------------------------------------------------------------------------------------------


async def test_the_access_token_is_cached_until_shortly_before_expires_in_runs_out() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)

    assert await connections.access_token(USER) == "sl.access-1"
    assert dropbox.refreshes == 0
    clock.now += 14400 - 60 - 1
    assert await connections.access_token(USER) == "sl.access-1"
    clock.now += 2

    assert await connections.access_token(USER) == "sl.access-2"
    assert dropbox.refreshes == 1


async def test_a_refresh_reads_expires_in_of_its_answer() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    clock.now += 14400
    dropbox.expires_in = 600

    assert await connections.access_token(USER) == "sl.access-2"
    clock.now += 600 - 60 - 1
    assert await connections.access_token(USER) == "sl.access-2"
    clock.now += 2
    assert await connections.access_token(USER) == "sl.access-3"


async def test_a_refresh_never_rotates_the_refresh_token_and_never_locks_the_row() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    before = store.rows[USER].refresh_token
    clock.now += 20000
    store.events.clear()

    await connections.access_token(USER)

    assert store.rows[USER].refresh_token == before and stored_token(store) == "refresh-1"
    assert [kind for kind, _ in store.events] == ["commit", "commit", "touch", "commit"]  # read, read, then touch
    assert store.refresh_expires == {}


class TrackedStore(MemoryStore):
    """Counts the transactions that are open."""

    def __init__(self) -> None:
        super().__init__()
        self.open = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryStore]:
        self.open += 1
        try:
            async with super().transaction() as store:
                yield store
        finally:
            self.open -= 1


class WatchedDropbox(FakeDropbox):
    """Records how many store transactions are open at each token call."""

    def __init__(self, store: TrackedStore) -> None:
        super().__init__()
        self.store = store
        self.open_at_token: list[int] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == API_HOST and request.url.path == "/oauth2/token":
            self.open_at_token.append(self.store.open)
        return await super().handle(request)


@pytest.mark.parametrize("grant_error", [None, INVALID_GRANT])
async def test_the_token_endpoint_is_called_outside_any_transaction(grant_error) -> None:
    store, clock = TrackedStore(), Clock()
    dropbox = WatchedDropbox(store)
    connections = await connected(dropbox, store, clock)
    clock.now += 20000
    dropbox.grant_error = grant_error
    dropbox.open_at_token.clear()

    with suppress(DropboxError):
        await connections.access_token(USER)

    assert dropbox.open_at_token == [0]
    assert store.open == 0
    assert (store.rows[USER].status == "expired") is (grant_error is not None)


class GatedDropbox(FakeDropbox):
    """Holds every token call until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == API_HOST and request.url.path == "/oauth2/token":
            await self.gate.wait()
        return await super().handle(request)


async def test_two_concurrent_refreshes_in_one_process_make_one_exchange() -> None:
    dropbox, store, clock = GatedDropbox(), MemoryStore(), Clock()
    dropbox.gate.set()
    connections = await connected(dropbox, store, clock)
    dropbox.gate.clear()
    clock.now += 20000

    first = asyncio.create_task(connections.access_token(USER))
    await asyncio.sleep(0)
    second = asyncio.create_task(connections.access_token(USER))
    await asyncio.sleep(0)
    dropbox.gate.set()

    assert await asyncio.gather(first, second) == ["sl.access-2", "sl.access-2"]
    assert dropbox.refreshes == 1


async def test_a_dead_refresh_token_marks_the_connection_expired_and_it_is_never_tried_again(caplog) -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    clock.now += 20000
    dropbox.grant_error = INVALID_GRANT
    with caplog.at_level(logging.INFO):
        assert await code_of(connections.access_token(USER)) is DropboxCode.RECONNECT_REQUIRED

    assert store.rows[USER].status == "expired" and USER in store.expired_at
    view = await connections.get(USER)
    assert view is not None and view.status == "expired"
    before = dropbox.refreshes
    assert await code_of(connections.access_token(USER)) is DropboxCode.RECONNECT_REQUIRED
    assert await code_of(connections.refresh(USER, used=None)) is DropboxCode.RECONNECT_REQUIRED
    assert dropbox.refreshes == before and "dropbox.expired" in caplog.text
    assert "refresh-1" not in caplog.text and NAME_CANARY not in caplog.text


async def test_a_refresh_token_that_no_longer_decrypts_expires_the_connection() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    store.rows[USER] = replace(store.rows[USER], refresh_token=b"not a fernet token")
    clock.now += 20000

    assert await code_of(connections.access_token(USER)) is DropboxCode.RECONNECT_REQUIRED
    assert store.rows[USER].status == "expired" and dropbox.refreshes == 0


async def test_the_refusals_of_a_refresh_map_to_closed_codes() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    clock.now += 20000
    dropbox.status = 503
    assert await code_of(connections.access_token(USER)) is DropboxCode.UNREACHABLE
    assert store.rows[USER].status == "active"  # a down Dropbox does not end the connection
    dropbox.status = None
    dropbox.grant_error = {"error": "invalid_client"}
    assert await code_of(connections.access_token(USER)) is DropboxCode.AUTH_FAILED
    assert store.rows[USER].status == "active"
    assert await code_of(make(dropbox, MemoryStore()).access_token(USER)) is DropboxCode.NOT_CONNECTED


async def test_a_401_refreshes_once_and_repeats_the_request() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)
    dropbox.expire_access()

    account = await connections.call(USER, connections.client.get_current_account)

    assert account.account_id == ACCOUNT_ID and dropbox.refreshes == 1 and stored_token(store) == "refresh-1"


async def test_a_second_refused_token_is_auth_failed() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)
    attempts: list[str] = []

    async def refused(token: SecretStr) -> None:
        attempts.append(token.get_secret_value())
        raise DropboxError(DropboxCode.ACCESS_REFUSED)

    assert await code_of(connections.call(USER, refused)) is DropboxCode.AUTH_FAILED
    assert attempts == ["sl.access-1", "sl.access-2"]


async def test_other_errors_of_a_request_pass_through_without_a_refresh() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)

    async def missing(_token: SecretStr) -> None:
        raise DropboxError(DropboxCode.NO_ACCESS)

    assert await code_of(connections.call(USER, missing)) is DropboxCode.NO_ACCESS
    assert dropbox.refreshes == 0


async def test_a_connection_another_process_deleted_or_replaced_is_not_served_from_the_cache() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)
    row = store.rows[USER]
    store.rows[USER] = replace(row, connected_at=row.connected_at.replace(year=2027))  # another process reconnected

    assert await connections.access_token(USER) == "sl.access-2"  # refreshed from the row, not the old cache
    store.rows.pop(USER)

    assert await code_of(connections.access_token(USER)) is DropboxCode.NOT_CONNECTED


# Disconnect ----------------------------------------------------------------------------------------------------------


async def test_disconnect_revokes_the_access_token_and_deletes_the_row() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)

    await connections.disconnect(USER)

    assert dropbox.revoked == ["sl.access-1"] and store.rows == {} and await connections.get(USER) is None
    assert await code_of(connections.access_token(USER)) is DropboxCode.NOT_CONNECTED


async def test_disconnect_refreshes_first_when_no_access_token_is_cached() -> None:
    dropbox, store, clock = FakeDropbox(), MemoryStore(), Clock()
    connections = await connected(dropbox, store, clock)
    clock.now += 20000

    await connections.disconnect(USER)

    assert dropbox.revoked == ["sl.access-2"] and store.rows == {}


class RevokeDownDropbox(FakeDropbox):
    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2/auth/token/revoke":
            raise httpx.ConnectError("down")
        return await super().handle(request)


async def test_disconnect_deletes_the_row_even_when_the_revoke_fails() -> None:
    dropbox, store = RevokeDownDropbox(), MemoryStore()
    connections = await connected(dropbox, store)

    await connections.disconnect(USER)

    assert store.rows == {} and dropbox.revoked == []


async def test_disconnecting_an_expired_or_missing_connection_just_deletes_it() -> None:
    dropbox, store = FakeDropbox(), MemoryStore()
    connections = await connected(dropbox, store)
    store.rows[USER] = replace(store.rows[USER], status="expired")

    await connections.disconnect(USER)
    await connections.disconnect(USER)

    assert store.rows == {} and dropbox.revoked == []
