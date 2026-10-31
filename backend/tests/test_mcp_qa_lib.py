"""Offline tests of the MCP QA library (`examples/mcp_qa_lib.py`, docs/mcp-server/testing.md).

External boundaries only are faked: Keycloak's HTTP API through `httpx.MockTransport` (one `FakeKeycloak` handler for
every test) and the `claude` CLI through the `CommandRunner` seam. Terminal handling runs against a real
`sys.executable` child. Nothing here needs the network, Keycloak, Docker or `claude`.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import gc
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import anyio
import httpx
import httpx2
import jwt
import pydantic
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent
from pydantic import BaseModel, SecretStr

import mcp_qa_lib
from db.models import ProjectMember
from graph.core import load_ontology
from mcp_qa_lib import (
    _CALL_ERRORS,
    CLAUDE_DISALLOWED_TOOLS,
    ISSUER,
    KEYCLOAK_URL,
    MCP_CLIENT_ID,
    MCP_REDIRECT_URI,
    MCP_RESOURCE_URL,
    PERSONA_CHECKS,
    PRM_URL,
    QA_USERS,
    REPORTS_DIR,
    WEB_CLIENT_ID,
    FixtureIds,
    KeycloakAdmin,
    LatencyProgress,
    LatencyStats,
    MemoryTokenStorage,
    PromptRun,
    QaError,
    RunPreconditionError,
    RunReport,
    RunTotals,
    ScenarioResult,
    TeardownVerdict,
    ToolEvent,
    ToolOutcome,
    _add_server,
    _timed,
    all_projects,
    alpha_features_listed,
    apply_local_defaults,
    beta_refused,
    call,
    capture_command,
    check_run,
    child_env,
    claude_add_argv,
    claude_lists,
    claude_login,
    claude_login_argv,
    claude_logout_argv,
    claude_prompt_argv,
    claude_remove_argv,
    claude_signed_in,
    claude_tool_names,
    complete_login,
    create_project_rest,
    dev_token,
    fetch_prm,
    find_authorization_url,
    fixture_ids,
    fixture_nodes,
    foreign_tool_calls,
    headers_json,
    mcp_session,
    narrow_scope,
    neo4j_driver,
    new_run_id,
    oauth_provider,
    parse_stream_json,
    percentile,
    persona_prompts,
    pkce_pair,
    port_is_free,
    post_mcp,
    preregistered_client,
    registered_server,
    remove_member,
    report_note,
    require_env,
    restore_member,
    run_command,
    run_latency,
    run_latency_into,
    run_prompt,
    run_pty,
    run_scenario,
    save_report,
    seeded_ids,
    server_name,
    signed_in_claude,
    stack_mode,
    strip_ansi,
    tool_json,
    user_token,
    write_report,
)

ROOT = Path(__file__).resolve().parents[2]
REALM_FILE = ROOT / "config" / "keycloak" / "specflow-realm.json"
CREDENTIAL = SecretStr("qa-credential-0001")
ACCESS = "access-0001"
ADMIN_BEARER = "admin-bearer-0001"
AUTH_PATH = "/realms/specflow/protocol/openid-connect/auth"
CODE_EXCHANGE_PATH = "/realms/specflow/protocol/openid-connect/token"
LOGIN_PATH = "/realms/specflow/login-actions/authenticate"
CONSENT_PATH = "/realms/specflow/login-actions/consent"
ADMIN_PATH = "/admin/realms/specflow"
COOKIES = ("AUTH_SESSION_ID=abc; Path=/; Secure; SameSite=None", "KC_RESTART=def; Path=/; Secure")
FORM = '<form id="kc" action="{action}" method="post"><input name="{field}"></form>'


def form_of(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode(), keep_blank_values=True))


def json_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


class FakeKeycloak:
    """Just enough of Keycloak for the login, token and admin calls."""

    accepted: SecretStr = CREDENTIAL
    extra_redirects = 0
    consent = False
    state: str | None = None
    iss = ISSUER
    admin_status = 200
    code_in_redirect = True
    token_status = 200
    location_on_create = True
    client_put_status = 204

    def __init__(self, *, users: dict[str, str] | None = None, **settings: Any) -> None:
        for name, value in settings.items():
            if not hasattr(type(self), name):
                raise TypeError(f"unknown FakeKeycloak setting {name}")
            setattr(self, name, value)
        self.users = {} if users is None else users
        self.requests: list[httpx.Request] = []
        self.authorize_query: dict[str, str] = {}
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = [
            ("GET", AUTH_PATH, self.authorize),
            ("GET", r"/hop/\d+", self.hop),
            ("GET", LOGIN_PATH, self.login_form),
            ("POST", LOGIN_PATH, self.login_post),
            ("POST", CONSENT_PATH, self.consent_post),
            ("POST", CODE_EXCHANGE_PATH, self.token),
            ("POST", "/realms/master/protocol/openid-connect/token", self.admin_login),
            ("GET", f"{ADMIN_PATH}/users", self.find_user),
            ("POST", f"{ADMIN_PATH}/users", self.create_user),
            ("PUT", f"{ADMIN_PATH}/users/[^/]+", lambda _request: httpx.Response(204)),
            ("PUT", f"{ADMIN_PATH}/users/[^/]+/reset-password", lambda _request: httpx.Response(204)),
            ("GET", f"{ADMIN_PATH}/clients", lambda _request: httpx.Response(200, json=[{"id": "client-1"}])),
            ("GET", f"{ADMIN_PATH}/clients/client-1", lambda _request: httpx.Response(200, json={"clientId": "c"})),
            ("PUT", f"{ADMIN_PATH}/clients/client-1", lambda _request: httpx.Response(self.client_put_status)),
        ]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, pattern, handler in self.routes:
            if request.method == method and re.fullmatch(pattern, request.url.path):
                return handler(request)
        return httpx.Response(404)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def redirect(self, location: str, *, cookies: bool = True) -> httpx.Response:
        headers = [("location", location), *(("set-cookie", cookie) for cookie in COOKIES if cookies)]
        return httpx.Response(302, headers=headers)

    def authorize(self, request: httpx.Request) -> httpx.Response:
        self.authorize_query = dict(request.url.params)
        return self.redirect(f"{KEYCLOAK_URL}/hop/1" if self.extra_redirects else f"{KEYCLOAK_URL}{LOGIN_PATH}")

    def hop(self, request: httpx.Request) -> httpx.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        last = number >= self.extra_redirects
        return self.redirect(f"{KEYCLOAK_URL}{LOGIN_PATH}" if last else f"{KEYCLOAK_URL}/hop/{number + 1}")

    def authenticated(self, request: httpx.Request) -> bool:
        return "AUTH_SESSION_ID=abc" in request.headers.get("cookie", "")

    def login_form(self, request: httpx.Request) -> httpx.Response:
        if not self.authenticated(request):
            return httpx.Response(400)
        return httpx.Response(200, text=FORM.format(action=f"{KEYCLOAK_URL}{LOGIN_PATH}?a=1&amp;b=2", field="password"))

    def login_post(self, request: httpx.Request) -> httpx.Response:
        if not self.authenticated(request):
            return httpx.Response(400)
        if form_of(request).get("password") != self.accepted.get_secret_value():
            return self.login_form(request)
        if self.consent:
            return httpx.Response(200, text=FORM.format(action=f"{KEYCLOAK_URL}{CONSENT_PATH}", field="accept"))
        return self.callback()

    def consent_post(self, request: httpx.Request) -> httpx.Response:
        return self.callback() if form_of(request) == {"accept": "Yes"} else httpx.Response(400)

    def callback(self) -> httpx.Response:
        state = self.authorize_query["state"] if self.state is None else self.state
        code = "code=code-1&" if self.code_in_redirect else ""
        return self.redirect(f"{self.authorize_query['redirect_uri']}?{code}state={state}&iss={self.iss}")

    def token(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.token_status, json={"access_token": ACCESS})

    def admin_login(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.admin_status, json={"access_token": ADMIN_BEARER})

    def find_user(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {ADMIN_BEARER}":
            return httpx.Response(401)
        found = self.users.get(request.url.params["email"])
        return httpx.Response(200, json=[{"id": found}] if found else [])

    def create_user(self, request: httpx.Request) -> httpx.Response:
        self.users[json_of(request)["email"]] = "created-1"
        location = {"location": f"{KEYCLOAK_URL}{ADMIN_PATH}/users/created-1"} if self.location_on_create else {}
        return httpx.Response(201, headers=location)


# NFR-MCP-05 and the identity provider setup ---------------------------------------------------------------------


def walk_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in walk_keys(value)}
    if isinstance(node, list):
        return {key for value in node for key in walk_keys(value)}
    return set()


def realm_clients() -> dict[str, dict[str, Any]]:
    realm = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    return {client["clientId"]: client for client in realm["clients"]}


def test_realm_file_has_no_users_secret_or_credentials_nfr_mcp_05() -> None:
    """NFR-MCP-05: the realm file holds no credential and no user."""

    realm = json.loads(REALM_FILE.read_text(encoding="utf-8"))

    assert not walk_keys(realm) & {"secret", "credentials", "users"}


def test_realm_file_matches_the_identity_provider_setup() -> None:
    """deployment.md#identity-provider-setup, items 1 to 6, with the local values."""

    realm = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    clients = realm_clients()
    mcp, web = clients["specflow-mcp"], clients[WEB_CLIENT_ID]

    assert realm["realm"] == "specflow"
    assert 300 <= realm["accessTokenLifespan"] <= 600
    assert realm["offlineSessionMaxLifespanEnabled"] is True
    assert realm["offlineSessionMaxLifespan"] >= 30 * 24 * 3600
    assert mcp["publicClient"] is True
    assert mcp["standardFlowEnabled"] is True
    assert not any(mcp[flag] for flag in ("implicitFlowEnabled", "directAccessGrantsEnabled", "serviceAccountsEnabled"))
    assert mcp["attributes"]["pkce.code.challenge.method"] == "S256"
    assert mcp["redirectUris"] == ["http://localhost:8765/callback", "http://127.0.0.1:8765/callback"]
    assert mcp["webOrigins"] == []
    assert web["publicClient"] is False
    assert web["redirectUris"] == ["http://localhost:3001/api/auth/callback/oidc"]
    audiences = {client: definition["protocolMappers"][0]["config"] for client, definition in clients.items()}
    assert audiences["specflow-mcp"]["included.custom.audience"] == MCP_RESOURCE_URL
    assert audiences[WEB_CLIENT_ID]["included.custom.audience"] == "specflow-api"
    assert all(config["access.token.claim"] == "true" for config in audiences.values())
    assert all(config["id.token.claim"] == "false" for config in audiences.values())


# Terminal output and stream-json --------------------------------------------------------------------------------

AUTH_URL = f"{ISSUER}/protocol/openid-connect/auth?client_id=specflow-mcp&state=s1"


@pytest.mark.parametrize("terminator", ["\x07", "\x1b\\"], ids=["bel", "st"])
def test_find_authorization_url_reads_osc8_with_bel_and_st(terminator: str) -> None:
    output = f"Open:\r\n\x1b]8;id=1;{AUTH_URL}{terminator}link\x1b]8;;{terminator}\r\n"

    assert find_authorization_url(output) == AUTH_URL


def test_find_authorization_url_falls_back_to_a_terminated_plain_url() -> None:
    assert find_authorization_url(f"visit {AUTH_URL}\r\nthen paste") == AUTH_URL


@pytest.mark.parametrize(
    "output",
    [
        "no link here",
        f"visit {AUTH_URL}",  # cut at a chunk boundary: no terminator yet
        f"\x1b]8;;{AUTH_URL}",  # OSC 8 without its BEL or ST
        "\x1b]8;;http://elsewhere.example/auth\x07x\x1b]8;;\x07 http://elsewhere.example/auth ",
    ],
    ids=["none", "truncated-plain", "truncated-osc8", "not-the-issuer"],
)
def test_find_authorization_url_returns_none_when_it_cannot_be_trusted(output: str) -> None:
    assert find_authorization_url(output) is None


def test_parse_stream_json_extracts_tool_calls_and_results() -> None:
    lines = [
        "not json at all",
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "list_projects", "input": {"a": 1}}]},
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": True}]
                },
            }
        ),
        json.dumps([1, 2]),
    ]

    call, result = parse_stream_json(lines)

    assert (call.kind, call.tool_use_id, call.name, call.payload, call.is_error) == (
        "call",
        "t1",
        "list_projects",
        {"a": 1},
        False,
    )
    assert (result.kind, result.name, result.payload, result.is_error) == ("result", "list_projects", "ok", True)
    assert parse_stream_json([]) == []


# Login helper ---------------------------------------------------------------------------------------------------


def authorize_url(redirect_uri: str = "http://localhost:8765/callback") -> str:
    return f"{AUTH_URL}&redirect_uri={redirect_uri}"


async def test_complete_login_posts_credentials_and_forwards_secure_cookies() -> None:
    fake = FakeKeycloak()

    redirected = await complete_login(authorize_url(), QA_USERS["alice"], CREDENTIAL, transport=fake.transport)

    assert redirected.startswith("http://localhost:8765/callback?code=code-1&state=s1")
    (post,) = fake.sent("POST", LOGIN_PATH)
    assert form_of(post) == {
        "username": QA_USERS["alice"],
        "password": CREDENTIAL.get_secret_value(),
        "credentialId": "",
    }
    assert "AUTH_SESSION_ID=abc" in post.headers["cookie"]
    assert "KC_RESTART=def" in post.headers["cookie"]
    assert post.url.query == b"a=1&b=2"  # the form action was unescaped
    assert not fake.sent("GET", "/callback")  # the redirect target is never requested


async def test_complete_login_follows_intermediate_redirects_and_a_consent_form() -> None:
    fake = FakeKeycloak(extra_redirects=2, consent=True)

    redirected = await complete_login(
        authorize_url(), QA_USERS["bob"], CREDENTIAL, transport=fake.transport, max_hops=10
    )

    assert "code=code-1" in redirected
    assert len(fake.sent("GET", "/hop/1")) == len(fake.sent("GET", "/hop/2")) == 1
    assert len(fake.sent("POST", CONSENT_PATH)) == 1


async def test_complete_login_serves_the_web_callback_too() -> None:
    fake = FakeKeycloak()
    web_callback = "http://localhost:3001/api/auth/callback/oidc"

    redirected = await complete_login(
        authorize_url(web_callback), QA_USERS["bob"], CREDENTIAL, transport=fake.transport
    )

    assert redirected.startswith(web_callback)


async def test_complete_login_raises_on_a_second_password_form_without_leaking_values() -> None:
    fake = FakeKeycloak(accepted=SecretStr("another-credential"))

    with pytest.raises(QaError) as raised:
        await complete_login(authorize_url(), QA_USERS["alice"], CREDENTIAL, transport=fake.transport)

    message = str(raised.value)
    assert "credentials rejected" in message
    assert CREDENTIAL.get_secret_value() not in message
    assert QA_USERS["alice"] not in message
    assert "http://" not in message


async def test_complete_login_raises_after_max_hops() -> None:
    fake = FakeKeycloak(extra_redirects=10)

    with pytest.raises(QaError, match="no redirect after 3 hops"):
        await complete_login(authorize_url(), QA_USERS["alice"], CREDENTIAL, transport=fake.transport, max_hops=3)


@pytest.mark.parametrize(
    ("url", "page", "message"),
    [
        (AUTH_URL, None, "no redirect_uri"),
        (authorize_url(), "<html>nothing</html>", "no form on the page"),
        (authorize_url(), FORM.format(action="/x", field="other"), "unexpected form"),
    ],
)
async def test_complete_login_names_the_failed_step(url: str, page: str | None, message: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text=page or ""))

    with pytest.raises(QaError, match=message):
        await complete_login(url, QA_USERS["alice"], CREDENTIAL, transport=transport)


# Token helper ---------------------------------------------------------------------------------------------------


async def test_user_token_sends_the_pkce_verifier_for_the_mcp_client() -> None:
    fake = FakeKeycloak()

    token = await user_token("alice", CREDENTIAL, transport=fake.transport)

    assert token == ACCESS
    (request,) = fake.sent("POST", CODE_EXCHANGE_PATH)
    form = form_of(request)
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert fake.authorize_query["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert fake.authorize_query["code_challenge_method"] == "S256"
    assert fake.authorize_query["client_id"] == "specflow-mcp"
    assert (form["client_id"], form["code"], form["grant_type"]) == ("specflow-mcp", "code-1", "authorization_code")
    assert "authorization" not in request.headers
    assert form_of(fake.sent("POST", LOGIN_PATH)[0])["username"] == QA_USERS["alice"]


async def test_user_token_uses_basic_auth_and_the_web_callback_for_the_web_client() -> None:
    fake = FakeKeycloak()
    web_secret = SecretStr("web-credential-0001")

    await user_token("bob", CREDENTIAL, client="web", web_secret=web_secret, transport=fake.transport)

    (request,) = fake.sent("POST", CODE_EXCHANGE_PATH)
    expected = base64.b64encode(f"{WEB_CLIENT_ID}:{web_secret.get_secret_value()}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert fake.authorize_query["redirect_uri"] == "http://localhost:3001/api/auth/callback/oidc"
    assert "code_verifier" in form_of(request)


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeKeycloak(state="forged"), "state mismatch"),
        (FakeKeycloak(iss="http://elsewhere.example/realms/x"), "issuer mismatch"),
    ],
    ids=["state", "iss"],
)
async def test_user_token_checks_state_and_issuer_before_asking_for_a_token(fake: FakeKeycloak, message: str) -> None:
    with pytest.raises(QaError, match=message):
        await user_token("alice", CREDENTIAL, transport=fake.transport)

    assert not fake.sent("POST", CODE_EXCHANGE_PATH)


async def test_user_token_web_client_needs_its_secret() -> None:
    with pytest.raises(QaError, match="needs its secret"):
        await user_token("alice", CREDENTIAL, client="web", transport=FakeKeycloak().transport)


# Keycloak admin -------------------------------------------------------------------------------------------------


async def test_keycloak_admin_ensure_user_creates_a_missing_user_with_both_names() -> None:
    fake = FakeKeycloak()

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=fake.transport) as admin:
        user_id = await admin.ensure_user("alice", CREDENTIAL)

    assert user_id == "created-1"
    (created,) = fake.sent("POST", f"{ADMIN_PATH}/users")
    assert json_of(created) == {
        "username": QA_USERS["alice"],
        "email": QA_USERS["alice"],
        "emailVerified": True,
        "enabled": True,
        "firstName": "Alice",
        "lastName": "QA",
        "requiredActions": [],
    }
    (reset,) = fake.sent("PUT", f"{ADMIN_PATH}/users/created-1/reset-password")
    assert json_of(reset) == {"type": "password", "value": CREDENTIAL.get_secret_value(), "temporary": False}


async def test_keycloak_admin_ensure_user_is_idempotent_for_a_found_user() -> None:
    fake = FakeKeycloak(users={QA_USERS["bob"]: "user-9"})

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=fake.transport) as admin:
        first = await admin.ensure_user("bob", CREDENTIAL)
        second = await admin.ensure_user("bob", CREDENTIAL)
        assert await admin.user_id("carol") is None

    assert first == second == "user-9"
    assert not fake.sent("POST", f"{ADMIN_PATH}/users")
    assert len(fake.sent("PUT", f"{ADMIN_PATH}/users/user-9")) == 2
    assert len(fake.sent("PUT", f"{ADMIN_PATH}/users/user-9/reset-password")) == 2
    assert len(fake.sent("POST", "/realms/master/protocol/openid-connect/token")) == 1  # one admin token per client


async def test_keycloak_admin_errors_name_the_status_only() -> None:
    admin_secret = SecretStr("admin-credential")
    fake = FakeKeycloak(admin_status=401)

    async with KeycloakAdmin(admin_secret, transport=fake.transport) as admin:
        with pytest.raises(QaError) as raised:
            await admin.ensure_user("alice", CREDENTIAL)

    assert str(raised.value) == "admin login failed: HTTP 401"
    assert admin_secret.get_secret_value() not in str(raised.value)


async def test_keycloak_admin_set_client_secret_puts_the_representation_with_the_secret() -> None:
    fake = FakeKeycloak()
    web_secret = SecretStr("web-credential-0001")

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=fake.transport) as admin:
        await admin.set_client_secret(WEB_CLIENT_ID, web_secret)

    (put,) = fake.sent("PUT", f"{ADMIN_PATH}/clients/client-1")
    assert json_of(put) == {"clientId": "c", "secret": web_secret.get_secret_value()}


async def test_keycloak_admin_set_client_secret_names_a_missing_client() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=[] if "/clients" in request.url.path else {"access_token": "t"})
    )

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=transport) as admin:
        with pytest.raises(QaError, match="find client failed: not found"):
            await admin.set_client_secret("nope", SecretStr("x"))


# Dev tokens -----------------------------------------------------------------------------------------------------

SIGNING_KEY = SecretStr("k" * 40)


@pytest.mark.parametrize(
    ("client", "audience", "azp"),
    [("mcp", MCP_RESOURCE_URL, "specflow-mcp"), ("web", "specflow-api", "specflow-web")],
)
def test_dev_token_claims_for_mcp_and_web(client: str, audience: str, azp: str) -> None:
    email = QA_USERS["alice"]

    token = dev_token(email, SIGNING_KEY, client=client, now=1_700_000_000)

    claims = jwt.decode(
        token, SIGNING_KEY.get_secret_value(), algorithms=["HS256"], audience=audience, options={"verify_exp": False}
    )
    assert claims == {
        "iss": "specflow-dev-login",
        "aud": audience,
        "azp": azp,
        "typ": "Bearer",
        "sub": f"dev:{email}",
        "email": email,
        "email_verified": True,
        "name": "alice",
        "iat": 1_700_000_000,
        "exp": 1_700_000_000 + 28800,
    }


def test_dev_token_defaults_to_the_current_time() -> None:
    token = dev_token(QA_USERS["bob"], SIGNING_KEY)

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 28800


def test_headers_json_shape() -> None:
    assert json.loads(headers_json("abc.def.ghi")) == {"Authorization": "Bearer abc.def.ghi"}


# Seed fixture ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("P-Alpha", {"Feature": 7, "Module": 2, "FunctionalRequirement": 3}),
        ("P-Beta", {"Feature": 3}),
        ("P-Empty", {}),
        ("P-Large", {"Feature": 300}),
    ],
)
def test_fixture_nodes_are_counted_per_label(project: str, expected: dict[str, int]) -> None:
    nodes = fixture_nodes(project, "project-id", load_ontology())

    assert dict(Counter(node.label for node in nodes)) == expected


def test_fixture_nodes_are_deterministic_prefixed_and_unique() -> None:
    ontology = load_ontology()
    prefixes = {"Feature": "FEAT::", "Module": "MOD::", "FunctionalRequirement": "REQ::FR::"}

    first = fixture_nodes("P-Alpha", "project-a", ontology)

    assert first == fixture_nodes("P-Alpha", "project-a", ontology)
    assert all(node.spec_id.startswith(prefixes[node.label]) for node in first)
    assert all(node.properties["spec_id"] == node.spec_id for node in first)
    assert len({node.uuid for node in first}) == len({node.spec_id for node in first}) == len(first)
    other = fixture_nodes("P-Alpha", "project-b", ontology)
    assert [n.spec_id for n in other] == [n.spec_id for n in first]
    assert not {n.uuid for n in other} & {n.uuid for n in first}
    large = [n.spec_id for n in fixture_nodes("P-Large", "project-c", ontology)]
    assert large == sorted(large)  # zero padding keeps spec id order stable for paging


# Claude plumbing: server registration ---------------------------------------------------------------------------


class RecordingRunner:
    """The `CommandRunner` seam: records each `claude mcp <step>` and fails the steps named in `fail`."""

    def __init__(self, *fail: str) -> None:
        self.fail = set(fail)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str], _cwd: Path) -> int:
        self.calls.append(list(argv))
        return 1 if argv[2] in self.fail else 0

    @property
    def steps(self) -> list[str]:
        return [argv[2] for argv in self.calls]


@pytest.fixture
def free_callback_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket boundary: these tests must not depend on whether 8765 is free on the machine."""

    monkeypatch.setattr(mcp_qa_lib, "port_is_free", lambda *_: True)


@pytest.mark.usefixtures("free_callback_port")
async def test_registered_server_adds_yields_and_tears_down_in_order(tmp_path: Path) -> None:
    runner = RecordingRunner()

    async with registered_server("run1", cwd=tmp_path, runner=runner) as name:
        assert name == "specflow-qa-run1"
        assert runner.steps == ["add"]

    assert runner.steps == ["add", "logout", "remove"]
    assert runner.calls[0][-2:] == ["specflow-qa-run1", MCP_RESOURCE_URL]
    assert "specflow-mcp" in runner.calls[0]
    assert runner.calls[2][-2:] == ["--scope", "local"]


@pytest.mark.usefixtures("free_callback_port")
async def test_registered_server_tears_down_when_the_body_fails_and_its_error_wins(tmp_path: Path) -> None:
    runner = RecordingRunner("logout")

    with pytest.raises(ValueError, match="login step failed"):
        async with registered_server("run2", cwd=tmp_path, runner=runner):
            raise ValueError("login step failed")

    assert runner.steps == ["add", "logout", "remove"]


@pytest.mark.usefixtures("free_callback_port")
async def test_registered_server_reports_a_failed_teardown_step_after_trying_both(tmp_path: Path) -> None:
    runner = RecordingRunner("logout")

    with pytest.raises(QaError, match="claude teardown failed: logout"):
        async with registered_server("run3", cwd=tmp_path, runner=runner):
            pass

    assert runner.steps == ["add", "logout", "remove"]


@pytest.mark.usefixtures("free_callback_port")
async def test_registered_server_still_tears_down_when_add_fails(tmp_path: Path) -> None:
    runner = RecordingRunner("add")

    with pytest.raises(QaError, match="claude mcp add failed"):
        async with registered_server("run4", cwd=tmp_path, runner=runner):
            pytest.fail("the body must not run")

    assert runner.steps == ["add", "logout", "remove"]


def test_port_is_free_detects_a_bound_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]

        assert port_is_free(port) is False

    assert port_is_free(port) is True


async def test_registered_server_refuses_a_busy_callback_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_qa_lib, "port_is_free", lambda *_: False)
    runner = RecordingRunner()

    with pytest.raises(QaError, match="is in use"):
        async with registered_server("run5", cwd=tmp_path, runner=runner):
            pass

    assert runner.calls == []


# Claude plumbing: processes -------------------------------------------------------------------------------------

LINK_CHILD = (
    "import sys\n"
    f"sys.stdout.write('\\x1b]8;;{AUTH_URL}\\x07link\\x1b]8;;\\x07 {AUTH_URL}?code=secret-code \\r\\n')\n"
    "sys.stdout.flush()\n"
    "line = sys.stdin.readline()\n"
    "print('got', len(line.strip()))\n"
    "print('Authenticated with x')\n"
)
BLOCKING_CHILD = "import threading; threading.Event().wait()"


async def answer_on_url(output: str) -> str | None:
    return "reply" if find_authorization_url(output) else None


async def test_run_pty_answers_once_and_masks_urls_in_the_outcome(tmp_path: Path) -> None:
    def finished(output: str) -> bool:
        return "authenticated with" in output.lower()

    outcome = await run_pty([sys.executable, "-c", LINK_CHILD], answer_on_url, finished, timeout_s=20, cwd=tmp_path)

    assert (outcome.completed, outcome.timed_out, outcome.exit_code) == (True, False, 0)
    assert "got 5" in outcome.tail
    assert "[url]" in outcome.tail
    assert "secret-code" not in outcome.tail
    assert "http://" not in outcome.tail


async def test_run_pty_times_out_and_kills_a_blocked_child(tmp_path: Path) -> None:
    async def silent(output: str) -> str | None:
        return None

    outcome = await run_pty(
        [sys.executable, "-c", BLOCKING_CHILD], silent, lambda _output: False, timeout_s=1, cwd=tmp_path
    )

    assert (outcome.timed_out, outcome.completed) == (True, False)
    assert outcome.exit_code == -signal.SIGTERM


def open_descriptors() -> int:
    gc.collect()
    return len(list(Path("/dev/fd").iterdir()))


async def test_run_pty_leaks_no_descriptor_when_the_command_is_missing(tmp_path: Path) -> None:
    before = open_descriptors()

    with pytest.raises(QaError, match="is not on PATH"):
        await run_pty(["no-such-command-mcp-qa"], answer_on_url, lambda _output: False, timeout_s=1, cwd=tmp_path)

    assert open_descriptors() <= before


async def test_run_command_returns_the_exit_code(tmp_path: Path) -> None:
    assert await run_command([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == 3


async def test_run_command_times_out_with_a_qa_error(tmp_path: Path) -> None:
    with pytest.raises(QaError, match="timed out"):
        await run_command([sys.executable, "-c", BLOCKING_CHILD], tmp_path, timeout_s=1)


# Run reports ----------------------------------------------------------------------------------------------------


def test_reports_go_to_the_git_ignored_folder() -> None:
    assert REPORTS_DIR == ROOT / ".reports" / "mcp-qa"
    ignored = subprocess.run(
        [str(shutil.which("git")), "check-ignore", "-q", str(REPORTS_DIR / "run.json")], cwd=ROOT, check=False
    )

    assert ignored.returncode == 0


def test_write_report_writes_scenarios_and_metrics(tmp_path: Path) -> None:
    report = RunReport("run-1", "e2e", [ScenarioResult("E1", "passed", 12.5, "ok")], {"p95_ms": 480.5})

    path = write_report(report, tmp_path / "reports")

    assert path == tmp_path / "reports" / "run-1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "run_id": "run-1",
        "kind": "e2e",
        "results": [{"scenario": "E1", "outcome": "passed", "duration_ms": 12.5, "note": "ok"}],
        "metrics": {"p95_ms": 480.5},
    }


@pytest.mark.parametrize(
    "note",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0",
        "sent Bearer abc",
        "Authorization header",
        "back?code=abc",
        "alice@example.com",
    ],
)
def test_write_report_refuses_token_header_code_and_email_shapes(tmp_path: Path, note: str) -> None:
    report = RunReport("run-2", "e2e", [ScenarioResult("E1", "failed", 1.0, note)])

    with pytest.raises(QaError, match="report refused"):
        write_report(report, tmp_path / "reports")

    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "with space"])
def test_write_report_refuses_an_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(QaError, match="unsafe run id"):
        write_report(RunReport(run_id, "e2e", []), tmp_path)

    assert list(tmp_path.iterdir()) == []


# Environment ----------------------------------------------------------------------------------------------------


def test_require_env_returns_secrets_and_strips_values() -> None:
    values = require_env("A_NAME", environ={"A_NAME": "  value-1 "})

    assert values["A_NAME"].get_secret_value() == "value-1"
    assert "value-1" not in repr(values)


def test_require_env_names_missing_variables_only_and_hints_for_keycloak_names() -> None:
    environ = {"KEYCLOAK_QA_PASSWORD": "present-value", "OTHER": "  "}

    with pytest.raises(SystemExit) as raised:
        require_env("KEYCLOAK_QA_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "OTHER", environ=environ)

    message = str(raised.value)
    assert "KEYCLOAK_ADMIN_PASSWORD, OTHER" in message
    assert "make env-local" in message
    assert "present-value" not in message


def test_require_env_has_no_keycloak_hint_for_other_names() -> None:
    with pytest.raises(SystemExit) as raised:
        require_env("NEO4J_PASSWORD", environ={})

    assert "make env-local" not in str(raised.value)


def test_apply_local_defaults_never_overrides_a_value() -> None:
    environ = {"DATABASE_PORT": "6000"}

    apply_local_defaults(environ)

    assert environ["DATABASE_PORT"] == "6000"
    assert environ["DATABASE_HOST"] == "localhost"
    assert environ["NEO4J_URI"] == "bolt://localhost:7687"
    assert urlsplit(environ["NEO4J_URI"]).port == 7687


async def test_neo4j_driver_unconfigured_raises_a_qa_error_naming_the_names() -> None:
    with pytest.raises(QaError, match="NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD"):
        async with neo4j_driver({}):
            pytest.fail("no driver without configuration")


# Review fixes: argv, claude_login, process groups, chunked links, fail-closed branches, E8 helpers ---------------

BLOCK = "import threading; threading.Event().wait()"
GRANDCHILD_PARENT = (
    "import subprocess, sys, threading\n"
    f"child = subprocess.Popen([sys.executable, '-c', {BLOCK!r}])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "threading.Event().wait()\n"
)


def test_argv_builders_give_the_exact_claude_commands() -> None:
    name = server_name("abc123")

    assert name == "specflow-qa-abc123"
    assert claude_add_argv(name) == [
        *("claude", "mcp", "add", "--transport", "http", "--scope", "local"),
        *("--client-id", "specflow-mcp", "--callback-port", "8765", name, MCP_RESOURCE_URL),
    ]
    assert claude_login_argv(name) == ["claude", "mcp", "login", name, "--no-browser"]
    assert claude_logout_argv(name) == ["claude", "mcp", "logout", name]
    assert claude_remove_argv(name) == ["claude", "mcp", "remove", name, "--scope", "local"]
    assert claude_prompt_argv("hello", ["mcp__a__x", "mcp__a__y"]) == [
        *("claude", "-p", "hello", "--output-format", "stream-json", "--verbose"),
        *("--model", "sonnet", "--max-turns", "8", "--max-budget-usd", "1.00"),
        *("--permission-mode", "dontAsk", "--no-session-persistence"),
        *("--disallowedTools", ",".join(CLAUDE_DISALLOWED_TOOLS)),
        *("--allowedTools", "mcp__a__x,mcp__a__y"),
    ]
    assert claude_tool_names(name) == (
        "mcp__specflow-qa-abc123__list_projects",
        "mcp__specflow-qa-abc123__list_features",
    )


def test_new_run_ids_are_unique_and_safe_for_report_names() -> None:
    first, second = new_run_id(), new_run_id()

    assert first != second
    assert all(re.fullmatch(r"[0-9a-f]{8}", run_id) for run_id in (first, second))


def pid_of(path: Path) -> int:
    return int(path.read_text(encoding="utf-8"))


async def wait_until_gone(pid: int, seconds: float = 10) -> bool:
    """True once the process has exited (a pidfd turns readable at exit, even for a zombie): no polling."""

    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    loop = asyncio.get_running_loop()
    exited = asyncio.Event()
    loop.add_reader(descriptor, exited.set)
    try:
        async with asyncio.timeout(seconds):
            await exited.wait()
    finally:
        loop.remove_reader(descriptor)
        os.close(descriptor)
    return True


async def test_run_pty_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"

    async def silent(_output: str) -> str | None:
        return None

    argv = [sys.executable, "-c", GRANDCHILD_PARENT, str(pid_file)]
    outcome = await run_pty(argv, silent, lambda _output: False, timeout_s=3, cwd=tmp_path)

    assert (outcome.timed_out, outcome.exit_code) == (True, -signal.SIGTERM)
    assert await wait_until_gone(pid_of(pid_file))


async def test_run_command_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(QaError, match="timed out"):
        await run_command([sys.executable, "-c", GRANDCHILD_PARENT, str(pid_file)], tmp_path, timeout_s=3)

    assert await wait_until_gone(pid_of(pid_file))


@pytest.mark.parametrize(
    "split",
    [
        f"\x1b]8;;{AUTH_URL[:40]}",  # an OSC 8 target cut before its text and terminator
        f"visit {AUTH_URL[:40]}",  # a plain URL cut mid-way
    ],
    ids=["osc8", "plain"],
)
async def test_run_pty_answers_once_with_the_whole_link_when_it_arrives_in_two_chunks(
    tmp_path: Path, split: str
) -> None:
    rest = AUTH_URL[40:]
    tail = f"{rest}\x07link\x1b]8;;\x07\r\n" if split.startswith("\x1b") else f"{rest}\r\n"
    child = (
        "import sys, threading\n"
        f"sys.stdout.write({split!r}); sys.stdout.flush()\n"
        "threading.Event().wait(0.4)\n"
        f"sys.stdout.write({tail!r}); sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        "print('got it')\n"
    )
    found: list[str] = []

    async def respond(output: str) -> str | None:
        url = find_authorization_url(output)
        if url is not None:
            found.append(url)
        return None if url is None else "reply"

    outcome = await run_pty(
        [sys.executable, "-c", child], respond, lambda out: "got it" in out, timeout_s=20, cwd=tmp_path
    )

    assert outcome.completed
    assert found == [AUTH_URL]


REDIRECT = "http://localhost:8765/callback?code=c-9&state=s-9&iss=" + ISSUER
LEAKY_CHILD = (
    "import sys\n"
    f"sys.stdout.write('\\x1b]8;;{AUTH_URL}\\x07link\\x1b]8;;\\x07\\r\\n')\n"
    f"print('echo {REDIRECT} code=zzz state=yyy {QA_USERS['alice']}', flush=True)\n"
    "sys.stdin.readline()\n"
)


@pytest.mark.parametrize(
    ("ending", "timeout_s", "expected"),
    [("raise SystemExit(1)", 20, "claude login failed: exit 1"), (BLOCK, 1, "claude login failed: timed out")],
    ids=["exit", "timeout"],
)
async def test_claude_login_errors_map_the_outcome_and_leak_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str, timeout_s: float, expected: str
) -> None:
    async def fixed_login(url: str, username: str, password: SecretStr, **_options: Any) -> str:
        return REDIRECT

    monkeypatch.setattr(
        mcp_qa_lib, "claude_login_argv", lambda _name: [sys.executable, "-c", f"{LEAKY_CHILD}{ending}\n"]
    )
    monkeypatch.setattr(mcp_qa_lib, "complete_login", fixed_login)

    with pytest.raises(QaError) as raised:
        await claude_login("specflow-qa-x", "alice", CREDENTIAL, cwd=tmp_path, timeout_s=timeout_s)

    message = str(raised.value)
    assert message == expected
    assert not any(leak in message for leak in ("code=", "state=", QA_USERS["alice"], "http"))


async def test_claude_login_succeeds_when_the_cli_reports_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fixed_login(url: str, username: str, password: SecretStr, **_options: Any) -> str:
        return REDIRECT

    child = f"{LEAKY_CHILD}print('Authenticated with x', flush=True)\n"
    monkeypatch.setattr(mcp_qa_lib, "claude_login_argv", lambda _name: [sys.executable, "-c", child])
    monkeypatch.setattr(mcp_qa_lib, "complete_login", fixed_login)

    await claude_login("specflow-qa-x", "alice", CREDENTIAL, cwd=tmp_path, timeout_s=20)


# Fail-closed branches -------------------------------------------------------------------------------------------


async def test_user_token_refuses_a_redirect_without_a_code() -> None:
    fake = FakeKeycloak(code_in_redirect=False)

    with pytest.raises(QaError, match="no authorization code"):
        await user_token("alice", CREDENTIAL, transport=fake.transport)

    assert not fake.sent("POST", CODE_EXCHANGE_PATH)


async def test_user_token_names_a_failing_token_endpoint_status() -> None:
    with pytest.raises(QaError, match=r"^token failed: HTTP 400$"):
        await user_token("alice", CREDENTIAL, transport=FakeKeycloak(token_status=400).transport)


async def test_keycloak_admin_refuses_a_create_response_without_a_location() -> None:
    fake = FakeKeycloak(location_on_create=False)

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=fake.transport) as admin:
        with pytest.raises(QaError, match="create user failed: no location"):
            await admin.ensure_user("alice", CREDENTIAL)

    assert not fake.sent("PUT", f"{ADMIN_PATH}/users/created-1/reset-password")


async def test_keycloak_admin_names_a_failing_client_secret_update() -> None:
    fake = FakeKeycloak(client_put_status=500)

    async with KeycloakAdmin(SecretStr("admin-credential"), transport=fake.transport) as admin:
        with pytest.raises(QaError, match=r"^set client secret failed: HTTP 500$"):
            await admin.set_client_secret(WEB_CLIENT_ID, SecretStr("web-credential-0001"))


# E8 helpers and fixture lookup (the database is the boundary: a fake session) -----------------------------------


class FakeSession:
    """The `AsyncSession` calls the E8 helpers and `fixture_ids` make; `scalars` feeds `scalar` in order."""

    def __init__(self, rows: list[ProjectMember] | None = None, scalars: list[Any] | None = None) -> None:
        self.rows = {(row.project_id, row.user_id): row for row in rows or []}
        self.queued = list(scalars or [])
        self.added: list[ProjectMember] = []
        self.deleted: list[ProjectMember] = []
        self.flushes = 0

    async def get(self, _model: type, key: tuple[uuid.UUID, uuid.UUID]) -> ProjectMember | None:
        return self.rows.get(key)

    def add(self, row: ProjectMember) -> None:
        self.rows[(row.project_id, row.user_id)] = row
        self.added.append(row)

    async def delete(self, row: ProjectMember) -> None:
        self.rows.pop((row.project_id, row.user_id))
        self.deleted.append(row)

    async def flush(self) -> None:
        self.flushes += 1

    async def scalar(self, _statement: object) -> Any:
        return self.queued.pop(0)


PROJECT_ID, USER_ID = uuid.uuid4(), uuid.uuid4()


async def test_remove_and_restore_member_report_what_they_changed() -> None:
    member = ProjectMember(project_id=PROJECT_ID, user_id=USER_ID, role="owner")
    session = FakeSession([member])

    assert await remove_member(session, PROJECT_ID, USER_ID) is True
    assert await remove_member(session, PROJECT_ID, USER_ID) is False
    assert session.deleted == [member]
    assert await restore_member(session, PROJECT_ID, USER_ID) is True
    assert await restore_member(session, PROJECT_ID, USER_ID) is False
    (restored,) = session.added
    assert (restored.project_id, restored.user_id, restored.role) == (PROJECT_ID, USER_ID, "owner")
    assert session.flushes == 2


async def test_fixture_ids_reads_users_then_projects_and_refuses_what_is_missing() -> None:
    ids = [uuid.uuid4() for _ in range(6)]

    found = await fixture_ids(FakeSession(scalars=list(ids)), "dev")

    assert found.users == {"alice": ids[0], "bob": ids[1]}
    assert list(found.projects) == ["P-Alpha", "P-Empty", "P-Large", "P-Beta"]
    assert list(found.projects.values()) == ids[2:]
    with pytest.raises(QaError, match="user alice is not seeded"):
        await fixture_ids(FakeSession(scalars=[None]), "dev")
    with pytest.raises(QaError, match="project P-Alpha is not seeded"):
        await fixture_ids(FakeSession(scalars=[ids[0], ids[1], None]), "dev")
    with pytest.raises(QaError, match="keycloak mode needs"):
        await fixture_ids(FakeSession(), "keycloak")


# SDK client, latency runner and prompt runner (MCP-M7) -----------------------------------------------------------

AUTHORIZE_ENDPOINT = f"{ISSUER}/protocol/openid-connect/auth"
PROVIDER_SCOPES = ["openid", "profile", "email", "roles", "web-origins", "offline_access", "address"]
PRM_DOCUMENT = {"resource": MCP_RESOURCE_URL, "authorization_servers": [ISSUER]}


class FakeProvider:
    """Just enough of the MCP resource server and Keycloak for the SDK's OAuth flow (an `httpx2` mock transport)."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.authorized: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.paths.append(request.url.path)
        challenge = {"WWW-Authenticate": f'Bearer resource_metadata="{PRM_URL}"'}
        if request.url.path == "/mcp":
            if request.headers.get("Authorization") == f"Bearer {ACCESS}":
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(401, headers=challenge)
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(200, json=PRM_DOCUMENT)
        if request.url.path.startswith("/.well-known/"):
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE_ENDPOINT,
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                    "scopes_supported": PROVIDER_SCOPES,
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if request.url.path.endswith("/token"):
            return httpx2.Response(200, json={"access_token": ACCESS, "token_type": "Bearer", "expires_in": 300})
        return httpx2.Response(404)

    async def login(self, url: str) -> str:
        self.authorized.append(url)
        state = parse_qs(urlsplit(url).query)["state"][0]
        return f"{MCP_REDIRECT_URI}?code=code-0001&state={state}&iss={ISSUER}"


async def provider_request(provider: FakeProvider, login: Any) -> httpx2.Response:
    auth = oauth_provider("alice", CREDENTIAL, login=login)
    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(provider.handler)) as http:
        return await http.post(MCP_RESOURCE_URL, json={})


def test_narrow_scope_replaces_only_the_scope() -> None:
    url = f"{AUTHORIZE_ENDPOINT}?response_type=code&scope=a+b+c&state=s1&resource={MCP_RESOURCE_URL}&code_challenge=ch"

    narrowed = parse_qs(urlsplit(narrow_scope(url)).query)

    assert narrowed["scope"] == ["openid"]
    assert (narrowed["state"], narrowed["code_challenge"], narrowed["resource"]) == (
        ["s1"],
        ["ch"],
        [MCP_RESOURCE_URL],
    )
    assert narrow_scope(url, "openid email").count("scope=openid+email") == 1
    assert "scope=openid" in narrow_scope(f"{AUTHORIZE_ENDPOINT}?state=s1")


async def test_oauth_provider_signs_in_with_the_preregistered_client_and_a_narrowed_scope() -> None:
    provider = FakeProvider()

    response = await provider_request(provider, provider.login)

    assert response.status_code == 200
    assert not [path for path in provider.paths if "regist" in path]
    (url,) = provider.authorized
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == ["openid"]
    assert (query["client_id"], query["redirect_uri"]) == (["specflow-mcp"], [MCP_REDIRECT_URI])
    assert query["resource"] == [MCP_RESOURCE_URL]


async def test_oauth_provider_names_only_the_error_of_a_refused_sign_in() -> None:
    provider = FakeProvider()

    async def refused(_url: str) -> str:
        return f"{MCP_REDIRECT_URI}?error=invalid_scope&error_description=secret+text&state=x"

    with pytest.raises(QaError, match=r"^sign-in failed: invalid_scope$"):
        await provider_request(provider, refused)

    async def unreadable(_url: str) -> str:
        return f"{MCP_REDIRECT_URI}?error=bad%20value%20code%3Dsecret"

    with pytest.raises(QaError, match=r"^sign-in failed: no code$"):
        await provider_request(provider, unreadable)


async def test_the_memory_storage_starts_with_the_public_client() -> None:
    storage = MemoryTokenStorage()
    info = await storage.get_client_info()

    assert info is not None
    assert (info.client_id, info.token_endpoint_auth_method) == ("specflow-mcp", "none")
    assert await storage.get_tokens() is None


async def test_an_mcp_session_needs_exactly_one_of_auth_and_token() -> None:
    for kwargs in ({}, {"token": "t", "auth": object()}):
        with pytest.raises(QaError, match="exactly one"):
            async with mcp_session(**kwargs):
                pass


def response_mock(responses: dict[str, httpx.Response] | Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(responses, Exception):
            raise responses
        return responses[f"{request.method} {request.url.path}"]

    return httpx.MockTransport(handler)


PRM_PATH = urlsplit(PRM_URL).path


@pytest.mark.parametrize(
    ("response", "mode"),
    [(httpx.Response(200, json=PRM_DOCUMENT), "oidc"), (httpx.Response(404), "dev")],
)
async def test_stack_mode_reads_the_resource_metadata(response: httpx.Response, mode: str) -> None:
    assert await stack_mode(transport=response_mock({f"GET {PRM_PATH}": response})) == mode


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (response_mock({f"GET {PRM_PATH}": httpx.Response(500)}), "stack mode unknown: HTTP 500"),
        (response_mock({f"GET {PRM_PATH}": httpx.Response(200, json={"resource": "http://x"})}), "another resource"),
        (response_mock({f"GET {PRM_PATH}": httpx.Response(200, text="no json")}), "another resource"),
        (response_mock(httpx.ConnectError("refused")), "not reachable: run make up"),
    ],
)
async def test_stack_mode_refuses_what_it_cannot_classify(transport: httpx.MockTransport, message: str) -> None:
    with pytest.raises(QaError, match=message):
        await stack_mode(transport=transport)


async def test_fetch_prm_returns_the_document_or_names_the_status() -> None:
    assert await fetch_prm(transport=response_mock({f"GET {PRM_PATH}": httpx.Response(200, json=PRM_DOCUMENT)})) == (
        PRM_DOCUMENT
    )
    with pytest.raises(QaError, match="prm failed: HTTP 404"):
        await fetch_prm(transport=response_mock({f"GET {PRM_PATH}": httpx.Response(404)}))


async def test_post_mcp_sends_an_initialize_with_the_token_and_host_override() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    transport = httpx.MockTransport(handler)

    anonymous = await post_mcp(None, transport=transport)
    foreign = await post_mcp("tok-0001", host="attacker.example", transport=transport)

    assert (anonymous.status_code, foreign.status_code) == (401, 401)
    assert "Authorization" not in seen[0].headers
    assert seen[1].headers["Authorization"] == "Bearer tok-0001"
    assert seen[1].headers["Host"] == "attacker.example"
    assert seen[0].headers["Host"] == "localhost:8002"
    assert json_of(seen[0])["method"] == "initialize"
    assert "text/event-stream" in seen[0].headers["Accept"]


async def test_create_project_rest_posts_the_name_and_returns_the_id() -> None:
    created = uuid.uuid4()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": str(created), "name": "QA"})

    assert await create_project_rest("QA", "web-0001", transport=httpx.MockTransport(handler)) == created
    assert (seen[0].url.path, json_of(seen[0]), seen[0].headers["Authorization"]) == (
        "/api/v1/projects",
        {"name": "QA"},
        "Bearer web-0001",
    )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401), "project create failed: HTTP 401"),
        (httpx.Response(201, json={"name": "QA"}), "no project id"),
        (httpx.Response(201, json={"id": "not-a-uuid"}), "no project id"),
    ],
)
async def test_create_project_rest_refuses_a_bad_response(response: httpx.Response, message: str) -> None:
    with pytest.raises(QaError, match=message):
        await create_project_rest("QA", "web-0001", transport=httpx.MockTransport(lambda _request: response))


class FakeToolResult:
    def __init__(self, structured: Any = None, text: str | None = None, *, is_error: bool = False) -> None:
        self.structured_content = structured
        self.is_error = is_error
        self.content = [] if text is None else [TextContent(type="text", text=text)]


class FakeToolSession:
    def __init__(self, *results: FakeToolResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeToolResult:
        self.calls.append((name, arguments))
        return self.results.pop(0)


async def test_call_reads_structured_content_then_json_text_and_exposes_the_error_code() -> None:
    error = {"error": {"code": "project_not_found", "message": "m"}}
    session = FakeToolSession(FakeToolResult({"items": []}), FakeToolResult(None, json.dumps(error), is_error=True))
    session.results.append(FakeToolResult(None, "not json"))

    first = await call(session, "list_projects", {})
    second = await call(session, "list_features", {"project_id": "p"})
    third = await call(session, "list_projects", {})

    assert (first.is_error, first.data, first.code) == (False, {"items": []}, None)
    assert (second.is_error, second.code) == (True, "project_not_found")
    assert (third.data, third.code) == ({}, None)


async def test_all_projects_pages_until_the_cursor_is_null_and_names_an_error_code() -> None:
    session = FakeToolSession(
        FakeToolResult({"items": [{"name": "a"}], "next_cursor": "c1"}),
        FakeToolResult({"items": [{"name": "b"}], "next_cursor": None}),
    )

    assert [p["name"] for p in await all_projects(session)] == ["a", "b"]
    assert [arguments for _, arguments in session.calls] == [{}, {"cursor": "c1"}]
    failing = FakeToolSession(FakeToolResult({"error": {"code": "internal_error"}}, is_error=True))
    with pytest.raises(QaError, match="list_projects failed: internal_error"):
        await all_projects(failing)


def test_percentile_is_nearest_rank() -> None:
    samples = [float(n) for n in range(1, 11)]

    assert percentile(samples, 0.5) == 5.0
    assert percentile(samples, 0.95) == 10.0
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([7.0], 0.95) == 7.0
    with pytest.raises(QaError, match="no samples"):
        percentile([], 0.5)


async def test_run_latency_starts_calls_on_cadence_even_when_every_call_is_still_running() -> None:
    running = peak = 0
    all_started = asyncio.Event()

    async def blocked_call() -> bool:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == 10:
            all_started.set()
        await all_started.wait()  # no call finishes before the tenth has started: no waiting on earlier calls
        running -= 1
        return True

    stats = await run_latency(blocked_call, rate_per_s=50, duration_s=0.2)

    assert (stats.calls, stats.errors, peak) == (10, 0, 10)
    assert stats.max_ms >= stats.p95_ms >= stats.p50_ms


async def test_run_latency_counts_a_false_and_a_raising_call_as_errors_and_keeps_their_samples() -> None:
    outcomes = iter([True, False, True, None])

    async def mixed() -> bool:
        outcome = next(outcomes)
        if outcome is None:
            raise QaError("secret detail")
        return outcome

    stats = await run_latency(mixed, rate_per_s=100, duration_s=0.04)

    assert (stats.calls, stats.errors) == (4, 2)
    assert stats.p95_ms >= 0


async def test_run_latency_refuses_an_empty_run_and_reports_metrics_rounded() -> None:
    async def ok() -> bool:
        return True

    with pytest.raises(QaError, match="at least one call"):
        await run_latency(ok, rate_per_s=1, duration_s=0.1)

    stats = LatencyStats(600, 1, 22.64, 26.04, 28.14, 60.0)

    assert stats.metrics("e14") == {
        "e14_calls": 600,
        "e14_errors": 1,
        "e14_p50_ms": 22.6,
        "e14_p95_ms": 26.0,
        "e14_max_ms": 28.1,
        "e14_rate_per_s": 10.0,
    }


STREAM_CHILD = (
    "import json, sys\n"
    "def emit(message): print(json.dumps(message), flush=True)\n"
    "emit({'type': 'system', 'subtype': 'init', 'mcp_servers': [{'name': 'srv', 'status': 'connected'}]})\n"
    "emit({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'u1', 'name': 'mcp__srv__x',"
    " 'input': {'project_id': 'p1'}}]}})\n"
    "big = 'z' * 100000\n"
    "emit({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1',"
    " 'content': [{'type': 'text', 'text': json.dumps({'items': [], 'pad': big})}]}]}})\n"
    "emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'num_turns': 2,"
    " 'total_cost_usd': 0.01})\n"
    "print('warning on stderr http://x/y', file=sys.stderr)\n"
    "sys.exit(3)\n"
)


async def test_run_prompt_parses_stream_json_lines_longer_than_the_stream_reader_limit(tmp_path: Path) -> None:
    run = await run_prompt([sys.executable, "-c", STREAM_CHILD], tmp_path, timeout_s=30)

    assert (run.exit_code, run.timed_out) == (3, False)
    assert max(len(line) for line in run.lines) > 2**16
    assert run.init is not None
    assert run.init["mcp_servers"][0]["status"] == "connected"
    assert run.result is not None
    assert (run.result["num_turns"], run.prose) == (2, "done")
    call_event, result_event = run.events
    assert (call_event.kind, call_event.name, call_event.payload) == ("call", "mcp__srv__x", {"project_id": "p1"})
    assert (result_event.kind, result_event.name) == ("result", "mcp__srv__x")
    assert tool_json(result_event)["items"] == []
    assert "project_id" in run.text
    assert run.stderr_tail.strip() == "warning on stderr [url]"


async def test_run_prompt_times_out_and_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"

    run = await run_prompt([sys.executable, "-c", GRANDCHILD_PARENT, str(pid_file)], tmp_path, timeout_s=3)

    assert run.timed_out
    assert (run.init, run.result, run.events) == (None, None, [])
    assert await wait_until_gone(pid_of(pid_file))


async def test_run_prompt_refuses_a_missing_cli(tmp_path: Path) -> None:
    with pytest.raises(QaError, match="is not on PATH"):
        await run_prompt(["no-such-cli-0001"], tmp_path)


def test_tool_json_reads_strings_text_blocks_and_gives_none_for_the_rest() -> None:
    def event(payload: Any) -> ToolEvent:
        return ToolEvent("result", "u", "n", payload, is_error=False)

    assert tool_json(event('{"a": 1}')) == {"a": 1}
    assert tool_json(event([{"type": "text", "text": '{"a":'}, {"type": "text", "text": " 2}"}])) == {"a": 2}
    assert tool_json(event("plain prose")) is None
    assert tool_json(event(None)) is None
    assert tool_json(event({"a": 3})) == {"a": 3}


def test_persona_prompts_name_the_beta_id_in_the_third_prompt_only() -> None:
    beta = uuid.uuid4()
    ids = FixtureIds({}, {"P-Beta": beta})

    prompts = persona_prompts(ids)

    assert [key for key, _ in prompts] == ["agent-1", "agent-2", "agent-3"]
    assert prompts[2][1] == f"Show me the features of project {beta}."
    assert not any(str(beta) in text for _, text in prompts[:2])


@pytest.mark.parametrize(
    "text",
    [
        "see http://localhost:8180/realms/x?code=abc&state=s for it",
        "alice@example.com and bob@other.example",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig here",
        "Bearer abc Authorization: Bearer abc code=abc @mention",
    ],
)
def test_report_note_masks_urls_emails_tokens_and_headers_so_write_report_accepts_it(text: str, tmp_path: Path) -> None:
    note = report_note(text)

    for fragment in ("http", "@", "eyJ", "Bearer", "Authorization", "code="):
        assert fragment.lower() not in note.lower()
    write_report(RunReport("note-run", "agent", [ScenarioResult("agent-1", "passed", 1.0, note)]), tmp_path)


def test_report_note_strips_escapes_collapses_whitespace_and_truncates() -> None:
    assert report_note("\x1b[31mred\x1b[0m\n\n  text") == "red text"
    assert report_note("x" * 500) == "x" * 300
    assert report_note("x" * 500, 2000) == "x" * 500


async def test_run_scenario_records_the_outcome_and_the_error_type_only() -> None:
    report = RunReport("r", "e2e", [])

    async def passing() -> None:
        await asyncio.sleep(0)

    async def failing() -> None:
        raise AssertionError("secret detail")

    await run_scenario(report, "E1", passing())
    with pytest.raises(AssertionError):
        await run_scenario(report, "E2", failing())

    assert [(r.scenario, r.outcome, r.note) for r in report.results] == [
        ("E1", "passed", ""),
        ("E2", "failed", "AssertionError"),
    ]
    assert all(r.duration_ms >= 0 for r in report.results)


async def test_capture_command_returns_the_exit_code_and_stdout(tmp_path: Path) -> None:
    code, out = await capture_command([sys.executable, "-c", "print('hi'); raise SystemExit(4)"], tmp_path)

    assert (code, out.strip()) == (4, "hi")
    with pytest.raises(QaError, match="timed out"):
        await capture_command([sys.executable, "-c", BLOCKING_CHILD], tmp_path, timeout_s=1)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("print('{\"loggedIn\": true}')", True),
        ("print('{\"loggedIn\": false}')", False),
        ("print('not json')", False),
        ("print('{\"loggedIn\": true}'); raise SystemExit(1)", False),
    ],
)
async def test_claude_signed_in_reads_logged_in_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, script: str, expected: bool
) -> None:
    real = mcp_qa_lib.capture_command

    async def stand_in(_argv: Sequence[str], cwd: Path, **kwargs: Any) -> tuple[int, str]:
        return await real([sys.executable, "-c", script], cwd, **kwargs)

    monkeypatch.setattr(mcp_qa_lib, "capture_command", stand_in)

    assert await claude_signed_in(tmp_path) is expected


async def test_claude_signed_in_is_false_without_the_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    assert await claude_signed_in(tmp_path) is False


@pytest.mark.parametrize(
    ("listing", "left"),
    [
        ("a\nspecflow-qa-1: http://x\n", True),
        ("a\n  specflow-qa-1: http://x\n", True),
        ("a\nb\n", False),
        ("other: http://x/specflow-qa-1 - ok\nmy-specflow-qa-2: http://y\n", False),  # not at a line start
    ],
)
async def test_claude_lists_looks_for_the_prefix_and_refuses_a_failed_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, listing: str, left: bool
) -> None:
    timeouts: list[float] = []

    async def stand_in(_argv: Sequence[str], _cwd: Path, **kwargs: Any) -> tuple[int, str]:
        timeouts.append(kwargs["timeout_s"])
        return 0, listing

    monkeypatch.setattr(mcp_qa_lib, "capture_command", stand_in)
    assert await claude_lists("specflow-qa-", tmp_path) is left
    assert timeouts == [120.0]

    async def failing(_argv: Sequence[str], _cwd: Path, **_kwargs: Any) -> tuple[int, str]:
        return 2, ""

    monkeypatch.setattr(mcp_qa_lib, "capture_command", failing)
    with pytest.raises(QaError, match="mcp list failed: exit 2"):
        await claude_lists("specflow-qa-", tmp_path)


class SignedInSeams:
    """The `claude_login` and `claude mcp list` boundaries of `signed_in_claude`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, left: bool = False, login_fails: bool = False) -> None:
        self.logged_in: list[str] = []
        self.listed = 0
        self.verdict = TeardownVerdict()

        async def login(name: str, _key: str, _password: SecretStr, **_kwargs: Any) -> None:
            self.logged_in.append(name)
            if login_fails:
                raise QaError("claude login failed: timed out")

        async def lists(_prefix: str, _cwd: Path) -> bool:
            self.listed += 1
            return left

        monkeypatch.setattr(mcp_qa_lib, "claude_login", login)
        monkeypatch.setattr(mcp_qa_lib, "claude_lists", lists)


@pytest.mark.usefixtures("free_callback_port")
async def test_signed_in_claude_signs_in_yields_the_name_and_checks_the_listing_after_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seams, runner = SignedInSeams(monkeypatch), RecordingRunner()

    async with signed_in_claude(
        "alice", CREDENTIAL, cwd=tmp_path, run_id="run9", runner=runner, verdict=seams.verdict
    ) as name:
        assert (name, seams.logged_in, seams.listed) == ("specflow-qa-run9", ["specflow-qa-run9"], 0)

    assert runner.steps == ["add", "logout", "remove"]
    assert (seams.listed, seams.verdict.clean) == (1, True)


@pytest.mark.usefixtures("free_callback_port")
async def test_signed_in_claude_raises_when_a_server_is_left_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seams = SignedInSeams(monkeypatch, left=True)

    with pytest.raises(QaError, match="left a specflow-qa- server"):
        async with signed_in_claude(
            "alice",
            CREDENTIAL,
            cwd=tmp_path,
            run_id="run8",
            runner=RecordingRunner(),
            verdict=seams.verdict,
        ):
            pass

    assert seams.verdict.clean is False


@pytest.mark.usefixtures("free_callback_port")
async def test_signed_in_claude_tears_down_when_the_login_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seams, runner = SignedInSeams(monkeypatch, left=True, login_fails=True), RecordingRunner()

    with pytest.raises(QaError, match="login failed"):
        async with signed_in_claude(
            "alice", CREDENTIAL, cwd=tmp_path, run_id="run7", runner=runner, verdict=seams.verdict
        ):
            pytest.fail("the body must not run")

    assert runner.steps == ["add", "logout", "remove"]
    # the check still runs (and is recorded) when the login fails, but the failure of the login wins
    assert (seams.listed, seams.verdict.clean) == (1, False)


@pytest.mark.usefixtures("free_callback_port")
async def test_signed_in_claude_tears_down_when_the_body_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seams, runner = SignedInSeams(monkeypatch, left=True), RecordingRunner()

    with pytest.raises(ValueError, match="body failed"):
        async with signed_in_claude(
            "alice", CREDENTIAL, cwd=tmp_path, run_id="run6", runner=runner, verdict=seams.verdict
        ):
            raise ValueError("body failed")

    assert runner.steps == ["add", "logout", "remove"]
    # a leftover server is recorded, never masks the body error
    assert (seams.listed, seams.verdict.clean) == (1, False)


@pytest.mark.usefixtures("free_callback_port")
async def test_signed_in_claude_counts_an_unreadable_listing_as_not_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    SignedInSeams(monkeypatch)
    verdict = TeardownVerdict()

    async def unreadable(_prefix: str, _cwd: Path) -> bool:
        raise QaError("claude mcp list failed: exit 2")

    monkeypatch.setattr(mcp_qa_lib, "claude_lists", unreadable)

    with pytest.raises(QaError, match="left a specflow-qa- server"):
        async with signed_in_claude(
            "alice", CREDENTIAL, cwd=tmp_path, run_id="run5", runner=RecordingRunner(), verdict=verdict
        ):
            pass

    assert verdict.clean is False


# Child environment (R1) ------------------------------------------------------------------------------------------

STACK_SECRETS = {
    "DATABASE_PASSWORD": "db-credential-0001",
    "KEYCLOAK_QA_PASSWORD": "kc-credential-0001",
    "KEYCLOAK_ADMIN_PASSWORD": "kc-credential-0002",
    "KEYCLOAK_WEB_CLIENT_SECRET": "kc-credential-0003",
    "AUTH_DEV_SIGNING_KEY": "signing-credential-0001",
    "NEO4J_PASSWORD": "neo-credential-0001",
}
KEPT_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/qa",
    "USER": "qa",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "xterm",
    "TMPDIR": "/scratch/qa",
    "CLAUDE_CONFIG_DIR": "/home/qa/.claude",
    "HTTPS_PROXY": "http://proxy.example:3128",
    "NO_PROXY": "localhost",
    "SSL_CERT_FILE": "/etc/ssl/ca.crt",
    "NODE_EXTRA_CA_CERTS": "/etc/ssl/ca.crt",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/ca.crt",
}


def test_child_env_keeps_the_allowlist_and_drops_the_stack_secrets() -> None:
    env = child_env({**KEPT_ENV, **STACK_SECRETS, "KEYCLOAK_URL": "http://x", "SOME_OTHER": "1"})

    assert env == KEPT_ENV
    assert not set(STACK_SECRETS) & set(env)
    assert not any(name.startswith("KEYCLOAK_") for name in env)


def test_child_env_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", STACK_SECRETS["DATABASE_PASSWORD"])
    monkeypatch.setenv("AUTH_DEV_SIGNING_KEY", STACK_SECRETS["AUTH_DEV_SIGNING_KEY"])
    monkeypatch.setenv("KEYCLOAK_QA_PASSWORD", STACK_SECRETS["KEYCLOAK_QA_PASSWORD"])
    monkeypatch.setenv("HOME", "/home/qa")

    env = child_env()

    assert {"PATH", "HOME"} <= set(env)
    assert not set(STACK_SECRETS) & set(env)


ENV_PROBE = (
    "import os, sys\n"
    "leaked = [n for n in os.environ if n in {names} or n.startswith('KEYCLOAK_')]\n"
    "print('LEAK' if leaked or 'PATH' not in os.environ or 'HOME' not in os.environ else 'CLEAN')\n"
    "sys.exit(0 if not leaked else 9)\n"
).replace("{names}", repr(set(STACK_SECRETS)))


@pytest.fixture
def stack_secrets_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in STACK_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KEYCLOAK_URL", "http://x")
    monkeypatch.setenv("HOME", str(Path.home()))


@pytest.mark.usefixtures("stack_secrets_in_environment")
async def test_every_spawner_hands_the_child_the_allowlisted_environment_only(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", ENV_PROBE]

    code, out = await capture_command(argv, tmp_path)
    assert (code, out.strip()) == (0, "CLEAN")

    assert await run_command(argv, tmp_path) == 0

    run = await run_prompt(argv, tmp_path, timeout_s=30)
    assert (run.exit_code, run.lines) == (0, ["CLEAN"])

    async def silent(_output: str) -> str | None:
        return None

    outcome = await run_pty(argv, silent, lambda output: "CLEAN" in output, timeout_s=20, cwd=tmp_path)
    assert (outcome.completed, outcome.exit_code, "LEAK" in outcome.tail) == (True, 0, False)


# Only our two tools (R2) -----------------------------------------------------------------------------------------


def test_the_prompt_argv_denies_the_builtin_tools_that_read_files_the_environment_or_the_network() -> None:
    argv = claude_prompt_argv("hello", ["mcp__a__x"])
    denied = set(argv[argv.index("--disallowedTools") + 1].split(","))

    assert {"Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Glob", "Grep", "NotebookEdit", "Task"} <= denied
    assert argv.index("--disallowedTools") < argv.index("--allowedTools") == len(argv) - 2
    assert denied == set(CLAUDE_DISALLOWED_TOOLS)
    assert not denied & {"mcp__a__x"}


def tool_call(name: str | None, identifier: str = "u") -> ToolEvent:
    return ToolEvent("call", identifier, name, {}, is_error=False)


def test_foreign_tool_calls_names_every_call_that_is_not_ours_or_a_tool_search() -> None:
    ours = ("mcp__s__list_projects", "mcp__s__list_features")
    events = [
        tool_call("mcp__s__list_projects"),
        tool_call("ToolSearch"),
        tool_call("Bash"),
        tool_call("mcp__other__list_projects"),
        tool_call("mcp__s__list_features"),
        ToolEvent("result", "u", "Read", "x", is_error=False),  # a result is not a call
    ]

    assert foreign_tool_calls(events, ours) == ["Bash", "mcp__other__list_projects"]
    assert foreign_tool_calls(events[:2] + events[4:], ours) == []


# Latency error handling (R7, R8) ---------------------------------------------------------------------------------


class Probe(BaseModel):
    count: int


def validation_error() -> Exception:
    try:
        Probe.model_validate({"count": "many"})
    except ValueError as error:  # pydantic's ValidationError is a ValueError
        return error
    raise AssertionError("the probe model accepted a bad value")


async def test_run_latency_counts_sdk_stream_and_validation_errors_as_call_errors_and_reports_progress() -> None:
    failures = iter([anyio.BrokenResourceError(), anyio.ClosedResourceError(), validation_error(), None])
    progress = LatencyProgress()

    async def flaky() -> bool:
        failure = next(failures)
        if failure is not None:
            raise failure
        return True

    stats = await run_latency(flaky, rate_per_s=100, duration_s=0.04, progress=progress)

    assert (stats.calls, stats.samples, stats.errors) == (4, 4, 3)
    assert progress.metrics("e14") == {"e14_started": 4, "e14_completed": 4, "e14_errors": 3}


async def test_run_latency_leaves_partial_progress_when_the_run_itself_fails() -> None:
    progress = LatencyProgress()

    async def crashing() -> bool:
        raise RuntimeError("not a call error")

    with pytest.raises(RuntimeError, match="not a call error"):
        await run_latency(crashing, rate_per_s=100, duration_s=0.05, progress=progress)

    assert progress.started >= 1
    assert progress.completed <= progress.started


def test_the_teardown_verdict_reads_not_reached_yes_or_no() -> None:
    assert [TeardownVerdict(clean).label for clean in (None, True, False)] == ["not reached", "yes", "no"]


# G2: direct tests of small library helpers (MCP-M7; the live tier exercises them end to end) -----------------------


def test_preregistered_client_is_the_public_specflow_mcp_client_so_no_registration_is_sent() -> None:
    client = preregistered_client()

    assert (client.client_id, client.token_endpoint_auth_method) == ("specflow-mcp", "none")
    assert client.client_id == MCP_CLIENT_ID
    assert [str(uri) for uri in client.redirect_uris or []] == [MCP_REDIRECT_URI]
    assert client.client_secret is None


async def test_the_memory_token_storage_starts_with_the_preregistered_client_and_no_tokens() -> None:
    storage = MemoryTokenStorage()

    info = await storage.get_client_info()
    assert info is not None and info.client_id == MCP_CLIENT_ID
    assert await storage.get_tokens() is None


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"error": {"code": "project_not_found", "message": "m"}}, "project_not_found"),
        ({"error": {"code": 42}}, "42"),
        ({"error": {"message": "no code"}}, None),
        ({"error": "plain text"}, None),
        ({"items": []}, None),
    ],
    ids=["code", "numeric-code", "no-code", "not-an-object", "no-error"],
)
def test_tool_outcome_reads_the_error_code_from_the_structured_error(data: dict[str, Any], code: str | None) -> None:
    outcome = ToolOutcome(is_error=code is not None, data=data)

    assert outcome.code == code
    assert outcome.data is data


def test_pkce_pair_has_a_charset_safe_verifier_and_the_s256_challenge_of_it() -> None:
    verifier, challenge = pkce_pair()

    assert 43 <= len(verifier) <= 128  # RFC 7636 section 4.1
    assert re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert challenge == expected and "=" not in challenge and len(challenge) == 43
    assert pkce_pair()[0] != verifier  # fresh randomness every time


def test_pkce_pair_matches_the_rfc_7636_appendix_b_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    # The only fake is the random source: the RFC's verifier must give the RFC's challenge.
    rfc_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    monkeypatch.setattr(mcp_qa_lib.secrets, "token_urlsafe", lambda _n: rfc_verifier)

    assert pkce_pair() == (rfc_verifier, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("\x1b[31mred\x1b[0m text", "red text"),
        ("\x1b]8;;https://example.invalid/x\x07link\x1b]8;;\x07", "link"),
        ("\x1b]8;;https://example.invalid/x\x1b\\link\x1b]8;;\x1b\\", "link"),
        ("\x1b[2K\x1b[1Gprogress", "progress"),
        ("nothing to strip", "nothing to strip"),
    ],
    ids=["csi-colour", "osc8-bel", "osc8-st", "csi-cursor", "plain"],
)
def test_strip_ansi_removes_hyperlink_and_colour_sequences(text: str, plain: str) -> None:
    assert strip_ansi(text) == plain


def call_failure(error_class: type[BaseException]) -> BaseException:
    factories: dict[type[BaseException], Callable[[], BaseException]] = {
        QaError: lambda: QaError("sign-in failed"),
        MCPError: lambda: MCPError(code=-32000, message="server said no"),
        httpx2.HTTPError: lambda: httpx2.ConnectError("refused"),
        OSError: lambda: ConnectionResetError("reset"),
        anyio.BrokenResourceError: anyio.BrokenResourceError,
        anyio.ClosedResourceError: anyio.ClosedResourceError,
    }
    return validation_error() if error_class not in factories else factories[error_class]()


def test_every_call_error_class_is_a_class_the_timed_cases_know() -> None:
    known = {QaError, MCPError, httpx2.HTTPError, OSError, anyio.BrokenResourceError, anyio.ClosedResourceError}

    assert known <= set(_CALL_ERRORS)  # a new class is welcome; a dropped one is a regression
    assert pydantic.ValidationError in _CALL_ERRORS


@pytest.mark.parametrize("error_class", _CALL_ERRORS, ids=lambda error_class: error_class.__name__)
async def test_timed_counts_each_call_error_class_as_an_error_without_raising(
    error_class: type[BaseException],
) -> None:
    progress = LatencyProgress()
    failure = call_failure(error_class)
    assert isinstance(failure, error_class)

    async def failing() -> bool:
        raise failure

    elapsed, ok = await _timed(failing, progress)

    assert ok is False and elapsed >= 0
    assert (progress.completed, progress.errors) == (1, 1)


async def test_timed_counts_a_success_and_a_false_result_and_lets_other_exceptions_propagate() -> None:
    progress = LatencyProgress()

    async def succeeds() -> bool:
        return True

    async def declines() -> bool:
        return False

    async def crashes() -> bool:
        raise RuntimeError("not a call error")

    assert (await _timed(succeeds, progress))[1] is True
    assert (await _timed(declines, progress))[1] is False
    assert (progress.completed, progress.errors) == (2, 1)
    with pytest.raises(RuntimeError, match="not a call error"):
        await _timed(crashes, progress)
    assert progress.completed == 2  # a propagated exception is not counted as a finished call
    assert (await _timed(succeeds))[1] is True  # progress is optional


async def test_add_server_runs_claude_mcp_add_in_the_temp_folder_and_names_a_failed_add(tmp_path: Path) -> None:
    seen: list[tuple[list[str], Path]] = []
    exits = iter([0, 1])

    async def runner(argv: Sequence[str], cwd: Path) -> int:
        seen.append((list(argv), cwd))
        return next(exits)

    await _add_server(runner, "specflow-qa-x", MCP_RESOURCE_URL, tmp_path)
    with pytest.raises(QaError, match="claude mcp add failed"):
        await _add_server(runner, "specflow-qa-x", "http://localhost:9/mcp", tmp_path)

    assert seen[0] == (claude_add_argv("specflow-qa-x", MCP_RESOURCE_URL), tmp_path)
    assert seen[1][0] == claude_add_argv("specflow-qa-x", "http://localhost:9/mcp")
    argv = seen[0][0]
    assert argv[argv.index("--client-id") + 1] == MCP_CLIENT_ID
    assert argv[argv.index("--transport") + 1] == "http" and argv[-1] == MCP_RESOURCE_URL


async def test_seeded_ids_needs_the_keycloak_admin_password_before_any_connection() -> None:
    # Only the offline refusal is testable: the lookup itself talks to Keycloak and Postgres (covered live, E-tier).
    with pytest.raises(SystemExit, match="KEYCLOAK_ADMIN_PASSWORD"):
        await seeded_ids("keycloak", environ={})


# T1: persona assertions of the agent tier, fed canned stream-json through the real `run_prompt` parser --------------

AGENT_NAME = "specflow-qa-run1"
AGENT_TOOLS = claude_tool_names(AGENT_NAME)
LIST_PROJECTS, LIST_FEATURES = AGENT_TOOLS
AGENT_IDS = FixtureIds(
    {"alice": uuid.uuid4()}, {name: uuid.uuid4() for name in ("P-Alpha", "P-Beta", "P-Empty", "P-Large")}
)
ALPHA_ID = str(AGENT_IDS.projects["P-Alpha"])
BETA_ID = str(AGENT_IDS.projects["P-Beta"])
ALPHA_SPEC_IDS = sorted(
    node.spec_id for node in fixture_nodes("P-Alpha", ALPHA_ID, load_ontology()) if node.label == "Feature"
)
BETA_FEATURE_NAME = "P-Beta Feature 001"
NO_CURSOR: dict[str, Any] = {"next_cursor": None}
REPLAY_CHILD = "import sys\nsys.stdout.write(open(sys.argv[1], encoding='utf-8').read())\n"


def init_message(*, status: str = "connected", server: str = AGENT_NAME, tools: Sequence[str] = AGENT_TOOLS) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "mcp_servers": [{"name": server, "status": status}],
        "tools": list(tools),
    }


def stream_call(use_id: str, tool: str, arguments: dict[str, Any]) -> dict:
    block = {"type": "tool_use", "id": use_id, "name": tool, "input": arguments}
    return {"type": "assistant", "message": {"content": [block]}}


def stream_result(use_id: str, data: dict[str, Any], *, is_error: bool = False) -> dict:
    block = {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": [{"type": "text", "text": json.dumps(data)}],
        "is_error": is_error,
    }
    return {"type": "user", "message": {"content": [block]}}


def result_message(subtype: str = "success", prose: str = "done", *, is_error: bool = False) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": prose,
        "num_turns": 3,
        "total_cost_usd": 0.0421,
    }


async def streamed(tmp_path: Path, *messages: dict) -> PromptRun:
    """A `PromptRun` parsed by the real `run_prompt` from a child that prints the canned messages."""

    transcript = tmp_path / "stream.jsonl"
    transcript.write_text("\n".join(json.dumps(message) for message in messages) + "\n", encoding="utf-8")
    return await run_prompt([sys.executable, "-c", REPLAY_CHILD, str(transcript)], tmp_path, timeout_s=30)


def projects_run(*names: str, tools: Sequence[str] = AGENT_TOOLS) -> list[dict]:
    items = [{"project_id": str(uuid.uuid4()), "name": name} for name in names]
    return [
        init_message(tools=tools),
        stream_call("u1", LIST_PROJECTS, {}),
        stream_result("u1", {"items": items, **NO_CURSOR}),
        result_message(),
    ]


def features_page(spec_ids: Sequence[str], cursor: str | None) -> dict[str, Any]:
    return {"items": [{"spec_id": spec_id, "name": spec_id} for spec_id in spec_ids], "next_cursor": cursor}


def alpha_run(*extra: dict, second_cursor: str | None = None) -> list[dict]:
    return [
        init_message(),
        stream_call("u1", LIST_FEATURES, {"project_id": ALPHA_ID, "limit": 5}),
        stream_result("u1", features_page(ALPHA_SPEC_IDS[:5], "page-2")),
        stream_call("u2", LIST_FEATURES, {"project_id": ALPHA_ID, "limit": 5, "cursor": "page-2"}),
        stream_result("u2", features_page(ALPHA_SPEC_IDS[5:], second_cursor)),
        *extra,
        result_message(),
    ]


def refusal(code: str = "project_not_found") -> dict[str, Any]:
    return {"error": {"code": code, "message": "Project not found."}}


def beta_run(*, code: str = "project_not_found", prose: str = "I cannot see that project.") -> list[dict]:
    return [
        init_message(),
        stream_call("u1", LIST_FEATURES, {"project_id": BETA_ID}),
        stream_result("u1", refusal(code), is_error=True),
        result_message(prose=prose),
    ]


async def test_each_persona_check_accepts_a_good_run_agent_tier(tmp_path: Path) -> None:
    runs = {
        "agent-1": await streamed(tmp_path, *projects_run("P-Alpha", "P-Empty", "P-Large")),
        "agent-2": await streamed(tmp_path, *alpha_run()),
        "agent-3": await streamed(tmp_path, *beta_run()),
    }

    assert len(ALPHA_SPEC_IDS) == 7
    for persona, run in runs.items():
        assert check_run(run, AGENT_NAME, AGENT_TOOLS) == RunTotals(0.0421, 3)
        PERSONA_CHECKS[persona](run, AGENT_TOOLS, AGENT_IDS)


async def test_agent_2_matches_results_to_calls_by_tool_use_id(tmp_path: Path) -> None:
    # A list_features call for another project (and its extra feature ids) must not leak into P-Alpha's set.
    other = [
        stream_call("u3", LIST_FEATURES, {"project_id": str(AGENT_IDS.projects["P-Empty"])}),
        stream_result("u3", features_page(["FEAT::from-another-project"], None)),
    ]
    run = await streamed(tmp_path, *alpha_run(*other))

    alpha_features_listed(run, AGENT_TOOLS, AGENT_IDS)


@pytest.mark.parametrize(
    ("persona", "messages", "reason"),
    [
        ("agent-1", projects_run("P-Alpha", "P-Empty"), "projects listed"),
        ("agent-1", projects_run("P-Alpha", "P-Empty", "P-Large", "P-Beta"), "P-Beta is listed"),
        ("agent-1", [init_message(), result_message()], "list_projects was never called"),
        ("agent-2", alpha_run(second_cursor="more"), "not read to the last page"),
        ("agent-2", [init_message(), result_message()], "never called for"),
        (
            "agent-2",
            [
                init_message(),
                stream_call("u1", LIST_FEATURES, {"project_id": ALPHA_ID}),
                stream_result("u1", features_page(ALPHA_SPEC_IDS[:5], None)),
                result_message(),
            ],
            "P-Alpha features: 5 found, 7 expected",
        ),
        (
            "agent-2",
            alpha_run(
                stream_call("u3", LIST_PROJECTS, {}),
                stream_result("u3", {"items": [{"spec_id": "REQ::FR1"}], **NO_CURSOR}),
            ),
            "REQ:: id",
        ),
        ("agent-3", beta_run(code="invalid_cursor"), "P-Beta answers"),
        ("agent-3", beta_run(prose=f"The project holds {BETA_FEATURE_NAME}."), "P-Beta feature name appears"),
        ("agent-3", [init_message(), result_message()], "never called for"),
    ],
    ids=[
        "p1-missing-project",
        "p1-lists-beta",
        "p1-never-called",
        "p2-more-pages",
        "p2-never-called",
        "p2-short-set",
        "p2-req-id",
        "p3-wrong-code",
        "p3-beta-name-in-the-stream",
        "p3-never-called",
    ],
)
async def test_a_persona_check_names_what_went_wrong(
    tmp_path: Path, persona: str, messages: list[dict], reason: str
) -> None:
    run = await streamed(tmp_path, *messages)

    with pytest.raises(QaError, match=reason) as failed:
        PERSONA_CHECKS[persona](run, AGENT_TOOLS, AGENT_IDS)

    assert not isinstance(failed.value, RunPreconditionError)


async def test_a_p_beta_feature_name_in_a_tool_result_fails_agent_3(tmp_path: Path) -> None:
    leaked = beta_run()
    leaked[2] = stream_result("u1", {"error": refusal()["error"], "hint": BETA_FEATURE_NAME}, is_error=True)

    with pytest.raises(QaError, match="P-Beta feature name appears"):
        beta_refused(await streamed(tmp_path, *leaked), AGENT_TOOLS, AGENT_IDS)


@pytest.mark.parametrize(
    ("messages", "reason"),
    [
        (projects_run("P-Alpha")[:-1], "no model access or budget: None"),
        (
            [*projects_run("P-Alpha")[:-1], result_message("error_max_budget_usd", is_error=True)],
            "error_max_budget_usd",
        ),
        ([*projects_run("P-Alpha")[:-1], result_message("error_during_execution")], "error_during_execution"),
        ([init_message(status="failed"), result_message()], "not connected: failed"),
        ([init_message(server="specflow-qa-other"), result_message()], "not connected: None"),
        ([result_message()], "not connected: None"),
        ([init_message(tools=[LIST_PROJECTS]), result_message()], "tool names"),
        ([init_message(tools=["Bash", "Read"]), result_message()], "tool names"),
    ],
    ids=[
        "no-result",
        "budget",
        "execution-error",
        "failed-server",
        "other-server",
        "no-init",
        "one-tool",
        "no-mcp-tools",
    ],
)
async def test_check_run_raises_a_precondition_for_a_run_that_cannot_answer(
    tmp_path: Path, messages: list[dict], reason: str
) -> None:
    run = await streamed(tmp_path, *messages)

    with pytest.raises(RunPreconditionError, match=reason):
        check_run(run, AGENT_NAME, AGENT_TOOLS)


async def test_check_run_refuses_a_timed_out_run_before_reading_anything_else(tmp_path: Path) -> None:
    run = replace(await streamed(tmp_path, *projects_run("P-Alpha")), timed_out=True)

    with pytest.raises(RunPreconditionError, match="timed out"):
        check_run(run, AGENT_NAME, AGENT_TOOLS)


async def test_check_run_fails_a_foreign_tool_call_but_not_a_precondition(tmp_path: Path) -> None:
    messages = projects_run("P-Alpha", "P-Empty", "P-Large")
    messages.insert(-1, stream_call("u9", "Bash", {"command": "ls"}))
    run = await streamed(tmp_path, *messages)

    with pytest.raises(QaError, match=r"outside our two tools: \['Bash'\]") as failed:
        check_run(run, AGENT_NAME, AGENT_TOOLS)

    assert not isinstance(failed.value, RunPreconditionError)


async def test_check_run_reads_the_init_tools_of_a_run_that_lists_extra_tools(tmp_path: Path) -> None:
    run = await streamed(tmp_path, *projects_run("P-Alpha", tools=[*AGENT_TOOLS, "Bash", "mcp__other__tool"]))

    assert check_run(run, AGENT_NAME, AGENT_TOOLS).turns == 3


# T2 and T3: the E14 partial numbers and the report saver ----------------------------------------


async def test_run_latency_into_leaves_the_partial_numbers_when_the_run_itself_dies_e14() -> None:
    metrics: dict[str, float | int | str] = {"mode": "oidc"}

    async def crashing() -> bool:
        raise RuntimeError("not a call error")

    with pytest.raises(RuntimeError, match="not a call error"):
        await run_latency_into(metrics, crashing, rate_per_s=100, duration_s=0.05)

    assert metrics["mode"] == "oidc"
    assert int(metrics["e14_started"]) >= 1
    assert {"e14_started", "e14_completed", "e14_errors"} <= set(metrics)
    assert "e14_p95_ms" not in metrics


async def test_run_latency_into_records_the_stats_of_a_finished_run_under_the_prefix() -> None:
    metrics: dict[str, float | int | str] = {}

    async def fine() -> bool:
        return True

    stats = await run_latency_into(metrics, fine, prefix="x", rate_per_s=100, duration_s=0.03)

    assert (stats.calls, stats.errors) == (3, 0)
    assert metrics["x_calls"] == 3 and metrics["x_errors"] == 0
    assert {"x_p50_ms", "x_p95_ms", "x_max_ms", "x_rate_per_s"} <= set(metrics)


def refused_report() -> RunReport:
    return RunReport("run-refused", "agent", [ScenarioResult("agent-1", "failed", 1.0, "mail someone@example.com")])


def test_save_report_writes_the_report_and_never_logs_its_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mcp_qa_lib, "write_report", functools.partial(write_report, directory=tmp_path))
    report = RunReport("run-saved", "agent", [ScenarioResult("agent-1", "passed", 1.0)])

    save_report(report, raise_on_refusal=True)

    assert json.loads((tmp_path / "run-saved.json").read_text(encoding="utf-8"))["run_id"] == "run-saved"


def test_save_report_raises_a_refusal_only_when_no_other_error_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mcp_qa_lib, "write_report", functools.partial(write_report, directory=tmp_path))

    with pytest.raises(QaError, match="report refused"):
        save_report(refused_report(), raise_on_refusal=True)
    save_report(refused_report(), raise_on_refusal=False)  # an error is already propagating: the refusal is dropped

    assert list(tmp_path.iterdir()) == []


def fail_then_save_a_refused_report() -> None:
    try:
        raise ValueError("the real failure")
    finally:
        save_report(refused_report(), raise_on_refusal=False)


def test_a_refused_report_in_a_finally_never_masks_the_body_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mcp_qa_lib, "write_report", functools.partial(write_report, directory=tmp_path))

    with pytest.raises(ValueError, match="the real failure"):
        fail_then_save_a_refused_report()
