"""Offline tests of the MCP QA library (`examples/mcp_qa_lib.py`, docs/mcp-server/testing.md).

External boundaries only are faked: Keycloak's HTTP API through `httpx.MockTransport` (one `FakeKeycloak` handler for
every test) and the `claude` CLI through the `CommandRunner` seam. Terminal handling runs against a real
`sys.executable` child. Nothing here needs the network, Keycloak, Docker or `claude`.
"""

from __future__ import annotations

import asyncio
import base64
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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pytest
from pydantic import SecretStr

import mcp_qa_lib
from db.models import ProjectMember
from graph.core import load_ontology
from mcp_qa_lib import (
    ISSUER,
    KEYCLOAK_URL,
    MCP_RESOURCE_URL,
    QA_USERS,
    REPORTS_DIR,
    WEB_CLIENT_ID,
    KeycloakAdmin,
    QaError,
    RunReport,
    ScenarioResult,
    apply_local_defaults,
    claude_add_argv,
    claude_login,
    claude_login_argv,
    claude_logout_argv,
    claude_prompt_argv,
    claude_remove_argv,
    complete_login,
    dev_token,
    find_authorization_url,
    fixture_ids,
    fixture_nodes,
    headers_json,
    neo4j_driver,
    new_run_id,
    parse_stream_json,
    port_is_free,
    registered_server,
    remove_member,
    require_env,
    restore_member,
    run_command,
    run_pty,
    server_name,
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
        *("--allowedTools", "mcp__a__x,mcp__a__y"),
    ]


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
