"""The Linear connections (plans/LINEAR/LINEAR-PLAN.md stream S1): the OAuth client against a scripted Linear and
``LinearConnections`` over an in-memory store. No token, code, ``state`` or member name may reach a log line, an error
or a ``repr``."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from integrations.cipher import TokenCipher
from linear.connections import ConnectFailure, LinearConnectError
from linear.errors import LinearInvalidError, LinearUnauthorizedError, LinearUnavailableError
from linear.oauth import GRANT_URL, LinearOAuthClient
from linear_oauth_support import (
    CLIENT_CANARY,
    CODE_CANARY,
    NAME_CANARY,
    OTHER,
    REDIRECT,
    TOKEN_KEY,
    USER,
    WORKSPACE,
    Clock,
    FakeLinear,
    MemoryStore,
    connected,
    consent_of,
    make_connections,
    s256,
)

DAY = 86400


# The OAuth client ----------------------------------------------------------------------------------------------------


def test_the_consent_url_asks_for_a_user_actor_with_pkce_and_state() -> None:
    query = consent_of(FakeLinear().client().authorize_url("the-state", "the-challenge"))

    assert query == {
        "client_id": "linear-client-1",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "read,write",
        "state": "the-state",
        "code_challenge": "the-challenge",
        "code_challenge_method": "S256",
        "actor": "user",
    }
    assert CLIENT_CANARY not in str(query) and repr(FakeLinear().client()) == "LinearOAuthClient()"


async def test_the_client_exchanges_refreshes_and_revokes_with_form_encoded_calls() -> None:
    linear = FakeLinear()
    client = linear.client()
    linear.allow("c1", s256("v1"))

    grant = await client.exchange("c1", "v1")
    again = await client.refresh(grant.refresh_token)
    await client.revoke(again.access_token)

    assert (grant.access_token.get_secret_value(), grant.expires_in, grant.scope) == ("access-1", DAY, "read,write")
    assert again.refresh_token.get_secret_value() == "refresh-2"
    assert linear.revoked == [("access-2", "access_token")]
    assert "access-1" not in repr(grant) and "refresh-1" not in repr(grant)


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        ("dead_grant", LinearUnauthorizedError),
        ("bad_request", LinearInvalidError),
        ("down", LinearUnavailableError),
    ],
)
async def test_every_refusal_of_the_token_endpoint_is_one_closed_error(setup: str, error: type[Exception]) -> None:
    linear = FakeLinear()
    client = linear.client()
    if setup == "dead_grant":
        linear.error = "invalid_grant"
    elif setup == "bad_request":
        linear.error = "invalid_request"
    else:
        linear.status = 503

    with pytest.raises(error) as raised:
        await client.refresh(SecretStr("refresh-x"))

    assert str(raised.value).startswith("linear_") and "canary" not in str(raised.value)


async def test_a_network_failure_is_unavailable_and_a_wrong_app_secret_is_forbidden() -> None:
    wrong = LinearOAuthClient("linear-client-1", SecretStr("other"), REDIRECT, transport=FakeLinear().transport())
    with pytest.raises(Exception, match="linear_forbidden"):
        await wrong.refresh(SecretStr("refresh-x"))

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to " + GRANT_URL)

    dead = LinearOAuthClient("c", SecretStr("s"), REDIRECT, transport=httpx.MockTransport(refuse))
    with pytest.raises(LinearUnavailableError) as raised:
        await dead.refresh(SecretStr("refresh-x"))
    assert "no route" not in str(raised.value)


# Connect -------------------------------------------------------------------------------------------------------------


async def test_connect_stores_the_encrypted_pair_and_the_viewer_and_logs_nothing_secret(caplog) -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = make_connections(linear, store)
    query = consent_of(connections.authorize_url(USER, "/projects/p/integrations?app=linear"))
    linear.allow(CODE_CANARY, query["code_challenge"])

    with caplog.at_level(logging.DEBUG):
        view, return_to = await connections.connect(USER, code=CODE_CANARY, state=query["state"])

    assert (view.status, view.user_name, view.organization_name, return_to) == (
        "active",
        NAME_CANARY,
        WORKSPACE[1],
        "/projects/p/integrations?app=linear",
    )
    row = store.rows[USER]
    assert (row.organization_id, row.organization_url_key, row.scopes) == ("org-1", "acme", "read,write")
    assert b"access-" not in row.access_token and b"refresh-" not in row.refresh_token
    cipher = TokenCipher(SecretStr(TOKEN_KEY))
    assert cipher.decrypt(row.access_token).get_secret_value() == "access-1"
    assert cipher.decrypt(row.refresh_token).get_secret_value() == "refresh-1"
    assert await connections.access_token(USER) == "access-1" and linear.refreshes == 0  # no refresh right after
    for canary in (CODE_CANARY, query["state"], NAME_CANARY, "access-1", "refresh-1", CLIENT_CANARY):
        assert canary not in caplog.text


@pytest.mark.parametrize("case", ["other_user", "garbage", "tampered", "expired"])
async def test_a_bad_state_is_refused_before_linear_is_called(case: str) -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = make_connections(linear, store, clock)
    state = consent_of(connections.authorize_url(OTHER if case == "other_user" else USER, "/x"))["state"]
    if case == "garbage":
        state = "a.b.c"
    if case == "tampered":
        state = state[:-2] + ("AA" if not state.endswith("AA") else "BB")
    if case == "expired":
        clock.seconds += 601

    with pytest.raises(LinearConnectError) as raised:
        await connections.connect(USER, code=CODE_CANARY, state=state)

    assert raised.value.code is ConnectFailure.STATE_INVALID and str(raised.value) == "linear_state_invalid"
    assert linear.calls == [] and store.rows == {}


async def test_a_code_with_another_pkce_verifier_or_a_used_code_is_refused() -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = make_connections(linear, store)
    query = consent_of(connections.authorize_url(USER, "/x"))
    linear.allow(CODE_CANARY, s256("a verifier of another consent"))

    with pytest.raises(LinearConnectError) as wrong:
        await connections.connect(USER, code=CODE_CANARY, state=query["state"])
    with pytest.raises(LinearConnectError) as used:
        await connections.connect(USER, code=CODE_CANARY, state=query["state"])

    assert wrong.value.code is ConnectFailure.CODE_INVALID and used.value.code is ConnectFailure.CODE_INVALID
    assert store.rows == {}


@pytest.mark.parametrize(
    ("status", "failure"),
    [(503, ConnectFailure.UNREACHABLE), (None, ConnectFailure.AUTH_FAILED)],
)
async def test_a_linear_outage_or_a_refused_app_secret_is_told_apart(status, failure) -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = make_connections(linear, store)
    query = consent_of(connections.authorize_url(USER, "/x"))
    linear.allow(CODE_CANARY, query["code_challenge"])
    linear.status = status
    if status is None:
        connections._client = LinearOAuthClient(
            "linear-client-1", SecretStr("wrong"), REDIRECT, transport=linear.transport()
        )

    with pytest.raises(LinearConnectError) as raised:
        await connections.connect(USER, code=CODE_CANARY, state=query["state"])

    assert raised.value.code is failure and store.rows == {}


async def test_a_second_connect_replaces_the_first() -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = await connected(linear, store)
    query = consent_of(connections.authorize_url(USER, "/x"))
    linear.allow("second-code", query["code_challenge"])

    await connections.connect(USER, code="second-code", state=query["state"])

    assert await connections.access_token(USER) == "access-2"
    assert len(store.rows) == 1


# Refresh -------------------------------------------------------------------------------------------------------------


async def test_a_refresh_rotates_both_tokens_and_commits_them_before_the_new_one_is_used() -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)
    clock.seconds += DAY - 30  # inside the 60 s early window

    fresh = await connections.access_token(USER)

    assert fresh == "access-2" and linear.refreshes == 1
    cipher = TokenCipher(SecretStr(TOKEN_KEY))
    row = store.rows[USER]
    assert cipher.decrypt(row.access_token).get_secret_value() == "access-2"
    assert cipher.decrypt(row.refresh_token).get_secret_value() == "refresh-2"
    assert row.expires_at == clock.now() + timedelta(seconds=DAY) and row.status == "active"
    assert store.events[-2:] == [("rotate", USER), ("commit", None)]
    assert await connections.access_token(USER) == "access-2" and linear.refreshes == 1  # cached


async def test_callers_that_wait_for_a_refresh_share_it() -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)
    clock.seconds += DAY

    tokens = await asyncio.gather(*(connections.access_token(USER) for _ in range(5)))

    assert set(tokens) == {"access-2"} and linear.refreshes == 1


async def test_a_token_that_another_process_already_refreshed_is_used_without_a_refresh() -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    first = await connected(linear, store, clock)
    clock.seconds += DAY
    assert await first.access_token(USER) == "access-2"  # the other process rotated the row

    cold = make_connections(linear, store, clock)  # this process has nothing cached

    assert await cold.access_token(USER) == "access-2" and linear.refreshes == 1


async def test_a_token_that_linear_refused_is_replaced_even_when_the_row_says_it_is_good() -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)

    assert await connections.refresh(USER, used="access-1") == "access-2"
    assert linear.refreshes == 1


@pytest.mark.parametrize("cause", ["invalid_grant", "rotated_key"])
async def test_a_dead_grant_expires_the_connection_once(cause: str, caplog) -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)
    clock.seconds += DAY
    if cause == "invalid_grant":
        linear.error = "invalid_grant"
    else:
        connections._cipher = TokenCipher(SecretStr(Fernet.generate_key().decode()))

    with caplog.at_level(logging.INFO), pytest.raises(LinearUnauthorizedError):
        await connections.access_token(USER)

    assert store.rows[USER].status == "expired"
    view = await connections.get(USER)
    assert view is not None and view.status == "expired"
    calls = len(linear.calls)
    with pytest.raises(LinearUnauthorizedError):
        await connections.access_token(USER)
    assert len(linear.calls) == calls and "linear.expired" in caplog.text


@pytest.mark.parametrize("status", [503, 401])
async def test_a_refresh_that_fails_for_another_reason_keeps_the_connection_active(status: int) -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)
    clock.seconds += DAY
    linear.status = status

    with pytest.raises(LinearUnavailableError):
        await connections.access_token(USER)

    assert store.rows[USER].status == "active"
    linear.status = None
    assert await connections.access_token(USER) == "access-2"  # the old refresh token is still the row's: retried


async def test_a_cached_token_is_not_served_after_another_process_ends_the_connection() -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = await connected(linear, store)
    assert await connections.access_token(USER) == "access-1"

    del store.rows[USER]

    with pytest.raises(LinearUnauthorizedError):
        await connections.access_token(USER)


async def test_the_token_provider_hands_the_gateway_a_fresh_token_each_time() -> None:
    linear, store, clock = FakeLinear(), MemoryStore(), Clock()
    connections = await connected(linear, store, clock)
    provider = connections.token_provider(USER)

    first = await provider()
    clock.seconds += DAY
    second = await provider()

    assert (first, second) == ("access-1", "access-2")
    assert connections.gateway(USER) is not connections.gateway(USER)


# Disconnect ----------------------------------------------------------------------------------------------------------


async def test_disconnect_deletes_the_row_and_revokes_both_tokens_at_linear() -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = await connected(linear, store)

    await connections.disconnect(USER)

    assert store.rows == {} and await connections.get(USER) is None
    assert sorted(linear.revoked) == [("access-1", "access_token"), ("refresh-1", "refresh_token")]
    with pytest.raises(LinearUnauthorizedError):
        await connections.access_token(USER)


@pytest.mark.parametrize("trouble", ["refused", "down"])
async def test_disconnect_still_deletes_when_linear_cannot_revoke(trouble: str) -> None:
    linear, store = FakeLinear(), MemoryStore()
    connections = await connected(linear, store)
    if trouble == "refused":
        linear.revoke_status = 400
    else:
        linear.status = 503

    await connections.disconnect(USER)

    assert store.rows == {}


async def test_disconnect_without_a_connection_calls_nothing() -> None:
    linear, store = FakeLinear(), MemoryStore()

    await make_connections(linear, store).disconnect(USER)

    assert linear.calls == []
