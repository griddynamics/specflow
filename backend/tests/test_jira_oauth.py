"""``jira.oauth`` and the bearer auth of ``jira.http`` on ``httpx.MockTransport``: the signed ``state``, the
refresh-token cipher, the token grants, the site's cloud, ``/myself``, the 401 refresh-once rule and the gateway's
base URL per mode. No network; every value is a fake."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from integrations.state import STATE_TTL_SECONDS
from jira.gateway import JiraError
from jira.http import HttpJiraGateway
from jira.oauth import (
    SCOPES,
    AtlassianOAuthClient,
    OAuthCode,
    OAuthError,
    StateSigner,
    TokenCipher,
    api_url,
)
from jira.reasons import Reason

KEY = SecretStr(Fernet.generate_key().decode())
SITE = "https://example.atlassian.net"
CLIENT_CANARY = "client-secret-canary-0b7d"  # the client secret
REDIRECT = "http://localhost:3001/api/integrations/atlassian/callback"
USER = "8a1f4c3e-0000-4000-8000-000000000001"
OTHER = "8a1f4c3e-0000-4000-8000-000000000002"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# State -----------------------------------------------------------------------------------------------------------


def test_a_state_verifies_for_its_user_within_ten_minutes_and_returns_the_return_to() -> None:
    clock = Clock()
    signer = StateSigner(KEY, clock=clock)
    state = signer.sign(USER, "/projects/p/settings?tab=jira")

    clock.now += STATE_TTL_SECONDS - 1

    assert state.count(".") == 2  # nonce.payload.signature
    assert signer.verify(state, USER) == "/projects/p/settings?tab=jira"
    assert signer.sign(USER, "/x") != signer.sign(USER, "/x")  # a fresh nonce each time


@pytest.mark.parametrize("case", ["expired", "other_user", "tampered", "other_key", "garbage", "two_parts"])
def test_a_bad_state_is_state_invalid(case: str) -> None:
    clock = Clock()
    signer = StateSigner(KEY, clock=clock)
    state = signer.sign(USER, "/x")
    nonce, payload, signature = state.split(".")
    user = USER
    if case == "expired":
        clock.now += STATE_TTL_SECONDS + 1
    elif case == "other_user":
        user = OTHER
    elif case == "tampered":
        forged = StateSigner(KEY, clock=clock).sign(OTHER, "/evil").split(".")[1]
        state = f"{nonce}.{forged}.{signature}"
    elif case == "other_key":
        state = StateSigner(SecretStr(Fernet.generate_key().decode()), clock=clock).sign(USER, "/x")
    elif case == "garbage":
        state = "a.b.c"
    else:
        state = f"{nonce}.{payload}"

    with pytest.raises(OAuthError) as raised:
        signer.verify(state, user)

    assert raised.value.code is OAuthCode.STATE_INVALID
    assert str(raised.value) == "atlassian_state_invalid"


def test_the_signer_and_the_cipher_hide_the_key_in_their_repr() -> None:
    assert KEY.get_secret_value() not in repr(StateSigner(KEY)) + repr(TokenCipher(KEY))


def test_the_cipher_round_trips_and_a_ciphertext_of_another_key_is_grant_invalid() -> None:
    cipher = TokenCipher(KEY)
    sealed = cipher.encrypt(SecretStr("refresh-canary"))

    assert b"refresh-canary" not in sealed
    assert cipher.decrypt(sealed).get_secret_value() == "refresh-canary"
    with pytest.raises(OAuthError) as raised:
        TokenCipher(SecretStr(Fernet.generate_key().decode())).decrypt(sealed)
    assert raised.value.code is OAuthCode.GRANT_INVALID


# The OAuth client --------------------------------------------------------------------------------------------------


class Atlassian:
    """Scripted ``auth.atlassian.com`` / ``api.atlassian.com``: ``routes`` maps ``METHOD host/path`` to responses."""

    def __init__(self, routes: dict[str, httpx.Response | list[httpx.Response] | Exception]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes[f"{request.method} {request.url.host}{request.url.path}"]
        if isinstance(route, list):
            route = route.pop(0)
        if isinstance(route, Exception):
            raise route
        return route

    def client(self) -> AtlassianOAuthClient:
        return AtlassianOAuthClient(
            "client-1", SecretStr(CLIENT_CANARY), REDIRECT, transport=httpx.MockTransport(self.handler)
        )


GRANTS = "POST auth.atlassian.com/oauth/token"
RESOURCES = "GET api.atlassian.com/oauth/token/accessible-resources"
GRANT = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, "scope": "read:jira-work"}


def test_the_authorize_url_asks_for_the_fixed_scopes_offline_access_and_carries_the_state() -> None:
    url = Atlassian({}).client().authorize_url("the-state")

    parts = urlsplit(url)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "auth.atlassian.com", "/authorize")
    assert query["audience"] == "api.atlassian.com"
    assert query["scope"].split(" ") == list(SCOPES) and "offline_access" in SCOPES
    assert (query["redirect_uri"], query["state"], query["response_type"]) == (REDIRECT, "the-state", "code")
    assert CLIENT_CANARY not in url


async def test_the_code_exchange_posts_the_code_and_the_client_and_keeps_the_tokens_secret() -> None:
    atlassian = Atlassian({GRANTS: httpx.Response(200, json=GRANT)})
    client = atlassian.client()

    grant = await client.exchange("code-1")
    await client.aclose()

    body = json.loads(atlassian.requests[0].content)
    assert body == {
        "client_id": "client-1",
        "client_secret": CLIENT_CANARY,
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": REDIRECT,
    }
    assert grant.access_token.get_secret_value() == "access-1" and grant.expires_in == 3600
    assert "access-1" not in repr(grant) and "refresh-1" not in repr(grant)


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (
            httpx.Response(403, json={"error": "invalid_grant", "error_description": "Bad code"}),
            "atlassian_code_invalid",
        ),
        (httpx.Response(401, json={"error": "access_denied"}), "atlassian_code_invalid"),
        (httpx.Response(503), "atlassian_unreachable"),
        (httpx.ConnectError("refused"), "atlassian_unreachable"),
    ],
)
async def test_a_refused_code_exchange_is_one_closed_code_without_atlassian_text(answer, code) -> None:
    client = Atlassian({GRANTS: answer}).client()

    with pytest.raises(OAuthError) as raised:
        await client.exchange("code-1")
    await client.aclose()

    assert str(raised.value) == code and "Bad code" not in repr(raised.value)


async def test_a_refresh_waits_10_seconds_for_atlassian_and_the_connect_flow_30() -> None:
    """A refresh holds the connection row's lock: a hung token endpoint must not hold it for long."""

    atlassian = Atlassian({GRANTS: [httpx.Response(200, json=GRANT), httpx.Response(200, json=GRANT)]})
    client = atlassian.client()

    await client.refresh(SecretStr("refresh-0"))
    await client.exchange("code-1")
    await client.aclose()

    refresh, exchange = (request.extensions["timeout"] for request in atlassian.requests)
    assert set(refresh.values()) == {10} and set(exchange.values()) == {30}


@pytest.mark.parametrize("status", [400, 403])
async def test_invalid_grant_on_a_refresh_is_grant_invalid_whatever_the_status(status: int) -> None:
    client = Atlassian({GRANTS: httpx.Response(status, json={"error": "invalid_grant"})}).client()

    with pytest.raises(OAuthError) as raised:
        await client.refresh(SecretStr("refresh-0"))
    await client.aclose()

    assert raised.value.code is OAuthCode.GRANT_INVALID


async def test_another_refusal_of_a_refresh_is_refused_not_a_dead_grant() -> None:
    client = Atlassian({GRANTS: httpx.Response(401, json={"error": "invalid_client"})}).client()

    with pytest.raises(OAuthError) as raised:
        await client.refresh(SecretStr("refresh-0"))
    await client.aclose()

    assert raised.value.code is OAuthCode.REFUSED


async def test_the_cloud_of_the_site_is_found_by_its_url_and_a_grant_without_it_is_site_not_granted() -> None:
    resources = [
        {"id": "cloud-other", "url": "https://other.atlassian.net"},
        {"id": "cloud-1", "url": f"{SITE}/"},
    ]
    atlassian = Atlassian({RESOURCES: [httpx.Response(200, json=resources), httpx.Response(200, json=resources[:1])]})
    client = atlassian.client()

    assert await client.cloud_id(SecretStr("access-1"), SITE) == "cloud-1"
    with pytest.raises(OAuthError) as raised:
        await client.cloud_id(SecretStr("access-1"), SITE)
    await client.aclose()

    assert raised.value.code is OAuthCode.SITE_NOT_GRANTED
    assert atlassian.requests[0].headers["Authorization"] == "Bearer access-1"


async def test_the_account_is_read_from_myself_on_the_cloud_and_kept_out_of_its_repr() -> None:
    route = "GET api.atlassian.com/ex/jira/cloud-1/rest/api/3/myself"
    atlassian = Atlassian({route: httpx.Response(200, json={"accountId": "acc-canary", "displayName": "Ada Canary"})})
    client = atlassian.client()

    account = await client.account(SecretStr("access-1"), "cloud-1")
    await client.aclose()

    assert (account.account_id, account.name) == ("acc-canary", "Ada Canary")
    assert "acc-canary" not in repr(account) and "Ada Canary" not in repr(account)


# Bearer auth and the gateway base URL ------------------------------------------------------------------------------


class Tokens:
    """A ``TokenSource``: ``access-1`` first; each refresh answers the next number."""

    def __init__(self, *, refuse: Reason | None = None) -> None:
        self.number = 1
        self.refreshed_after: list[str] = []
        self.refuse = refuse

    async def access_token(self) -> str:
        if self.refuse is not None:
            raise JiraError(self.refuse)
        return f"access-{self.number}"

    async def refreshed(self, used: str) -> str:
        self.refreshed_after.append(used)
        self.number += 1
        return f"access-{self.number}"


def jira(
    answers: list[httpx.Response], seen: list[httpx.Request], auth: list[str] | None = None
) -> httpx.MockTransport:
    """Jira answering ``answers`` in turn (the last repeats); ``auth`` gets each request's header as it was sent (the
    retry sends the same request object again)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if auth is not None:
            auth.append(request.headers["Authorization"])
        return answers.pop(0) if len(answers) > 1 else answers[0]

    return httpx.MockTransport(handler)


async def test_the_oauth_gateway_calls_the_cloud_through_api_atlassian_com_with_the_bearer_token() -> None:
    seen: list[httpx.Request] = []
    gateway = HttpJiraGateway.oauth(
        "cloud-1", Tokens(), transport=jira([httpx.Response(200, json={"accountId": "a"})], seen)
    )

    await gateway.myself()
    await gateway.aclose()

    assert str(seen[0].url) == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/myself"
    assert seen[0].headers["Authorization"] == "Bearer access-1"
    assert api_url("cloud-1") == "https://api.atlassian.com/ex/jira/cloud-1"
    assert "access-1" not in repr(gateway)


async def test_the_token_gateway_keeps_basic_auth_on_the_site() -> None:
    seen: list[httpx.Request] = []
    gateway = HttpJiraGateway(
        SITE, "svc@example.com", SecretStr("t"), transport=jira([httpx.Response(200, json={})], seen)
    )

    await gateway.myself()
    await gateway.aclose()

    assert str(seen[0].url) == f"{SITE}/rest/api/3/myself"
    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_a_401_refreshes_once_and_retries_the_call_once() -> None:
    seen: list[httpx.Request] = []
    sent: list[str] = []
    tokens = Tokens()
    answers = [httpx.Response(401), httpx.Response(200, json={"accountId": "a"})]
    gateway = HttpJiraGateway.oauth("cloud-1", tokens, transport=jira(answers, seen, sent))

    assert await gateway.myself() == "a"
    await gateway.aclose()

    assert sent == ["Bearer access-1", "Bearer access-2"]
    assert tokens.refreshed_after == ["access-1"]


async def test_a_second_401_is_jira_auth_failed_after_one_refresh(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    tokens = Tokens()
    gateway = HttpJiraGateway.oauth("cloud-1", tokens, transport=jira([httpx.Response(401)], seen))

    with pytest.raises(JiraError) as raised:
        await gateway.myself()
    await gateway.aclose()

    assert raised.value.reason is Reason.AUTH_FAILED
    assert len(seen) == 2 and tokens.refreshed_after == ["access-1"]
    assert "access-1" not in caplog.text and "access-2" not in caplog.text


async def test_a_token_source_that_cannot_act_ends_the_call_with_its_reason_and_no_request() -> None:
    seen: list[httpx.Request] = []
    gateway = HttpJiraGateway.oauth(
        "cloud-1", Tokens(refuse=Reason.RECONNECT_REQUIRED), transport=jira([httpx.Response(200)], seen)
    )

    with pytest.raises(JiraError) as raised:
        await gateway.myself()
    await gateway.aclose()

    assert raised.value.reason is Reason.RECONNECT_REQUIRED and seen == []
