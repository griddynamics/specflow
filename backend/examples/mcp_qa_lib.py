"""QA library of the MCP server (docs/mcp-server/testing.md): Keycloak admin, login and token helpers, dev tokens.

Used by `examples/mcp_qa.py` (commands) and, later, by the end-to-end and agent QA tiers. Everything here talks to
the local Keycloak at `http://localhost:8180` (`make up WITH=keycloak`) and never prints or logs a token, password,
cookie, email, authorization code or redirect URL: errors name the step and the HTTP status only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import html
import json
import math
import os
import pty
import re
import secrets
import shutil
import signal
import socket
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import anyio
import httpx
import httpx2
import jwt
from jsonschema import Draft202012Validator
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.exceptions import MCPError
from mcp.types import LATEST_PROTOCOL_VERSION, GetPromptResult, TextContent
from neo4j import AsyncDriver
from pydantic import AnyUrl, SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Project, ProjectMember, User
from db.repositories import create_project, upsert_user
from db.session import build_engine, build_session_factory
from graph.core import (
    GraphReader,
    LLMConfig,
    Neo4jConfig,
    Ontology,
    OwnedWriter,
    connect,
    ensure_schema,
    load_ontology,
)
from logger.logger import INFO, get_logger, log_event
from settings import GRAPH_EMBEDDING_DIM, DatabaseSettings

logger = get_logger("examples.mcp_qa")

KEYCLOAK_URL = "http://localhost:8180"
REALM = "specflow"
ISSUER = f"{KEYCLOAK_URL}/realms/{REALM}"
MCP_RESOURCE_URL = "http://localhost:8002/mcp"
MCP_CLIENT_ID = "specflow-mcp"
WEB_CLIENT_ID = "specflow-web"
CALLBACK_PORT = 8765
MCP_REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
WEB_REDIRECT_URI = "http://localhost:3001/api/auth/callback/oidc"
ADMIN_USER = "admin"
QA_USERS = {"alice": "alice@example.com", "bob": "bob@example.com", "carol": "carol@other.example"}
LOCAL_DEFAULTS = {
    "DATABASE_HOST": "localhost",
    "DATABASE_PORT": "5433",
    "DATABASE_NAME": "specflow",
    "DATABASE_USER": "specflow",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
}
DEV_ISSUER = "specflow-dev-login"
WEB_AUDIENCE = "specflow-api"
DEV_TOKEN_TTL_SECONDS = 8 * 3600
KEYCLOAK_HINT = "run `make env-local`, then recreate keycloak (`... up -d --force-recreate keycloak`)"

PRM_URL = "http://localhost:8002/.well-known/oauth-protected-resource/mcp"
API_URL = "http://localhost:8000"
QA_CLIENT_SCOPE = "openid"
PUBLIC_CLIENT_AUTH: Final = "none"  # RFC 7591 token_endpoint_auth_method of a public client
SERVER_PREFIX = "specflow-qa-"

Client = Literal["mcp", "web"]
StackMode = Literal["oidc", "dev"]
_TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
_FORM = re.compile(r'<form[^>]*\baction="([^"]+)"', re.IGNORECASE)
_REDIRECT_STATUSES = (302, 303)


class QaError(RuntimeError):
    """A QA step failed; the message names the step and a status, never a secret, an email or a URL."""


def apply_local_defaults(environ: MutableMapping[str, str]) -> None:
    """Fill the host-side connection names that `.env.local` (passwords only) does not carry."""

    for name, value in LOCAL_DEFAULTS.items():
        environ.setdefault(name, value)


def require_env(*names: str, environ: MutableMapping[str, str] | None = None) -> dict[str, SecretStr]:
    """Return the named variables as secrets; raise `SystemExit` naming the missing names only."""

    source = os.environ if environ is None else environ
    missing = [name for name in names if not source.get(name, "").strip()]
    if missing:
        hint = f"; {KEYCLOAK_HINT}" if any(name.startswith("KEYCLOAK_") for name in missing) else ""
        listed = ", ".join(missing)
        raise SystemExit(f"missing environment variables: {listed} (run with `uv run --env-file ../.env.local`){hint}")
    return {name: SecretStr(source[name].strip()) for name in names}


async def _send(http: httpx.AsyncClient, operation: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One HTTP call; transport errors become a `QaError` naming the operation and the exception type."""

    try:
        return await http.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise QaError(f"{operation} failed: {type(error).__name__}") from None


def _expect(response: httpx.Response, operation: str, *statuses: int) -> httpx.Response:
    if response.status_code not in statuses:
        raise QaError(f"{operation} failed: HTTP {response.status_code}")
    return response


# Keycloak admin ------------------------------------------------------------------------------------------------


class KeycloakAdmin:
    """Realm administration through the REST API with a `master` admin token (no kcadm, no docker exec)."""

    def __init__(self, admin_password: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._password = admin_password
        self._http = httpx.AsyncClient(base_url=KEYCLOAK_URL, transport=transport, timeout=30)
        self._bearer: str | None = None

    async def __aenter__(self) -> KeycloakAdmin:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def _call(self, operation: str, method: str, path: str, *ok: int, **kwargs: Any) -> httpx.Response:
        if self._bearer is None:
            self._bearer = await self._login()
        headers = {"Authorization": f"Bearer {self._bearer}"}
        response = await _send(self._http, operation, method, f"/admin/realms/{REALM}{path}", headers=headers, **kwargs)
        return _expect(response, operation, *ok)

    async def _login(self) -> str:
        form = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": ADMIN_USER,
            "password": self._password.get_secret_value(),
        }
        response = await _send(
            self._http, "admin login", "POST", "/realms/master/protocol/openid-connect/token", data=form
        )
        return str(_expect(response, "admin login", 200).json()["access_token"])

    async def user_id(self, key: str) -> str | None:
        """The Keycloak id of the QA user `key` (`alice`), or None when it does not exist."""

        params = {"email": QA_USERS[key], "exact": "true"}
        found = (await self._call("find user", "GET", "/users", 200, params=params)).json()
        return str(found[0]["id"]) if found else None

    async def ensure_user(self, key: str, password: SecretStr) -> str:
        """Create or update the QA user (verified email, both names set so sign-in works) and reset its password."""

        email = QA_USERS[key]
        fields = {
            "username": email,
            "email": email,
            "emailVerified": True,
            "enabled": True,
            "firstName": key.title(),
            "lastName": "QA",
            "requiredActions": [],
        }
        user_id = await self.user_id(key)
        if user_id is None:
            created = await self._call("create user", "POST", "/users", 201, json=fields)
            location = created.headers.get("location")
            if location is None:
                raise QaError("create user failed: no location header")
            user_id = location.rsplit("/", 1)[1]
        else:
            await self._call("update user", "PUT", f"/users/{user_id}", 204, json=fields)
        credential = {"type": "password", "value": password.get_secret_value(), "temporary": False}
        await self._call("reset password", "PUT", f"/users/{user_id}/reset-password", 204, json=credential)
        log_event(logger, INFO, "mcp_qa.keycloak.user_ready", user=key)
        return user_id

    async def set_client_secret(self, client_id: str, secret: SecretStr) -> None:
        """Set the secret of a confidential client (the realm import generates a random one)."""

        found = (await self._call("find client", "GET", "/clients", 200, params={"clientId": client_id})).json()
        if not found:
            raise QaError("find client failed: not found")
        path = f"/clients/{found[0]['id']}"
        representation = (await self._call("read client", "GET", path, 200)).json()
        representation["secret"] = secret.get_secret_value()
        await self._call("set client secret", "PUT", path, 204, json=representation)
        log_event(logger, INFO, "mcp_qa.keycloak.client_secret_set", client=client_id)


# Login helper --------------------------------------------------------------------------------------------------


class _CookieJar:
    """Keycloak marks its cookies `Secure` on plain HTTP, so httpx drops them: forward every one by hand."""

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def remember(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            self._cookies[name.strip()] = rest.split(";", 1)[0]

    def headers(self) -> dict[str, str]:
        return {"cookie": "; ".join(f"{name}={value}" for name, value in self._cookies.items())}


def _next_form(
    response: httpx.Response, username: str, password: SecretStr, *, posted: bool
) -> tuple[str, dict[str, str]]:
    """The action URL and fields to post for the login or consent form on `response`."""

    step = f"HTTP {response.status_code}"
    match = _FORM.search(response.text)
    if match is None:
        raise QaError(f"login failed: no form on the page ({step})")
    action = urljoin(str(response.url), html.unescape(match.group(1)))
    if 'name="password"' in response.text:
        if posted:
            raise QaError(f"login failed: credentials rejected ({step})")
        return action, {"username": username, "password": password.get_secret_value(), "credentialId": ""}
    if 'name="accept"' in response.text:
        return action, {"accept": "Yes"}
    raise QaError(f"login failed: unexpected form ({step})")


async def complete_login(
    authorize_url: str,
    username: str,
    password: SecretStr,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_hops: int = 5,
) -> str:
    """Finish an authorization URL at Keycloak over plain HTTP; return the redirect URL (`code`, `state`, `iss`).

    The redirect target is the `redirect_uri` of `authorize_url` (the MCP callback or the web callback); it is
    never requested. Raises `QaError` naming only the step and the status.
    """

    target = parse_qs(urlsplit(authorize_url).query).get("redirect_uri", [""])[0]
    if not target:
        raise QaError("login failed: the authorization URL has no redirect_uri")
    jar = _CookieJar()
    posted = False
    async with httpx.AsyncClient(follow_redirects=False, timeout=15, transport=transport) as http:
        response = await _send(http, "login", "GET", authorize_url)
        for _ in range(max_hops):
            jar.remember(response)
            location = str(response.headers.get("location", ""))
            if response.status_code in _REDIRECT_STATUSES and location.startswith(target):
                return location
            if response.status_code in _REDIRECT_STATUSES:
                response = await _send(
                    http, "login", "GET", urljoin(str(response.url), location), headers=jar.headers()
                )
                continue
            action, data = _next_form(response, username, password, posted=posted)
            posted = posted or "password" in data
            response = await _send(http, "login", "POST", action, data=data, headers=jar.headers())
    raise QaError(f"login failed: no redirect after {max_hops} hops")


# Token helper --------------------------------------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str, scope: str = "openid") -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{ISSUER}/protocol/openid-connect/auth?{urlencode(query)}"


def _authorization_code(redirected: str, state: str) -> str:
    """The code of a redirect URL, after checking `state` and `iss` (RFC 9207)."""

    query = parse_qs(urlsplit(redirected).query)
    if query.get("state", [""])[0] != state:
        raise QaError("token failed: state mismatch")
    if query.get("iss", [""])[0] != ISSUER:
        raise QaError("token failed: issuer mismatch")
    code = query.get("code", [""])[0]
    if not code:
        raise QaError("token failed: no authorization code in the redirect")
    return code


async def user_token(
    key: str,
    password: SecretStr,
    *,
    client: Client = "mcp",
    web_secret: SecretStr | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Access token of a QA user: authorization code with PKCE S256; `web` adds the client secret (basic auth)."""

    if client == "web" and web_secret is None:
        raise QaError("token failed: the web client needs its secret")
    client_id, redirect_uri = (
        (MCP_CLIENT_ID, MCP_REDIRECT_URI) if client == "mcp" else (WEB_CLIENT_ID, WEB_REDIRECT_URI)
    )
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    url = authorization_url(client_id, redirect_uri, state, challenge)
    redirected = await complete_login(url, QA_USERS[key], password, transport=transport)
    form = {
        "grant_type": "authorization_code",
        "code": _authorization_code(redirected, state),
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    auth = httpx.BasicAuth(client_id, web_secret.get_secret_value()) if web_secret is not None else None
    async with httpx.AsyncClient(timeout=15, transport=transport) as http:
        response = await _send(http, "token", "POST", _TOKEN_ENDPOINT, data=form, auth=auth)
    return str(_expect(response, "token", 200).json()["access_token"])


# Dev tokens ----------------------------------------------------------------------------------------------------


def dev_token(email: str, signing_key: SecretStr, *, client: Client = "mcp", now: int | None = None) -> str:
    """HS256 dev token (docs/mcp-server/auth.md, Local and development mode; frontend/lib/dev-token.ts for `web`)."""

    issued = int(time.time()) if now is None else now
    audience, azp = (MCP_RESOURCE_URL, MCP_CLIENT_ID) if client == "mcp" else (WEB_AUDIENCE, WEB_CLIENT_ID)
    claims = {
        "iss": DEV_ISSUER,
        "aud": audience,
        "azp": azp,
        "typ": "Bearer",
        "sub": f"dev:{email}",
        "email": email,
        "email_verified": True,
        "name": email.split("@", maxsplit=1)[0],
        "iat": issued,
        "exp": issued + DEV_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, signing_key.get_secret_value(), algorithm="HS256")


def headers_json(token: str) -> str:
    """The `headersHelper` output of Claude Code: the request headers as one JSON object."""

    return json.dumps({"Authorization": f"Bearer {token}"})


# Seed ----------------------------------------------------------------------------------------------------------

Mode = Literal["keycloak", "dev"]
PROJECTS = {"P-Alpha": "alice", "P-Empty": "alice", "P-Large": "alice", "P-Beta": "bob"}
GRAPH: dict[str, dict[str, int]] = {
    "P-Alpha": {"Feature": 7, "Module": 2, "FunctionalRequirement": 3},
    "P-Beta": {"Feature": 3},
    "P-Empty": {},
    "P-Large": {"Feature": 300},
}
SEED_USERS = ("alice", "bob")  # carol is refused by domain before any database access
SEED_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "specflow:mcp-qa-seed")
_KINDS = {"Feature": "FEATURE", "Module": "MODULE", "FunctionalRequirement": "FR"}


@dataclass(frozen=True)
class SeedPrincipal:
    """The identity fields `db.repositories.upsert_user` reads."""

    subject: str
    email: str
    name: str


@dataclass(frozen=True)
class FixtureNode:
    """One spec node of the graph fixture: `uuid` is the writer key, `properties` the owned properties."""

    label: str
    spec_id: str
    uuid: str
    properties: dict[str, str]


@dataclass(frozen=True)
class FixtureIds:
    """Postgres ids of the fixture: users by key (`alice`) and projects by name (`P-Alpha`)."""

    users: dict[str, uuid.UUID]
    projects: dict[str, uuid.UUID]


@dataclass(frozen=True)
class SeedReport:
    """What a seed run left behind; `lines()` is the printed form (names, ids and counts only)."""

    mode: Mode
    keycloak_users: int | None
    users: int
    projects: int
    memberships: int
    graph: dict[str, tuple[uuid.UUID, dict[str, int]]] = field(default_factory=dict)

    def lines(self) -> list[str]:
        lines = [f"mode {self.mode}"]
        if self.keycloak_users is not None:
            lines.append(f"keycloak users {self.keycloak_users}")
        lines += [f"users {self.users}", f"projects {self.projects}", f"memberships {self.memberships}"]
        for name, (project_id, counts) in sorted(self.graph.items()):
            shown = ", ".join(f"{label} {count}" for label, count in counts.items()) or "no nodes"
            lines.append(f"{name} {project_id} {shown}")
        return lines


def fixture_nodes(project_name: str, project_id: str, ontology: Ontology) -> list[FixtureNode]:
    """The graph fixture of one project: deterministic spec ids and uuids, so a second seed run changes nothing."""

    short = project_name.removeprefix("P-").upper()
    nodes: list[FixtureNode] = []
    for label, count in GRAPH[project_name].items():
        prefix = ontology.id_prefix(label)
        for number in range(1, count + 1):
            spec_id = f"{prefix}{short}_{_KINDS[label]}_{number:03d}"
            properties = {
                "spec_id": spec_id,
                "name": f"{project_name} {label} {number:03d}",
                "stmt": f"QA fixture {label} {number:03d} of {project_name}.",
            }
            key = str(uuid.uuid5(SEED_NAMESPACE, f"{project_id}:{spec_id}"))
            nodes.append(FixtureNode(label, spec_id, key, properties))
    return nodes


def dev_subjects() -> dict[str, str]:
    """Dev mode subjects (`dev:<email>`) of the seeded users."""

    return {key: f"dev:{QA_USERS[key]}" for key in SEED_USERS}


@asynccontextmanager
async def postgres_session() -> AsyncIterator[AsyncSession]:
    """A session in one transaction (commit on exit, rollback on error) from `DATABASE_*`; disposes the engine."""

    engine = build_engine(DatabaseSettings.from_environment())
    try:
        async with build_session_factory(engine)() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def _ensure_member(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if await session.get(ProjectMember, (project_id, user_id)) is None:
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role="owner"))
        await session.flush()


async def _ensure_project(session: AsyncSession, user_id: uuid.UUID, name: str) -> uuid.UUID:
    """The oldest project `name` of the user (created when absent), with the owner membership restored."""

    existing = await session.scalar(
        select(Project.id)
        .where(Project.created_by == user_id, Project.name == name)
        .order_by(Project.created_at)
        .limit(1)
    )
    project_id = existing if existing is not None else (await create_project(session, user_id, name)).id
    await _ensure_member(session, project_id, user_id)
    return project_id


async def _seed_postgres(session: AsyncSession, subjects: Mapping[str, str]) -> FixtureIds:
    users: dict[str, uuid.UUID] = {}
    for key in SEED_USERS:
        principal = SeedPrincipal(subjects[key], QA_USERS[key], key.title())
        users[key] = (await upsert_user(session, principal)).id
    projects = {name: await _ensure_project(session, users[owner], name) for name, owner in PROJECTS.items()}
    return FixtureIds(users, projects)


async def fixture_ids(session: AsyncSession, mode: Mode, subjects: Mapping[str, str] | None = None) -> FixtureIds:
    """Look up the seeded users and projects; keycloak mode needs `subjects` (the Keycloak user ids by key)."""

    resolved = dev_subjects() if mode == "dev" and subjects is None else subjects
    if resolved is None:
        raise QaError("fixture lookup failed: keycloak mode needs the user subjects")
    users: dict[str, uuid.UUID] = {}
    for key in SEED_USERS:
        found = await session.scalar(select(User.id).where(User.auth_subject == resolved[key]))
        if found is None:
            raise QaError(f"fixture lookup failed: user {key} is not seeded")
        users[key] = found
    projects: dict[str, uuid.UUID] = {}
    for name, owner in PROJECTS.items():
        query = select(Project.id).where(Project.created_by == users[owner], Project.name == name)
        found = await session.scalar(query.order_by(Project.created_at).limit(1))
        if found is None:
            raise QaError(f"fixture lookup failed: project {name} is not seeded")
        projects[name] = found
    return FixtureIds(users, projects)


async def remove_member(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """E8: delete the `project_members` row (no repository function does); True when it existed."""

    member = await session.get(ProjectMember, (project_id, user_id))
    if member is None:
        return False
    await session.delete(member)
    await session.flush()
    return True


async def restore_member(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, role: str = "owner") -> bool:
    """E8: insert the row again; True when it was missing."""

    if await session.get(ProjectMember, (project_id, user_id)) is not None:
        return False
    session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
    await session.flush()
    return True


async def _count_postgres(session: AsyncSession, ids: FixtureIds, subjects: Mapping[str, str]) -> tuple[int, int, int]:
    users = await session.scalar(select(func.count()).select_from(User).where(User.auth_subject.in_(subjects.values())))
    projects = await session.scalar(
        select(func.count()).select_from(Project).where(Project.id.in_(ids.projects.values()))
    )
    members = await session.scalar(
        select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id.in_(ids.projects.values()))
    )
    return users or 0, projects or 0, members or 0


def _placeholder_llm_config(environ: Mapping[str, str]) -> LLMConfig:
    """`ensure_schema` only needs the embedding dimension; the model names are inert placeholders."""

    dimension = int(environ.get("GRAPH_EMBEDDING_DIM", "").strip() or GRAPH_EMBEDDING_DIM)
    return LLMConfig(
        endpoint="http://localhost:0",
        api_key=SecretStr("unused"),
        main_model="unused",
        small_model="unused",
        embedding_model="unused",
        embedding_dim=dimension,
    )


@asynccontextmanager
async def neo4j_driver(environ: Mapping[str, str]) -> AsyncIterator[AsyncDriver]:
    names = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
    missing = [name for name in names if not environ.get(name, "").strip()]
    if missing:
        raise QaError(
            "neo4j is not configured: set " + ", ".join(missing) + " (apply_local_defaults sets the first two)"
        )
    config = Neo4jConfig(
        uri=environ["NEO4J_URI"].strip(),
        user=environ["NEO4J_USER"].strip(),
        password=SecretStr(environ["NEO4J_PASSWORD"].strip()),
        database=environ.get("NEO4J_DATABASE", "").strip() or None,
    )
    driver = connect(config)
    try:
        yield driver
    finally:
        await driver.close()


async def _seed_graph(environ: Mapping[str, str], ids: FixtureIds) -> dict[str, dict[str, int]]:
    """Write the fixture through `OwnedWriter` as the build owner; return the node counts read back."""

    ontology = load_ontology()
    database = environ.get("NEO4J_DATABASE", "").strip() or None
    counts: dict[str, dict[str, int]] = {}
    async with neo4j_driver(environ) as driver:
        await ensure_schema(driver, ontology, _placeholder_llm_config(environ), database)
        reader = GraphReader(driver, ontology, database=database)
        for name, project_id in ids.projects.items():
            nodes = fixture_nodes(name, str(project_id), ontology)
            if nodes:
                writer = OwnedWriter(driver, ontology, "build", str(project_id), database=database)
                async with writer.transaction() as group:
                    for node in nodes:
                        await group.upsert_node(node.label, node.uuid, node.properties)
            counts[name] = {label: len(await reader.nodes(str(project_id), labels=[label])) for label in GRAPH[name]}
    return counts


async def _ensure_keycloak(environ: Mapping[str, str]) -> dict[str, str]:
    """Ensure the three QA users and the `specflow-web` secret; return the Keycloak user id of each QA user."""

    names = ("KEYCLOAK_ADMIN_PASSWORD", "KEYCLOAK_QA_PASSWORD", "KEYCLOAK_WEB_CLIENT_SECRET")
    secrets_ = require_env(*names, environ=dict(environ))
    async with KeycloakAdmin(secrets_["KEYCLOAK_ADMIN_PASSWORD"]) as admin:
        ids = {key: await admin.ensure_user(key, secrets_["KEYCLOAK_QA_PASSWORD"]) for key in QA_USERS}
        await admin.set_client_secret(WEB_CLIENT_ID, secrets_["KEYCLOAK_WEB_CLIENT_SECRET"])
    return ids


async def seed(mode: Mode, environ: Mapping[str, str] | None = None) -> SeedReport:
    """Idempotent seed of Keycloak (keycloak mode only), Postgres and Neo4j; a second run reports the same counts.

    Subjects are the Keycloak user ids, or `dev:<email>` in dev mode. The two modes create separate Postgres rows
    (`auth_subject` is unique), so counts are per mode. Postgres is read from `DATABASE_*` of the process environment.

    Idempotence holds for one Keycloak lifetime: a recreated Keycloak issues new user ids, so the next run creates
    new users, projects and graph groups (the old rows and nodes stay behind, filtered out of the counts).
    """

    source = os.environ if environ is None else environ
    keycloak_ids = await _ensure_keycloak(source) if mode == "keycloak" else None
    subjects = dev_subjects() if keycloak_ids is None else {key: keycloak_ids[key] for key in SEED_USERS}
    async with postgres_session() as session:
        ids = await _seed_postgres(session, subjects)
        users, projects, memberships = await _count_postgres(session, ids, subjects)
    graph = await _seed_graph(source, ids)
    log_event(logger, INFO, "mcp_qa.seed.done", mode=mode, users=users, projects=projects, memberships=memberships)
    detail = {name: (ids.projects[name], graph[name]) for name in PROJECTS}
    return SeedReport(mode, None if keycloak_ids is None else len(keycloak_ids), users, projects, memberships, detail)


# Stack, raw HTTP and the SDK client (E1 to E14) -------------------------------------------------------------------

_MCP_ACCEPT = "application/json, text/event-stream"
_OAUTH_ERROR = re.compile(r"[A-Za-z0-9_]{1,64}")


async def _prm_response(transport: httpx.AsyncBaseTransport | None) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            return await http.get(PRM_URL)
    except httpx.TransportError:
        raise QaError("MCP endpoint not reachable: run make up") from None
    except httpx.HTTPError as error:
        raise QaError(f"prm failed: {type(error).__name__}") from None


async def stack_mode(*, transport: httpx.AsyncBaseTransport | None = None) -> StackMode:
    """`oidc` when the protected resource metadata names the resource URL, `dev` when it is absent (404)."""

    response = await _prm_response(transport)
    if response.status_code == HTTPStatus.NOT_FOUND:
        return "dev"
    if response.status_code != HTTPStatus.OK:
        raise QaError(f"stack mode unknown: HTTP {response.status_code}")
    if _json_object(response).get("resource") != MCP_RESOURCE_URL:
        raise QaError("stack mode unknown: the metadata names another resource")
    return "oidc"


async def fetch_prm(*, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """The protected resource metadata document (E1)."""

    return _json_object(_expect(await _prm_response(transport), "prm", HTTPStatus.OK))


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def post_mcp(
    token: str | None,
    *,
    host: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    method: str = "initialize",
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """POST a JSON-RPC request (default `initialize`) to the MCP endpoint, with an optional bearer token and `Host`."""

    headers = {"Accept": _MCP_ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if host is not None:
        headers["Host"] = host
    initialize = {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "specflow-qa", "version": "0"},
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": initialize if params is None else params,
    }
    async with httpx.AsyncClient(timeout=15, transport=transport) as http:
        return await _send(http, "mcp request", "POST", MCP_RESOURCE_URL, json=body, headers=headers)


async def create_project_rest(
    name: str, web_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> uuid.UUID:
    """E13: create a project through the REST API with a web-audience token; return its id."""

    headers = {"Authorization": f"Bearer {web_token}"}
    async with httpx.AsyncClient(timeout=15, transport=transport) as http:
        response = await _send(
            http, "project create", "POST", f"{API_URL}/api/v1/projects", json={"name": name}, headers=headers
        )
    try:
        return uuid.UUID(str(_json_object(_expect(response, "project create", HTTPStatus.CREATED))["id"]))
    except (KeyError, ValueError):
        raise QaError("project create failed: no project id in the response") from None


async def seeded_ids(mode: Mode, environ: Mapping[str, str] | None = None) -> FixtureIds:
    """The Postgres ids of the seeded users and projects (read, never remembered: a Keycloak recreate changes them)."""

    if mode == "dev":
        async with postgres_session() as session:
            return await fixture_ids(session, "dev")
    source = dict(os.environ if environ is None else environ)
    password = require_env("KEYCLOAK_ADMIN_PASSWORD", environ=source)["KEYCLOAK_ADMIN_PASSWORD"]
    async with KeycloakAdmin(password) as admin:
        found = {key: await admin.user_id(key) for key in SEED_USERS}
    subjects = {key: subject for key, subject in found.items() if subject is not None}
    if len(subjects) != len(SEED_USERS):
        raise QaError("fixture lookup failed: the QA users are not in Keycloak (run the seed)")
    async with postgres_session() as session:
        return await fixture_ids(session, "keycloak", subjects)


def preregistered_client() -> OAuthClientInformationFull:
    """The `specflow-mcp` public client as the SDK stores a registered one: no registration request is sent."""

    return OAuthClientInformationFull(
        client_id=MCP_CLIENT_ID,
        redirect_uris=[AnyUrl(MCP_REDIRECT_URI)],
        token_endpoint_auth_method=PUBLIC_CLIENT_AUTH,
    )


class MemoryTokenStorage:
    """The SDK's `TokenStorage` in memory, with the pre-registered client information from the start."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = preregistered_client()

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


def narrow_scope(authorize_url: str, scope: str = QA_CLIENT_SCOPE) -> str:
    """The authorization URL with its `scope` replaced; every other parameter is kept.

    The protected resource metadata has no `scopes_supported`, so the SDK asks for every scope of the provider
    metadata and Keycloak answers `invalid_scope` for those not assigned to `specflow-mcp` (testing.md, Client notes).
    """

    parts = urlsplit(authorize_url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    narrowed = [(name, scope if name == "scope" else value) for name, value in pairs]
    if all(name != "scope" for name, _ in narrowed):
        narrowed.append(("scope", scope))
    return urlunsplit(parts._replace(query=urlencode(narrowed)))


def oauth_provider(
    key: str,
    password: SecretStr,
    *,
    mcp_url: str = MCP_RESOURCE_URL,
    login: Callable[[str], Awaitable[str]] | None = None,
) -> OAuthClientProvider:
    """The SDK's OAuth provider for QA user `key`: pre-registered client, scope narrowed, login over plain HTTP.

    `login(authorization url) -> redirect url` is the test seam (default: `complete_login`). Requests through the
    provider are serialized by the SDK, so load tests use a bearer header instead (`mcp_session(token=...)`).
    """

    async def sign_in(url: str) -> str:
        return await complete_login(url, QA_USERS[key], password)

    redirects: list[str] = []

    async def redirect_handler(url: str) -> None:
        redirects.append(await (login or sign_in)(narrow_scope(url)))

    async def callback_handler() -> AuthorizationCodeResult:
        query = parse_qs(urlsplit(redirects[-1] if redirects else "").query)
        code = query.get("code", [""])[0]
        if not code:
            error = query.get("error", [""])[0]
            raise QaError(f"sign-in failed: {error if _OAUTH_ERROR.fullmatch(error) else 'no code'}")
        return AuthorizationCodeResult(code=code, state=query.get("state", [None])[0], iss=query.get("iss", [None])[0])

    metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl(MCP_REDIRECT_URI)],
        token_endpoint_auth_method=PUBLIC_CLIENT_AUTH,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    return OAuthClientProvider(mcp_url, metadata, MemoryTokenStorage(), redirect_handler, callback_handler)


@asynccontextmanager
async def mcp_session(
    *,
    auth: httpx2.Auth | None = None,
    token: str | None = None,
    mcp_url: str = MCP_RESOURCE_URL,
    timeout_s: float = 30.0,
) -> AsyncIterator[ClientSession]:
    """An initialized SDK session over streamable HTTP; exactly one of `auth` (OAuth provider) or `token` (bearer)."""

    if (auth is None) == (token is None):
        raise QaError("mcp session needs exactly one of auth and token")
    headers = None if token is None else {"Authorization": f"Bearer {token}"}
    try:
        async with (
            httpx2.AsyncClient(auth=auth, headers=headers, timeout=timeout_s) as http,
            streamable_http_client(mcp_url, http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session
    except BaseExceptionGroup as group:
        # The SDK's task groups wrap an error of the body: hand the caller the error itself (an assertion, a QaError).
        leaves = _leaves(group)
        if len(leaves) != 1:
            raise
        raise leaves[0] from None


def _leaves(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    found: list[BaseException] = []
    for error in group.exceptions:
        found.extend(_leaves(error) if isinstance(error, BaseExceptionGroup) else [error])
    return found


@dataclass(frozen=True)
class ToolOutcome:
    """A tool result: `data` is its structured content (or the JSON of its first text block)."""

    is_error: bool
    data: dict[str, Any]

    @property
    def code(self) -> str | None:
        error = self.data.get("error")
        return str(error["code"]) if isinstance(error, dict) and "code" in error else None


async def call(session: ClientSession, tool: str, arguments: Mapping[str, Any]) -> ToolOutcome:
    result = await session.call_tool(tool, dict(arguments))
    data: Any = result.structured_content
    if data is None:
        text = next((block.text for block in result.content if isinstance(block, TextContent)), "")
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
    return ToolOutcome(bool(result.is_error), data if isinstance(data, dict) else {})


def prompt_text(result: GetPromptResult) -> str:
    """The text blocks of a `prompts/get` result, in order."""

    return "\n".join(m.content.text for m in result.messages if isinstance(m.content, TextContent))


async def all_projects(session: ClientSession) -> list[dict[str, Any]]:
    """Every project of the caller: `list_projects` pages until `next_cursor` is null."""

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        outcome = await call(session, "list_projects", {} if cursor is None else {"cursor": cursor})
        if outcome.is_error:
            raise QaError(f"list_projects failed: {outcome.code}")
        items.extend(outcome.data.get("items", []))
        cursor = outcome.data.get("next_cursor")
        if cursor is None:
            return items


# Workspace binding (testing.md, Temp workspace) --------------------------------------------------------------------

BINDING_FILE: Final = Path(".specflow") / "config.json"
BINDING_VERSION: Final = 1
BINDING_SCHEMA: Final = (
    Path(__file__).resolve().parents[1] / "contracts" / "schemas" / "specflow-workspace-config.schema.json"
)
WORKSPACE_PREFIX: Final = "specflow-qa-ws-"


def make_workspace() -> Path:
    """A fresh folder under the system temp directory (the caller deletes it)."""

    return Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()


def find_binding(start: Path) -> Path | None:
    """The nearest `.specflow/config.json` from `start` up to the filesystem root, or None."""

    origin = start.resolve()
    return next(
        (c for folder in (origin, *origin.parents) if (c := folder / BINDING_FILE).is_file()),
        None,
    )


def binding_text(project_id: uuid.UUID | str, project_name: str, server: str = MCP_RESOURCE_URL) -> str:
    """The exact version 1 JSON the server tells the agent to write (tools.md, Workspace binding)."""

    document = {
        "version": BINDING_VERSION,
        "server": server,
        "project_id": str(project_id),
        "project_name": project_name,
    }
    return json.dumps(document)


def write_binding(
    workspace: Path,
    project_id: uuid.UUID | str,
    project_name: str,
    *,
    server: str = MCP_RESOURCE_URL,
) -> Path:
    """Write or replace the whole binding file (temp file in the same folder, then `os.replace`)."""

    path = workspace / BINDING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(binding_text(project_id, project_name, server), encoding="utf-8")
        temporary.replace(path)  # os.replace: atomic on one filesystem
    finally:
        temporary.unlink(missing_ok=True)
    return path


@functools.cache
def _binding_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(BINDING_SCHEMA.read_text(encoding="utf-8")))


def validate_binding(document: object) -> dict[str, Any]:
    """`document` when it matches the contract schema; else a `QaError` naming the failed keys and rules only."""

    problems = sorted(
        {
            f"{'/'.join(str(part) for part in error.absolute_path) or 'file'}: {error.validator}"
            for error in _binding_validator().iter_errors(document)
        }
    )
    if problems:
        raise QaError("binding file invalid: " + "; ".join(problems))
    return cast("dict[str, Any]", document)


def read_binding(path: Path) -> dict[str, Any]:
    """The validated content of a binding file (`QaError` for an unreadable, non-JSON or invalid file)."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise QaError("binding file unreadable or not JSON") from None
    return validate_binding(document)


def bound_project_id(start: Path, *, server: str = MCP_RESOURCE_URL) -> uuid.UUID:
    """The `project_id` of the nearest binding file, only when its `server` is `server` (`QaError` otherwise)."""

    path = find_binding(start)
    if path is None:
        raise QaError("no .specflow/config.json from the working folder up")
    document = read_binding(path)
    if document["server"] != server:
        raise QaError("the binding file names another server")
    return uuid.UUID(document["project_id"])


# Latency runner (E14) --------------------------------------------------------------------------------------------


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (`sorted[ceil(q * n) - 1]`)."""

    if not samples:
        raise QaError("percentile of no samples")
    ordered = sorted(samples)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


@dataclass
class LatencyProgress:
    """Counts of a latency run as it goes, so a run that raises still leaves its partial numbers."""

    started: int = 0
    completed: int = 0
    errors: int = 0

    def metrics(self, prefix: str) -> dict[str, int]:
        return {
            f"{prefix}_started": self.started,
            f"{prefix}_completed": self.completed,
            f"{prefix}_errors": self.errors,
        }


@dataclass(frozen=True)
class LatencyStats:
    calls: int
    errors: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    duration_s: float
    samples: int = 0  # completed calls (errored ones included), counted from the samples themselves

    def metrics(self, prefix: str) -> dict[str, float | int]:
        rate = self.calls / self.duration_s if self.duration_s > 0 else 0.0
        return {
            f"{prefix}_calls": self.calls,
            f"{prefix}_errors": self.errors,
            f"{prefix}_p50_ms": round(self.p50_ms, 1),
            f"{prefix}_p95_ms": round(self.p95_ms, 1),
            f"{prefix}_max_ms": round(self.max_ms, 1),
            f"{prefix}_rate_per_s": round(rate, 1),
        }


_CALL_ERRORS: Final = (
    *(QaError, MCPError, httpx2.HTTPError, OSError),
    *(anyio.BrokenResourceError, anyio.ClosedResourceError, ValidationError),
)


async def _timed(
    call_once: Callable[[], Awaitable[bool]], progress: LatencyProgress | None = None
) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        ok = await call_once()
    except _CALL_ERRORS:  # any failure of the call is an error; its text is dropped
        ok = False
    if progress is not None:
        progress.completed += 1
        progress.errors += 0 if ok else 1
    return (time.perf_counter() - started) * 1000, ok


async def run_latency(
    call_once: Callable[[], Awaitable[bool]],
    *,
    rate_per_s: float = 10.0,
    duration_s: float = 60.0,
    progress: LatencyProgress | None = None,
) -> LatencyStats:
    """`rate_per_s * duration_s` calls on a fixed cadence, each started on time whatever the earlier ones are doing.

    Each call is timed around its own await (client side); `False` or an exception is an error (still sampled).
    `progress` (optional) is updated as calls start and finish, also when the run itself raises.
    """

    total = round(rate_per_s * duration_s)
    if total < 1:
        raise QaError("latency run needs at least one call")
    loop = asyncio.get_running_loop()
    started = loop.time()
    tasks: list[asyncio.Task[tuple[float, bool]]] = []
    try:
        async with asyncio.timeout(duration_s + 30):
            for index in range(total):
                await asyncio.sleep(max(started + index / rate_per_s - loop.time(), 0))
                tasks.append(asyncio.create_task(_timed(call_once, progress)))
                if progress is not None:
                    progress.started += 1
            done = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
    samples = [elapsed for elapsed, _ in done]
    errors = sum(1 for _, ok in done if not ok)
    return LatencyStats(
        total,
        errors,
        percentile(samples, 0.5),
        percentile(samples, 0.95),
        max(samples),
        loop.time() - started,
        len(samples),
    )


# Claude Code plumbing ------------------------------------------------------------------------------------------

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC8_TARGET = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_URL = re.compile(r"https?://\S+")
_AUTH_PATH = "/protocol/openid-connect/auth"
_TAIL_CHARS = 2048
_STOP_GRACE_S = 5.0
_COMMAND_TIMEOUT_S = 60.0
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
_SECRET_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer|Authorization|code=|@", re.IGNORECASE)
CLAUDE_MODEL = "sonnet"
CLAUDE_MAX_TURNS = 10
CLAUDE_MAX_BUDGET_USD = "0.50"  # per prompt: five prompts per flow stay under 3 USD even when every one spends it all
PROMPT_TIMEOUT_S = 300.0
# Built-in tools that could read other files, the environment or the network; denied on top of the allow list (a deny
# rule beats user and project settings). `Read`, `Write` and `Edit` are not here: `Write` must stay undenied or the
# `Edit` allow rule cannot cover it. `--tools` and `--strict-mcp-config` are not used: see the 07-handoff review fixes.
CLAUDE_DISALLOWED_TOOLS: Final = (
    *("Bash", "BashOutput", "KillShell", "PowerShell", "MultiEdit", "NotebookEdit"),
    *("Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent"),
)
# The only write permission: Claude Code applies Edit rules to the Write tool; scoped `Write(...)` rules do not match.
EDIT_RULE: Final = "Edit(./.specflow/**)"
WRITE_TOOLS: Final = ("Write", "Edit", "MultiEdit", "NotebookEdit")
DISCOVERY_TOOLS: Final = ("ToolSearch",)  # may appear for MCP tool discovery; reads nothing
REPORTS_DIR = Path(__file__).resolve().parents[2] / ".reports" / "mcp-qa"
CommandRunner = Callable[[Sequence[str], Path], Awaitable[int]]
_LIST_TIMEOUT_S = 120.0
# What a `claude` child needs: its sign-in (home, config dir), a terminal, locale, and the network (proxy, CA bundle).
# Never the stack's secrets (database, Keycloak, signing key, QA password): the child talks to a model.
_CHILD_ENV_WORDS: Final = (
    "PATH HOME USER LOGNAME SHELL TERM TMPDIR LANG LANGUAGE CLAUDE_CONFIG_DIR "
    "XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME XDG_RUNTIME_DIR "
    "HTTPS_PROXY HTTP_PROXY NO_PROXY ALL_PROXY https_proxy http_proxy no_proxy all_proxy "
    "SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS REQUESTS_CA_BUNDLE CURL_CA_BUNDLE"
)
_CHILD_ENV_NAMES: Final = frozenset(_CHILD_ENV_WORDS.split())
_CHILD_ENV_PREFIXES: Final = ("LC_",)


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of every spawned `claude` process: an allowlist of `environ` (default `os.environ`)."""

    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name in _CHILD_ENV_NAMES or name.startswith(_CHILD_ENV_PREFIXES)
    }


def strip_ansi(text: str) -> str:
    """The text without OSC (hyperlink) and CSI (colour, cursor) escape sequences."""

    return _CSI.sub("", _OSC.sub("", text))


def find_authorization_url(text: str, issuer: str = ISSUER) -> str | None:
    """The Keycloak authorization URL in terminal output: the OSC 8 hyperlink target, else the plain text."""

    prefix = f"{issuer}{_AUTH_PATH}"
    for target in _OSC8_TARGET.findall(text):
        if target.startswith(prefix):
            return str(target)
    plain = _OSC.sub("", text)
    found = re.search(re.escape(prefix) + r"[^\s\x1b\x07\]]+(?=[\s\x1b\x07\]])", plain)
    return found.group(0) if found else None


@dataclass(frozen=True)
class ToolEvent:
    """A tool call or result of a `claude -p --output-format stream-json` run (`name` of a result is its call's)."""

    kind: Literal["call", "result"]
    tool_use_id: str
    name: str | None
    payload: Any
    is_error: bool


def _blocks(line: str) -> list[dict[str, Any]]:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return []
    content = message.get("message", {}).get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def parse_stream_json(lines: Iterable[str]) -> list[ToolEvent]:
    """Tool calls (`assistant` `tool_use`) and results (`user` `tool_result`) in order; other lines are skipped."""

    events: list[ToolEvent] = []
    names: dict[str, str] = {}
    for line in lines:
        for block in _blocks(line):
            if block.get("type") == "tool_use":
                identifier = str(block.get("id", ""))
                names[identifier] = str(block.get("name", ""))
                events.append(ToolEvent("call", identifier, names[identifier], block.get("input"), is_error=False))
            elif block.get("type") == "tool_result":
                identifier = str(block.get("tool_use_id", ""))
                events.append(
                    ToolEvent(
                        "result", identifier, names.get(identifier), block.get("content"), bool(block.get("is_error"))
                    )
                )
    return events


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def server_name(run_id: str) -> str:
    return f"{SERVER_PREFIX}{run_id}"


def claude_add_argv(name: str, mcp_url: str = MCP_RESOURCE_URL) -> list[str]:
    return [
        *("claude", "mcp", "add", "--transport", "http", "--scope", "local"),
        *("--client-id", MCP_CLIENT_ID, "--callback-port", str(CALLBACK_PORT), name, mcp_url),
    ]


def claude_login_argv(name: str) -> list[str]:
    return ["claude", "mcp", "login", name, "--no-browser"]


def claude_logout_argv(name: str) -> list[str]:
    return ["claude", "mcp", "logout", name]


def claude_remove_argv(name: str) -> list[str]:
    return ["claude", "mcp", "remove", name, "--scope", "local"]


def claude_tool_names(name: str) -> tuple[str, str]:
    """The two MCP tools of server `name` as Claude Code names them (`mcp__<server>__<tool>`)."""

    return f"mcp__{name}__list_projects", f"mcp__{name}__list_features"


def claude_allowed_tools(name: str) -> list[str]:
    """What a flow prompt may use: our two MCP tools, `Read` and the binding-file edit rule (D7)."""

    return [*claude_tool_names(name), "Read", EDIT_RULE]


def claude_prompt_argv(
    prompt: str,
    allowed_tools: Sequence[str],
    *,
    model: str = CLAUDE_MODEL,
    max_turns: int = CLAUDE_MAX_TURNS,
    max_budget_usd: str = CLAUDE_MAX_BUDGET_USD,
) -> list[str]:
    """`claude -p` with stream-json output, a model, spend guards and no prompts; `--allowedTools` is variadic: last."""

    return [
        *("claude", "-p", prompt, "--output-format", "stream-json", "--verbose"),
        *("--model", model, "--max-turns", str(max_turns), "--max-budget-usd", max_budget_usd),
        *("--permission-mode", "dontAsk", "--no-session-persistence"),
        *("--disallowedTools", ",".join(CLAUDE_DISALLOWED_TOOLS)),
        *("--allowedTools", ",".join(allowed_tools)),
    ]


def _target(event: ToolEvent, root: Path) -> Path | None:
    """The file a Read or write call names (`file_path`, or `notebook_path`), resolved against `root`."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    raw = payload.get("file_path") or payload.get("notebook_path")
    return (root / raw).resolve() if isinstance(raw, str) and raw else None


def tool_violations(events: Iterable[ToolEvent], tools: Iterable[str], workspace: Path) -> list[str]:
    """What the run did that D7 does not allow (names only; empty: clean).

    Allowed: our MCP tools, discovery tools, `Read` of a file inside the workspace and a write tool whose target is
    `<workspace>/.specflow/config.json`. Any other call is a violation.
    """

    root, ours = workspace.resolve(), {*tools, *DISCOVERY_TOOLS}
    binding = root / BINDING_FILE  # not resolved: a symlinked binding file resolves elsewhere and fails the match
    found: list[str] = []
    for event in (e for e in events if e.kind == "call" and e.name not in ours):
        target, name = _target(event, root), str(event.name)
        if name in WRITE_TOOLS:
            if target != binding:
                found.append(f"{name} outside the binding file")
        elif name == "Read":
            if target is None or not target.is_relative_to(root):
                found.append("Read outside the workspace")
        else:
            found.append(f"{name} is not allowed")
    return found


Snapshot = dict[str, str]


def workspace_snapshot(workspace: Path) -> Snapshot:
    """Relative path to SHA-256 of every file of the workspace except `.git/`."""

    found: Snapshot = {}
    for folder, dirs, files in os.walk(workspace):
        if Path(folder) == workspace:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            path = Path(folder) / name
            found[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def stray_files(before: Snapshot, after: Snapshot) -> list[str]:
    """Files created, changed or removed between two snapshots, other than the binding file."""

    binding = BINDING_FILE.as_posix()
    return sorted(path for path in {*before, *after} if path != binding and before.get(path) != after.get(path))


def port_is_free(port: int = CALLBACK_PORT) -> bool:
    """True when nothing holds `127.0.0.1:<port>` (an earlier sign-in can keep 8765)."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _executable(argv: Sequence[str]) -> str:
    path = shutil.which(argv[0])
    if path is None:
        raise QaError(f"{argv[0]} is not on PATH")
    return path


def _signal_group(process: asyncio.subprocess.Process, sent: signal.Signals) -> None:
    """Signal the whole process group (the CLI's node children too); every child starts its own session."""

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sent)


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), _STOP_GRACE_S)
    except TimeoutError:
        _signal_group(process, signal.SIGKILL)
        await process.wait()


async def run_command(argv: Sequence[str], cwd: Path, *, timeout_s: float = _COMMAND_TIMEOUT_S) -> int:
    """Run a command without a terminal and discard its output; return the exit code.

    A command that outlives `timeout_s` is stopped (process group: terminate, then kill) and raises `QaError`.
    """

    process = await asyncio.create_subprocess_exec(
        _executable(argv),
        *argv[1:],
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            return await process.wait()
    except TimeoutError:
        raise QaError(f"{argv[0]} command timed out after {timeout_s:g}s") from None
    finally:
        await _stop(process)


@dataclass(frozen=True)
class PtyOutcome:
    """How a pseudo-terminal run ended; `tail` is the last 2 KiB of output without escapes and without URLs."""

    exit_code: int | None
    timed_out: bool
    completed: bool
    tail: str


@dataclass
class _Transcript:
    text: str = ""
    completed: bool = False


async def _pump(
    queue: asyncio.Queue[str | None],
    master: int,
    transcript: _Transcript,
    respond: Callable[[str], Awaitable[str | None]],
    done: Callable[[str], bool],
) -> None:
    answered = False
    while (chunk := await queue.get()) is not None:
        transcript.text += chunk
        if not answered and (answer := await respond(transcript.text)) is not None:
            os.write(master, f"{answer}\r".encode())
            answered = True
        if done(transcript.text):
            transcript.completed = True
            return


def _tail(text: str) -> str:
    return _URL.sub("[url]", strip_ansi(text))[-_TAIL_CHARS:]


async def run_pty(
    argv: Sequence[str],
    respond: Callable[[str], Awaitable[str | None]],
    done: Callable[[str], bool],
    *,
    timeout_s: float,
    cwd: Path,
) -> PtyOutcome:
    """Run `argv` with a pseudo-terminal as stdin, stdout and stderr, under a timeout.

    `respond(output so far)` is awaited per chunk until it returns an answer, which is written once followed by a
    carriage return; `done(output so far)` ends the run. On timeout (or when `respond` raises) the process is
    terminated, then killed. The outcome never holds a URL.
    """

    executable = _executable(argv)  # before openpty: a missing CLI leaks no descriptor
    master, slave = pty.openpty()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            cwd=cwd,
            env=child_env(),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master, 4096)
        except OSError:  # EIO: the child closed its end
            data = b""
        if not data:
            loop.remove_reader(master)
        queue.put_nowait(data.decode(errors="replace") if data else None)

    loop.add_reader(master, on_readable)
    transcript = _Transcript()
    timed_out = False
    try:
        async with asyncio.timeout(timeout_s):
            await _pump(queue, master, transcript, respond, done)
            await process.wait()
    except TimeoutError:
        timed_out = True
    finally:
        loop.remove_reader(master)
        await _stop(process)
        os.close(master)
    return PtyOutcome(process.returncode, timed_out, transcript.completed, _tail(transcript.text))


async def claude_login(name: str, key: str, password: SecretStr, *, cwd: Path, timeout_s: float = 120) -> None:
    """`claude mcp login <name> --no-browser`: read the OSC 8 URL, sign in over HTTP, paste the redirect URL back."""

    async def respond(output: str) -> str | None:
        url = find_authorization_url(output)
        return None if url is None else await complete_login(url, QA_USERS[key], password)

    def done(output: str) -> bool:
        return "authenticated with" in strip_ansi(output[-400:]).lower()

    outcome = await run_pty(claude_login_argv(name), respond, done, timeout_s=timeout_s, cwd=cwd)
    if not outcome.completed:
        reason = "timed out" if outcome.timed_out else f"exit {outcome.exit_code}"
        raise QaError(f"claude login failed: {reason}")
    log_event(logger, INFO, "mcp_qa.claude.login", user=key)


async def _attempt(runner: CommandRunner, argv: Sequence[str], cwd: Path) -> bool:
    try:
        return await runner(argv, cwd) == 0
    except (QaError, OSError):
        return False


async def _add_server(runner: CommandRunner, name: str, mcp_url: str, cwd: Path) -> None:
    if await runner(claude_add_argv(name, mcp_url), cwd) != 0:
        raise QaError("claude mcp add failed")


@asynccontextmanager
async def registered_server(
    run_id: str, *, cwd: Path, runner: CommandRunner = run_command, mcp_url: str = MCP_RESOURCE_URL
) -> AsyncIterator[str]:
    """Register `specflow-qa-<run id>` for the block; always log out and remove it afterwards.

    Both teardown steps are attempted even when one fails. A failed step raises a `QaError` listing the steps, unless
    the body already raised (its error wins).
    """

    if not port_is_free():
        raise QaError(f"port {CALLBACK_PORT} is in use")
    name = server_name(run_id)
    body_failed = False
    try:
        await _add_server(runner, name, mcp_url, cwd)
        yield name
    except BaseException:
        body_failed = True
        raise
    finally:
        steps = (("logout", claude_logout_argv(name)), ("remove", claude_remove_argv(name)))
        failed = [step for step, argv in steps if not await _attempt(runner, argv, cwd)]
        if failed and not body_failed:
            raise QaError("claude teardown failed: " + ", ".join(failed))


# Claude Code prompt runner (agent tier) --------------------------------------------------------------------------


def _json_line(line: str) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


@dataclass(frozen=True)
class PromptRun:
    """A `claude -p` run: `lines` are its stream-json lines (never logged), `events` the tool calls and results."""

    exit_code: int | None
    timed_out: bool
    lines: list[str]
    events: list[ToolEvent]
    init: dict[str, Any] | None
    result: dict[str, Any] | None
    stderr_tail: str

    @property
    def text(self) -> str:
        """Everything the run printed, for the check that a value appears nowhere in it."""

        return "\n".join(self.lines)

    @property
    def prose(self) -> str:
        return str(self.result.get("result", "")) if self.result else ""


async def capture_command(argv: Sequence[str], cwd: Path, *, timeout_s: float = _COMMAND_TIMEOUT_S) -> tuple[int, str]:
    """Run a command without a terminal; return its exit code and stdout (in memory only: never log it)."""

    process = await asyncio.create_subprocess_exec(
        _executable(argv),
        *argv[1:],
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, _ = await process.communicate()
    except TimeoutError:
        raise QaError(f"{argv[0]} command timed out after {timeout_s:g}s") from None
    finally:
        await _stop(process)
    return process.returncode or 0, stdout.decode(errors="replace")


async def run_prompt(argv: Sequence[str], cwd: Path, *, timeout_s: float = PROMPT_TIMEOUT_S) -> PromptRun:
    """Run a `claude -p` command to the end (or the timeout) and parse its stream-json output.

    Output is read in one piece: stream-json lines can exceed the 64 KiB limit of a `StreamReader` line.
    """

    process = await asyncio.create_subprocess_exec(
        _executable(argv),
        *argv[1:],
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout = stderr = b""
    timed_out = False
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        timed_out = True
    finally:
        await _stop(process)
    lines = stdout.decode(errors="replace").splitlines()
    messages = [message for line in lines if (message := _json_line(line)) is not None]
    init = next((m for m in messages if m.get("type") == "system" and m.get("subtype") == "init"), None)
    result = next((m for m in reversed(messages) if m.get("type") == "result"), None)
    return PromptRun(
        process.returncode,
        timed_out,
        lines,
        parse_stream_json(lines),
        init,
        result,
        _tail(stderr.decode(errors="replace")),
    )


def tool_json(event: ToolEvent) -> Any:
    """The JSON a tool result carries (a string, or a list of text blocks); `None` when it is not JSON."""

    payload = event.payload
    if isinstance(payload, list):
        payload = "".join(str(b.get("text", "")) for b in payload if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(payload, str):
        return payload if isinstance(payload, dict) else None
    try:
        return json.loads(payload)
    except ValueError:
        return None


async def claude_signed_in(cwd: Path) -> bool:
    """`claude auth status`: exit 0 and `loggedIn` true (nothing else is read)."""

    try:
        code, out = await capture_command(["claude", "auth", "status"], cwd)
    except (QaError, OSError):
        return False
    status = _json_line(out)
    return code == 0 and status is not None and status.get("loggedIn") is True


async def claude_lists(prefix: str, cwd: Path) -> bool:
    """True when `claude mcp list` (run in `cwd`) has a line that starts with a server name beginning `prefix`."""

    code, out = await capture_command(["claude", "mcp", "list"], cwd, timeout_s=_LIST_TIMEOUT_S)
    if code != 0:
        raise QaError(f"claude mcp list failed: exit {code}")
    return re.search(rf"^\s*{re.escape(prefix)}", out, re.MULTILINE) is not None


@dataclass
class TeardownVerdict:
    """Whether the `claude mcp list` check after a signed-in run found no `specflow-qa-` server (None: not reached)."""

    clean: bool | None = None

    @property
    def label(self) -> str:
        return "not reached" if self.clean is None else ("yes" if self.clean else "no")


@asynccontextmanager
async def signed_in_claude(
    key: str,
    password: SecretStr,
    *,
    cwd: Path,
    run_id: str,
    runner: CommandRunner = run_command,
    verdict: TeardownVerdict | None = None,
) -> AsyncIterator[str]:
    """Register `specflow-qa-<run id>`, sign in as `key`, yield the name; afterwards verify nothing is left behind.

    Logout and removal always run (`registered_server`), and so does the `claude mcp list` check, also when the body
    or the login failed. `verdict` (optional) receives its result (an unreadable listing is not clean). A leftover
    `specflow-qa-` server raises, unless an earlier error is already propagating (that error wins).
    """

    failed = False
    try:
        async with registered_server(run_id, cwd=cwd, runner=runner) as name:
            await claude_login(name, key, password, cwd=cwd)
            yield name
    except BaseException:
        failed = True
        raise
    finally:
        try:
            clean = not await claude_lists(SERVER_PREFIX, cwd)
        except (QaError, OSError):
            clean = False
        if verdict is not None:
            verdict.clean = clean
        if not clean and not failed:
            raise QaError("claude teardown left a specflow-qa- server")


# Run reports ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    outcome: Literal["passed", "failed", "skipped"]
    duration_ms: float
    note: str = ""


@dataclass(frozen=True)
class RunReport:
    run_id: str
    kind: str
    results: list[ScenarioResult]
    metrics: dict[str, float | int | str] = field(default_factory=dict)


def write_report(report: RunReport, directory: Path = REPORTS_DIR) -> Path:
    """Write `<run id>.json` (scenario, outcome, duration, metrics); refuse a token, header, code or email shape."""

    if not _RUN_ID.fullmatch(report.run_id):
        raise QaError("report refused: unsafe run id")
    text = json.dumps(asdict(report), indent=2)
    if _SECRET_SHAPE.search(text):
        raise QaError("report refused: a value looks like a token, header, code or email")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.run_id}.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


_URL_SHAPE = re.compile(r"https?://\S+")
_EMAIL_SHAPE = re.compile(r"\S+@\S+")
_JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_HEADER_WORDS = re.compile(r"Bearer|Authorization|code=", re.IGNORECASE)


def report_note(text: str, limit: int = 300) -> str:
    """Model prose or an error text made safe for a report: no URL, email, token or header, one line, truncated."""

    cleaned = _URL_SHAPE.sub("[url]", strip_ansi(text))
    cleaned = _EMAIL_SHAPE.sub("[email]", cleaned)
    cleaned = _JWT_SHAPE.sub("[token]", cleaned)
    cleaned = _HEADER_WORDS.sub("", cleaned).replace("@", "")
    return " ".join(cleaned.split())[:limit]


async def run_scenario(report: RunReport, scenario: str, work: Awaitable[None]) -> None:
    """Await `work`, record its outcome and duration in `report` (the error type only on failure), re-raise errors."""

    started = time.perf_counter()
    try:
        await work
    except BaseException as error:
        elapsed = (time.perf_counter() - started) * 1000
        report.results.append(ScenarioResult(scenario, "failed", round(elapsed, 1), type(error).__name__))
        raise
    report.results.append(ScenarioResult(scenario, "passed", round((time.perf_counter() - started) * 1000, 1)))


# Report saving and the E14 numbers -------------------------------------------------------------------------------


def save_report(report: RunReport, *, raise_on_refusal: bool) -> None:
    """Write the report in its own step: a refused report is logged and never replaces an error already in flight."""

    try:
        path = write_report(report)
    except QaError:
        log_event(logger, INFO, "mcp_qa.report_refused", run_id=report.run_id)
        if raise_on_refusal:
            raise
    else:
        log_event(logger, INFO, "mcp_qa.report", report=path.name, results=len(report.results))


async def run_latency_into(
    metrics: dict[str, float | int | str],
    call_once: Callable[[], Awaitable[bool]],
    *,
    prefix: str = "e14",
    **pacing: Any,
) -> LatencyStats:
    """`run_latency` that leaves its numbers in `metrics`: the partial counts if the run dies, else the stats."""

    progress = LatencyProgress()
    try:
        stats = await run_latency(call_once, progress=progress, **pacing)
    except BaseException:
        metrics.update(progress.metrics(prefix))
        raise
    metrics.update(stats.metrics(prefix))
    return stats


# Workspace flow of the agent tier (testing.md, Agent QA with Claude Code) ----------------------------------------

ALPHA_FEATURES: Final = 7
BETA_FEATURES: Final = 3
FLOW_STEPS: Final = 5  # prompts of one flow: bind, reuse, foreign, switch, reuse
SELECT_PROJECT: Final = "select_project"
REUSE_PROMPT: Final = "List every feature of this repository's SpecFlow project."
PLAIN_BIND_PROMPT: Final = "Bind this folder to the SpecFlow project {project}"
PromptRunner = Callable[[Sequence[str], Path], Awaitable[PromptRun]]


class RunPreconditionError(QaError):
    """The run cannot answer the question (no connection, no model access, timed out): not a verdict on the server."""


@dataclass(frozen=True)
class RunTotals:
    cost_usd: float
    turns: int


@dataclass(frozen=True)
class FlowContext:
    """What a step check needs: the registered server, the workspace and the seeded ids."""

    name: str
    workspace: Path
    ids: FixtureIds

    @property
    def tools(self) -> tuple[str, str]:
        return claude_tool_names(self.name)

    @property
    def binding(self) -> Path:
        return self.workspace / BINDING_FILE


def _require(message: str, *, ok: bool) -> None:
    if not ok:
        raise QaError(message)


def _ours(run: PromptRun, tools: tuple[str, str]) -> list[ToolEvent]:
    """The events of our two tools; other calls (for example a tool search) only count in the note."""

    return [event for event in run.events if event.name in tools]


def _of_tool(events: Iterable[ToolEvent], tool: str, kind: str) -> list[ToolEvent]:
    return [event for event in events if event.kind == kind and str(event.name).endswith(f"__{tool}")]


def _payload(event: ToolEvent) -> dict[str, Any]:
    data = tool_json(event)
    return data if isinstance(data, dict) else {}


def _project_arg(event: ToolEvent) -> str:
    return str(event.payload.get("project_id", "")).lower() if isinstance(event.payload, dict) else ""


def _feature_results(events: list[ToolEvent], project_id: str) -> list[ToolEvent]:
    """Results of the `list_features` calls made for `project_id`, matched by `tool_use_id`."""

    calls = {call.tool_use_id for call in _of_tool(events, "list_features", "call") if _project_arg(call) == project_id}
    _require(f"list_features was never called for {project_id}", ok=bool(calls))
    return [event for event in _of_tool(events, "list_features", "result") if event.tool_use_id in calls]


def _only_project(events: list[ToolEvent], project_id: str) -> None:
    """Every `list_features` call used this id and none used another."""

    used = {_project_arg(call) for call in _of_tool(events, "list_features", "call")}
    _require(f"list_features used {len(used)} distinct ids, expected only the bound one", ok=used == {project_id})


def _write_targets(run: PromptRun, ctx: FlowContext) -> set[Path]:
    root = ctx.workspace.resolve()
    calls = [event for event in run.events if event.kind == "call" and event.name in WRITE_TOOLS]
    return {target for event in calls if (target := _target(event, root)) is not None}


def check_run(run: PromptRun, ctx: FlowContext) -> RunTotals:
    """What every prompt needs before its assertions mean anything; a precondition failure names the reason."""

    if run.timed_out:
        raise RunPreconditionError("the prompt timed out")
    init = run.init or {}
    servers = {str(item.get("name")): item.get("status") for item in init.get("mcp_servers", [])}
    if servers.get(ctx.name) != "connected":
        raise RunPreconditionError(f"the MCP server is not connected: {servers.get(ctx.name)}")
    seen = [str(tool) for tool in init.get("tools", []) if str(tool).startswith("mcp__")]
    if not set(ctx.tools) <= set(seen):
        raise RunPreconditionError(f"tool names: {seen}")
    result = run.result
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        raise RunPreconditionError(f"no model access or budget: {(result or {}).get('subtype')}")
    _no_violations(run, ctx)
    return RunTotals(float(result.get("total_cost_usd") or 0), int(result.get("num_turns") or 0))


def _no_violations(run: PromptRun, ctx: FlowContext) -> None:
    violations = tool_violations(run.events, ctx.tools, ctx.workspace)
    _require(f"tool calls D7 does not allow: {sorted(set(violations))}", ok=not violations)


def slash_expanded(run: PromptRun, name: str) -> bool:
    """True when the run's init lists `mcp__<name>__select_project` as a slash command.

    The stream never echoes the expanded prompt text. The init `slash_commands` is the list Claude Code expands from;
    a command not in it (a mistyped or unregistered prompt) reaches the model as plain text.
    """

    commands = {str(command).lstrip("/") for command in (run.init or {}).get("slash_commands", [])}
    return f"mcp__{name}__{SELECT_PROJECT}" in commands


def _bound_to(ctx: FlowContext, project: str) -> None:
    """The workspace file validates, names this server and the seeded project (a missing file fails)."""

    _require("the binding file does not exist", ok=ctx.binding.is_file())
    document = read_binding(ctx.binding)
    _require("the binding file names another server", ok=document["server"] == MCP_RESOURCE_URL)
    _require(
        f"the binding file does not name {project}", ok=uuid.UUID(document["project_id"]) == ctx.ids.projects[project]
    )


def _sole_write(run: PromptRun, ctx: FlowContext) -> None:
    targets = _write_targets(run, ctx)
    _require(
        f"write targets: {len(targets)}, expected exactly the binding file",
        ok=targets == {ctx.workspace.resolve() / BINDING_FILE},
    )


def _no_writes(run: PromptRun, ctx: FlowContext) -> None:
    _require("the run wrote a file", ok=not _write_targets(run, ctx))


def _binding_digest(snapshot: Snapshot) -> str | None:
    return snapshot.get(BINDING_FILE.as_posix())


def _reuse_results(run: PromptRun, ctx: FlowContext, project: str) -> list[ToolEvent]:
    """Common to steps 2 and 4b: `list_features` only with this project's id, `list_projects` never, no write."""

    project_id = str(ctx.ids.projects[project]).lower()
    _require("list_projects was called in a bound folder", ok=not _of_tool(run.events, "list_projects", "call"))
    _no_writes(run, ctx)
    events = _ours(run, ctx.tools)
    results = _feature_results(events, project_id)
    _only_project(events, project_id)
    return results


def bound_to_alpha(run: PromptRun, ctx: FlowContext, _before: Snapshot) -> None:
    """Step 1: `list_projects` called; the file is the only write target; it validates and names P-Alpha."""

    _require("list_projects was never called", ok=bool(_of_tool(run.events, "list_projects", "call")))
    _sole_write(run, ctx)
    _bound_to(ctx, "P-Alpha")


def alpha_reused(run: PromptRun, ctx: FlowContext, _before: Snapshot) -> None:
    """Step 2: `list_features` for P-Alpha only; the union of the results holds its 7 feature ids."""

    alpha = str(ctx.ids.projects["P-Alpha"])
    results = _reuse_results(run, ctx, "P-Alpha")
    found = {str(item["spec_id"]) for event in results for item in _payload(event).get("items", [])}
    expected = {node.spec_id for node in fixture_nodes("P-Alpha", alpha, load_ontology()) if node.label == "Feature"}
    _require("the fixture does not hold 7 P-Alpha features", ok=len(expected) == ALPHA_FEATURES)
    _require(f"P-Alpha features: {len(found)} found, {len(expected)} expected", ok=found == expected)
    leaked = any("REQ::" in str(event.payload) for event in run.events if event.kind == "result")
    _require("a REQ:: id was returned", ok=not leaked)


def beta_refused(run: PromptRun, ctx: FlowContext, before: Snapshot) -> None:
    """Step 3: only the bound P-Beta id is used and answers `project_not_found`; the file is untouched."""

    beta = str(ctx.ids.projects["P-Beta"]).lower()
    events = _ours(run, ctx.tools)
    refused = _feature_results(events, beta)
    _only_project(events, beta)
    codes = [_payload(event).get("error", {}).get("code") for event in refused]
    _require(f"P-Beta answers: {codes}", ok=all(code == "project_not_found" for code in codes))
    names = [node.properties["name"] for node in fixture_nodes("P-Beta", beta, load_ontology())]
    _require("the fixture does not hold 3 P-Beta features", ok=len(names) == BETA_FEATURES)
    _require("a P-Beta feature name appears in the run", ok=not [name for name in names if name in run.text])
    _no_writes(run, ctx)
    kept = _binding_digest(workspace_snapshot(ctx.workspace)) == _binding_digest(before)
    _require("the agent changed the binding file", ok=kept)


def switched_to_empty(run: PromptRun, ctx: FlowContext, before: Snapshot) -> None:
    """Step 4: the file is the only write target, was replaced, validates and names P-Empty."""

    _sole_write(run, ctx)
    _bound_to(ctx, "P-Empty")
    replaced = _binding_digest(workspace_snapshot(ctx.workspace)) != _binding_digest(before)
    _require("the binding file was not replaced", ok=replaced)


def empty_reused(run: PromptRun, ctx: FlowContext, _before: Snapshot) -> None:
    """Step 4, second session: `list_features` for P-Empty only, `total_count` 0."""

    results = _reuse_results(run, ctx, "P-Empty")
    counts = [(_payload(event).get("total_count"), _payload(event).get("items")) for event in results]
    _require(f"P-Empty answers: {counts}", ok=all(count == (0, []) for count in counts))


Check = Callable[[PromptRun, FlowContext, Snapshot], None]


@dataclass(frozen=True)
class StepRecord:
    """One step of a flow for the report: outcome, tool calls, duration, cost, `slash`, `plain` or `-`."""

    step: str
    outcome: Literal["passed", "failed"]
    tool_calls: tuple[str, ...]
    duration_s: float
    cost_usd: float
    mode: str
    note: str = ""
    prose: str = ""


def step_result(record: StepRecord, run_number: int) -> ScenarioResult:
    """The report row of a step; the model's prose is recorded in the note, never asserted."""

    detail = f"{record.mode} cost_usd={record.cost_usd:.4f} tools={','.join(record.tool_calls) or '-'} {record.note}"
    note = f"{detail} {report_note(record.prose)}".strip()
    return ScenarioResult(f"run{run_number}-{record.step}", record.outcome, round(record.duration_s * 1000, 1), note)


def flow_failure(records: Sequence[StepRecord]) -> str | None:
    """The first failed step with its note, a short flow, or None when all `FLOW_STEPS` steps passed."""

    failed = next((record for record in records if record.outcome != "passed"), None)
    if failed is not None:
        return f"{failed.step}: {failed.note}"
    return None if len(records) == FLOW_STEPS else f"the flow stopped after {len(records)} of {FLOW_STEPS} steps"


class FlowStep(NamedTuple):
    key: str
    act: Callable[[], Awaitable[PromptRun]]
    check: Check
    prepare: Callable[[], object] | None = None  # the harness's part, before the snapshot


class WorkspaceFlow:
    """The four steps (five prompts) of the agent tier in one workspace, with the harness playing the user's part."""

    def __init__(
        self,
        ctx: FlowContext,
        runner: PromptRunner = run_prompt,
        *,
        model: str = CLAUDE_MODEL,
        max_turns: int = CLAUDE_MAX_TURNS,
        max_budget_usd: str = CLAUDE_MAX_BUDGET_USD,
    ) -> None:
        self.ctx, self.runner = ctx, runner
        self.model, self.max_turns, self.max_budget_usd = model, max_turns, max_budget_usd
        self.records: list[StepRecord] = []  # fills as steps finish, so a step that raises keeps the earlier ones
        self._cost, self._mode = 0.0, "-"
        self._calls: list[str] = []
        self._runs: list[PromptRun] = []
        self._prose: list[str] = []

    async def _run(self, prompt: str) -> PromptRun:
        tools = claude_allowed_tools(self.ctx.name)
        argv = claude_prompt_argv(
            prompt, tools, model=self.model, max_turns=self.max_turns, max_budget_usd=self.max_budget_usd
        )
        run = await self.runner(argv, self.ctx.workspace)
        self._cost += float((run.result or {}).get("total_cost_usd") or 0)
        self._calls += [str(e.name).rsplit("__", maxsplit=1)[-1] for e in run.events if e.kind == "call"]
        self._prose.append(run.prose)
        self._runs.append(run)
        return run

    async def _select(self, project: str) -> PromptRun:
        """The prompt as a slash command (D8); the plain request only when the stream shows it was not expanded."""

        self._mode = "slash"
        run = await self._run(f"/mcp__{self.ctx.name}__{SELECT_PROJECT} {project}")
        if run.timed_out or slash_expanded(run, self.ctx.name):
            return run
        self._mode = "plain"
        return await self._run(PLAIN_BIND_PROMPT.format(project=project))

    async def _ask(self) -> PromptRun:
        return await self._run(REUSE_PROMPT)

    def _judge(self, run: PromptRun, step: FlowStep, before: Snapshot) -> str:
        """The note of a failed check ("" when the step passed): D7 on every run of the step, then the step's own."""

        try:
            for earlier in self._runs[:-1]:  # the unexpanded slash run of a fallback
                _no_violations(earlier, self.ctx)
            check_run(run, self.ctx)
            strays = stray_files(before, workspace_snapshot(self.ctx.workspace))
            _require(f"files created or changed outside the binding file: {len(strays)}", ok=not strays)
            step.check(run, self.ctx, before)
        except RunPreconditionError as reason:
            return f"precondition: {reason}"
        except QaError as failed:
            return f"assertion: {failed}"
        return ""

    async def _step(self, step: FlowStep) -> StepRecord:
        if step.prepare is not None:
            step.prepare()
        before = workspace_snapshot(self.ctx.workspace)
        self._cost, self._calls, self._prose, self._runs, self._mode = 0.0, [], [], [], "-"
        started = time.perf_counter()
        run = await step.act()  # a runner that raises (no `claude`, no process) is not a verdict: it propagates
        note = self._judge(run, step, before)
        outcome: Literal["passed", "failed"] = "failed" if note else "passed"
        elapsed, calls, prose = time.perf_counter() - started, tuple(self._calls), " | ".join(self._prose)
        return StepRecord(step.key, outcome, calls, elapsed, self._cost, self._mode, note, prose)

    def _bind_beta(self) -> None:
        write_binding(self.ctx.workspace, self.ctx.ids.projects["P-Beta"], "P-Beta")

    async def run(self) -> list[StepRecord]:
        """Run the steps in order and stop after the first that fails."""

        steps = [
            FlowStep("1-bind-alpha", lambda: self._select("P-Alpha"), bound_to_alpha),
            FlowStep("2-reuse-alpha", self._ask, alpha_reused),
            FlowStep("3-foreign-beta", self._ask, beta_refused, self._bind_beta),
            FlowStep("4a-switch-empty", lambda: self._select("P-Empty"), switched_to_empty),
            FlowStep("4b-reuse-empty", self._ask, empty_reused),
        ]
        for step in steps:
            self.records.append(await self._step(step))
            if self.records[-1].outcome != "passed":
                break
        return self.records


async def make_git_workspace(runner: CommandRunner = run_command) -> Path:
    """A fresh `git init` folder under the system temp directory; removed again when `git init` fails."""

    workspace = make_workspace()
    try:
        code = await runner(["git", "init", "-q"], workspace)
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    if code != 0:
        shutil.rmtree(workspace, ignore_errors=True)
        raise QaError("git init failed")
    return workspace


@asynccontextmanager
async def flow_workspace(verdict: TeardownVerdict, runner: CommandRunner = run_command) -> AsyncIterator[Path]:
    """A git workspace for one run; deleted afterwards unless the teardown left a server (recovery runs from it)."""

    workspace = await make_git_workspace(runner)
    try:
        yield workspace
    finally:
        if verdict.clean is not False:
            shutil.rmtree(workspace, ignore_errors=True)


def parse_runs(text: str) -> int:
    """The number of flows to run (`SPECFLOW_QA_RUNS`, `--runs`): a positive integer."""

    try:
        runs = int(text)
    except ValueError:
        runs = 0
    if runs < 1:
        raise QaError("the number of runs must be a positive integer")
    return runs
