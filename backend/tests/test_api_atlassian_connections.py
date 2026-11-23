"""``AtlassianConnections`` (docs/jira-sync/oauth.md "Tokens", "Connect flow") over the fake ``db.atlassian``, a
scripted OAuth client and the real ``HttpJiraGateway`` on ``httpx.MockTransport``: the rotated refresh token is
committed before the new access token is used, concurrent refreshes make one token call, ``invalid_grant`` expires
the connection for good, the cache and the connect flow. No network; every value is a fake."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from typing import Any

import httpx
import pytest

from api.jira.connections import EARLY_EXPIRY_SECONDS, AtlassianConnections
from atlassian_support import (
    ACCOUNT_ID,
    ACCOUNT_NAME,
    CLIENT_CANARY,
    CLOUD,
    PUBLIC_FUNCTIONS,
    FakeAtlassianDb,
    FakeOAuthClient,
    oauth_settings,
)
from db import atlassian as atlassian_module
from jira.gateway import JiraError
from jira.http import HttpJiraGateway, TokenSource
from jira.oauth import OAuthCode, OAuthError
from jira.reasons import Reason

USER = uuid.UUID("8a1f4c3e-0000-4000-8000-000000000001")
OTHER = uuid.UUID("8a1f4c3e-0000-4000-8000-000000000002")
SECRETS = ("access-", "refresh-", "code-canary", ACCOUNT_ID, ACCOUNT_NAME, CLIENT_CANARY)


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """The connections over the fakes; ``jira_answers`` script the Jira calls of the gateways it builds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = FakeAtlassianDb()
        self.db.install(monkeypatch)
        self.client = FakeOAuthClient(self.db.events)
        self.clock = Monotonic()
        self.jira_answers: list[httpx.Response] = [httpx.Response(200, json={"accountId": "acc"})]
        self.connections = AtlassianConnections(
            oauth_settings(),
            transaction=self.db.transaction,
            client=self.client,
            gateway_factory=self.gateway,
            monotonic=self.clock,
        )

    def gateway(self, cloud_id: str, tokens: TokenSource) -> HttpJiraGateway:
        def handler(request: httpx.Request) -> httpx.Response:
            self.db.events.append(("jira", request.headers["Authorization"]))
            return self.jira_answers.pop(0) if len(self.jira_answers) > 1 else self.jira_answers[0]

        return HttpJiraGateway.oauth(cloud_id, tokens, transport=httpx.MockTransport(handler))


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    made = Rig(monkeypatch)
    yield made
    await made.connections.aclose()


def haystack(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in caplog.records)


def test_the_fake_repository_keeps_every_public_function_with_an_equal_signature() -> None:
    public = [
        name
        for name, member in inspect.getmembers(atlassian_module, inspect.iscoroutinefunction)
        if member.__module__ == atlassian_module.__name__ and not name.startswith("_")
    ]
    assert sorted(PUBLIC_FUNCTIONS) == sorted(public)
    for name in public:
        assert inspect.signature(getattr(FakeAtlassianDb(), name)) == inspect.signature(getattr(atlassian_module, name))


# The acting gateway ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "reason"), [(None, Reason.NOT_CONNECTED), ("expired", Reason.RECONNECT_REQUIRED)])
async def test_a_missing_or_expired_connection_cannot_act(rig, status, reason) -> None:
    if status is not None:
        rig.db.add(USER, status=status)

    with pytest.raises(JiraError) as raised:
        await rig.connections.gateway(USER)
    with pytest.raises(JiraError) as nobody:
        await rig.connections.gateway(None)

    assert raised.value.reason is reason and nobody.value.reason is Reason.NOT_CONNECTED
    assert rig.client.refreshes == []


async def test_an_active_connection_has_one_gateway_on_its_cloud(rig) -> None:
    rig.db.add(USER)

    first = await rig.connections.gateway(USER)

    assert await rig.connections.gateway(USER) is first
    assert repr(first) == f"HttpJiraGateway(site_url='https://api.atlassian.com/ex/jira/{CLOUD}/')"


# Tokens ------------------------------------------------------------------------------------------------------------


async def test_the_rotated_refresh_token_is_committed_before_the_new_access_token_is_used(rig, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rig.db.add(USER, refresh="refresh-0")
    gateway = await rig.connections.gateway(USER)

    await gateway.myself()

    assert rig.client.refreshes == ["refresh-0"]
    assert rig.db.refresh_token(USER) == "refresh-1"  # stored encrypted, rotated
    assert rig.db.events[-4:] == [
        ("token_call", "refresh-0"),
        ("rotate", USER),
        ("commit", None),
        ("jira", "Bearer access-1"),
    ]
    assert rig.db.locked == [USER]  # read FOR UPDATE
    assert rig.db.rows[USER].last_used_at is not None
    refreshed = [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.refreshed"]
    assert len(refreshed) == 1 and getattr(refreshed[0], "user_id", None) == str(USER)
    assert not [s for s in SECRETS if s in haystack(caplog)]


async def test_concurrent_callers_share_one_refresh(rig) -> None:
    rig.db.add(USER)
    rig.client.gate = asyncio.Event()

    calls = [asyncio.create_task(rig.connections.access_token(USER)) for _ in range(5)]
    await asyncio.sleep(0)
    rig.client.gate.set()
    tokens = await asyncio.gather(*calls)

    assert tokens == ["access-1"] * 5
    assert rig.client.refreshes == ["refresh-0"]


async def test_a_caller_cancelled_during_the_refresh_still_leaves_the_rotated_token_committed(rig) -> None:
    """Atlassian disables the old refresh token on every rotation: a cancelled caller (a pass stopped by a shutdown or
    a target change) must not roll the rotated one back."""

    rig.db.add(USER, refresh="refresh-0")
    rig.client.gate = asyncio.Event()
    caller = asyncio.create_task(rig.connections.access_token(USER))
    for _ in range(5):
        await asyncio.sleep(0)
    assert rig.client.refreshes == ["refresh-0"]  # the token call is under way at Atlassian

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    rig.client.gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert rig.db.refresh_token(USER) == "refresh-1"
    assert ("rollback", None) not in rig.db.events and rig.db.events[-2:] == [("rotate", USER), ("commit", None)]
    assert await rig.connections.access_token(USER) == "access-1"  # remembered: no second token call
    assert rig.client.refreshes == ["refresh-0"]


async def test_close_waits_for_a_refresh_whose_caller_was_cancelled(rig) -> None:
    rig.db.add(USER, refresh="refresh-0")
    rig.client.gate = asyncio.Event()
    caller = asyncio.create_task(rig.connections.access_token(USER))
    for _ in range(5):
        await asyncio.sleep(0)
    caller.cancel()
    closing = asyncio.create_task(rig.connections.aclose())
    for _ in range(5):
        await asyncio.sleep(0)

    assert not closing.done() and not rig.client.closed
    rig.client.gate.set()
    await asyncio.wait_for(closing, timeout=2)
    assert rig.db.refresh_token(USER) == "refresh-1" and rig.client.closed


async def test_the_access_token_is_reused_until_a_minute_before_it_expires(rig) -> None:
    rig.db.add(USER)
    assert await rig.connections.access_token(USER) == "access-1"

    rig.clock.now += rig.client.expires_in - EARLY_EXPIRY_SECONDS - 1
    assert await rig.connections.access_token(USER) == "access-1"
    rig.clock.now += 2
    assert await rig.connections.access_token(USER) == "access-2"

    assert rig.client.refreshes == ["refresh-0", "refresh-1"]


async def test_a_jira_401_refreshes_once_for_concurrent_callers_and_never_hands_out_the_refused_token(rig) -> None:
    rig.db.add(USER)
    used = await rig.connections.access_token(USER)

    again = await asyncio.gather(*(rig.connections.refresh(USER, used=used) for _ in range(3)))

    assert again == ["access-2"] * 3
    assert rig.client.refreshes == ["refresh-0", "refresh-1"]


@pytest.mark.parametrize("cause", ["invalid_grant", "key_rotated"])
async def test_a_dead_grant_expires_the_connection_and_it_is_never_refreshed_again(rig, caplog, cause) -> None:
    caplog.set_level(logging.DEBUG)
    rig.db.add(USER)
    if cause == "invalid_grant":
        rig.client.refuse = OAuthCode.GRANT_INVALID
    else:
        rig.db.rows[USER].refresh_token = b"ciphertext of another key"

    with pytest.raises(JiraError) as first:
        await rig.connections.access_token(USER)
    with pytest.raises(JiraError) as second:
        await rig.connections.access_token(USER)

    assert first.value.reason is second.value.reason is Reason.RECONNECT_REQUIRED
    assert rig.db.rows[USER].status == "expired" and rig.db.rows[USER].expired_at is not None
    assert len(rig.client.refreshes) == (1 if cause == "invalid_grant" else 0)
    assert rig.db.events.count(("expire", USER)) == 1 and ("commit", None) in rig.db.events
    expired = [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.expired"]
    assert len(expired) == 1 and getattr(expired[0], "code", None) == "jira_reconnect_required"
    with pytest.raises(JiraError) as acting:
        await rig.connections.gateway(USER)
    assert acting.value.reason is Reason.RECONNECT_REQUIRED


@pytest.mark.parametrize(
    ("code", "reason"), [(OAuthCode.UNREACHABLE, Reason.UNREACHABLE), (OAuthCode.REFUSED, Reason.AUTH_FAILED)]
)
async def test_another_refresh_failure_keeps_the_connection_active(rig, code, reason) -> None:
    rig.db.add(USER)
    rig.client.refuse = code

    with pytest.raises(JiraError) as raised:
        await rig.connections.access_token(USER)

    assert raised.value.reason is reason
    assert rig.db.rows[USER].status == "active" and rig.db.refresh_token(USER) == "refresh-0"


async def test_a_refresh_after_the_row_was_deleted_is_not_connected(rig) -> None:
    with pytest.raises(JiraError) as raised:
        await rig.connections.access_token(USER)

    assert raised.value.reason is Reason.NOT_CONNECTED and rig.client.refreshes == []


# Connect and disconnect --------------------------------------------------------------------------------------------


async def test_connect_checks_the_state_stores_the_connection_and_uses_the_new_access_token(rig, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rig.db.add(USER, status="expired")  # connecting again replaces the row
    state = rig.connections.authorize_url(USER, "/p/x").split("state=", 1)[1]

    view, return_to = await rig.connections.connect(USER, code="code-canary", state=state)
    await (await rig.connections.gateway(USER)).myself()

    assert return_to == "/p/x"
    assert (view.status, view.account_name, view.cloud_id) == ("active", ACCOUNT_NAME, CLOUD)
    row = rig.db.rows[USER]
    assert (row.status, row.account_id, row.account_name, row.scopes) == (
        "active",
        ACCOUNT_ID,
        ACCOUNT_NAME,
        "read:jira-work",
    )
    assert rig.db.refresh_token(USER) == "refresh-first"
    assert rig.db.events[-1] == ("jira", "Bearer access-first") and rig.client.refreshes == []
    assert [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.connected"]
    assert not [s for s in SECRETS if s in haystack(caplog)] and state not in haystack(caplog)


async def test_a_state_of_another_user_is_refused_before_any_atlassian_call(rig) -> None:
    state = rig.connections.authorize_url(OTHER, "/p/x").split("state=", 1)[1]

    with pytest.raises(OAuthError) as raised:
        await rig.connections.connect(USER, code="code-canary", state=state)

    assert raised.value.code is OAuthCode.STATE_INVALID
    assert rig.client.exchanges == [] and USER not in rig.db.rows


async def test_a_grant_without_the_site_keeps_no_connection(rig) -> None:
    rig.client.site_error = OAuthCode.SITE_NOT_GRANTED
    state = rig.connections.authorize_url(USER, "/p/x").split("state=", 1)[1]

    with pytest.raises(OAuthError) as raised:
        await rig.connections.connect(USER, code="code-canary", state=state)

    assert raised.value.code is OAuthCode.SITE_NOT_GRANTED and USER not in rig.db.rows


async def test_disconnect_deletes_the_row_and_drops_the_cached_access_token(rig, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    rig.db.add(USER)
    await rig.connections.access_token(USER)

    await rig.connections.disconnect(USER)

    assert USER not in rig.db.rows
    with pytest.raises(JiraError) as raised:
        await rig.connections.access_token(USER)
    assert raised.value.reason is Reason.NOT_CONNECTED
    assert [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.disconnected"]


async def test_close_closes_the_gateways_and_the_client(rig) -> None:
    rig.db.add(USER)
    gateway = await rig.connections.gateway(USER)

    await rig.connections.aclose()

    assert gateway._client.is_closed and rig.client.closed
