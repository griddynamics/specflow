"""QA library of the MCP server (docs/mcp-server/testing.md): Keycloak admin, login and token helpers, dev tokens.

Used by `examples/mcp_qa.py` (commands) and, later, by the end-to-end and agent QA tiers. Everything here talks to
the local Keycloak at `http://localhost:8180` (`make up WITH=keycloak`) and never prints or logs a token, password,
cookie, email, authorization code or redirect URL: errors name the step and the HTTP status only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import json
import os
import pty
import re
import secrets
import shutil
import signal
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import jwt
from neo4j import AsyncDriver
from pydantic import SecretStr
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

Client = Literal["mcp", "web"]
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
REPORTS_DIR = Path(__file__).resolve().parents[2] / ".reports" / "mcp-qa"
CommandRunner = Callable[[Sequence[str], Path], Awaitable[int]]


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
    return f"specflow-qa-{run_id}"


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


def claude_prompt_argv(prompt: str, allowed_tools: Sequence[str]) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        ",".join(allowed_tools),
    ]


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
            executable, *argv[1:], cwd=cwd, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
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
