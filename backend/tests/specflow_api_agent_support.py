"""Shared fixtures of the API agent route tests (no tests in here): the REST app with the agent routes on, over fakes.

Boundaries faked: Postgres (``FakeRepo``), the graph (a scripted executor), the plan (``FakePlanService``: the real
plan library over an in-memory plan), Temporal and storage (the REST harness's fakes). Tokens are real RS256 JWTs of the
run's loopback IdP, checked by the real verifiers. Nothing here opens a socket beyond that stub.

The data is the QA-seed shape of the MCP contract gate (alice owns three projects, bob one), re-declared here so
these tests do not depend on the gate's seam module, which later sessions edit.
"""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES, USER_ID_HEADER
from api.agent_reads import AgentReads
from api.app import create_app
from api.runtime import Runtime
from graph.core import (
    ConfigurationError,
    Executor,
    GraphReader,
    InfrastructureUnavailableError,
)
from graph_support import shipped_ontology
from health import Readiness
from plan_support import FakePlanService
from settings import RoleSettings, load_settings
from specflow_api_support import FakeGraphStore, FakeTemporal
from specflow_api_support import token as web_token
from specflow_auth_support import session_idp
from storage.memory import InMemoryObjectStore
from test_specflow_api_fakes import FakeRepo

ALPHA, BETA, GAMMA, FOREIGN = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3, 4))
UNKNOWN_PROJECT = "00000000-0000-4000-8000-0000000000ff"
FEATURE_IDS = ["FEAT::F1", "FEAT::F2", "FEAT::F3"]
SEEDED = (
    (ALPHA, "Alpha", "alice", 3),
    (BETA, "Beta", "alice", 2),
    (GAMMA, "Gamma", "alice", 1),
    (FOREIGN, "Foreign", "bob", 4),
)
ALICE_EMAIL = "alice@example.com"
ALICE_SUBJECT = f"user:{ALICE_EMAIL}"
MCP_URL = "http://localhost:8002/mcp"  # the MCP resource URL (and so the audience) of the tests' environment
MCP_CLIENT = "specflow-mcp"
EXTREME_PROJECT_CURSOR = (  # a cursor whose instant has no UTC value: ``invalid_cursor``, not a database error
    base64.urlsafe_b64encode(b"0001-01-01T00:00:00+14:00|22222222-2222-4222-8222-222222222222").decode().rstrip("=")
)
SMOKE_CLIENT = "specflow-smoke"
USER_HEADER = USER_ID_HEADER

# One call per route (page one of the gate's cases), keyed by tool name.
CALLS: Mapping[str, tuple[str, dict[str, Any]]] = {
    "list_projects": (AGENT_ROUTES["list_projects"], {"limit": 2}),
    "list_features": (AGENT_ROUTES["list_features"], {"project_id": ALPHA, "limit": 2}),
    "list_tasks": (AGENT_ROUTES["list_tasks"], {"project_id": ALPHA, "limit": 2}),
    "list_feature_tasks": (
        AGENT_ROUTES["list_feature_tasks"],
        {"project_id": ALPHA, "feature_id": "FEAT::PLACE", "limit": 2},
    ),
    "get_task_path": (
        AGENT_ROUTES["get_task_path"],
        {"project_id": ALPHA, "task_id": "TASK::ORDERS::C", "limit": 2},
    ),
    "get_task_brief": (
        AGENT_ROUTES["get_task_brief"],
        {"project_id": ALPHA, "task_id": "TASK::ORDERS::B"},
    ),
}
PROJECT_ROUTES = tuple(name for name in CALLS if name != "list_projects")
# The people tools' routes (session 08), kept apart from ``CALLS``, which the tests of the split's routes compare with
# their goldens: (method, path, query, JSON body of a write). Story ids: ``plan_claims_support.mini_plan``.
PEOPLE_CALLS: Mapping[str, tuple[str, str, dict[str, Any], dict[str, Any] | None]] = {
    "list_areas": ("GET", AGENT_ROUTES["list_areas"], {"project_id": ALPHA}, None),
    "list_stories": ("GET", AGENT_ROUTES["list_stories"], {"project_id": ALPHA, "limit": 2}, None),
    "preview_story": (
        "GET",
        AGENT_ROUTES["preview_story"],
        {"project_id": ALPHA, "story_id": "STORY::A::API"},
        None,
    ),
    "claim_story": (
        "POST",
        AGENT_ROUTES["claim_story"],
        {"project_id": ALPHA},
        {"story_id": "STORY::A::API", "agent_label": "Claude Code", "branch": "fix/x"},
    ),
    "release_story": (
        "POST",
        AGENT_ROUTES["release_story"],
        {"project_id": ALPHA},
        {"story_id": "STORY::A::API", "claim_token": "t"},
    ),
    "add_finding": (
        "POST",
        AGENT_ROUTES["add_finding"],
        {"project_id": ALPHA},
        {"kind": "gap", "text": "x", "about": ["FEAT::API"]},
    ),
    "report_task": (
        "POST",
        AGENT_ROUTES["report_task"],
        {"project_id": ALPHA},
        {"task_id": "TASK::A::API_LOGIC", "claim_token": "t", "progress": "done"},
    ),
    "complete_story": (
        "POST",
        AGENT_ROUTES["complete_story"],
        {"project_id": ALPHA},
        {"story_id": "STORY::A::API", "claim_token": "t", "merge_commit": "a" * 40},
    ),
}


def mcp_token(email: str = ALICE_EMAIL, **over: Any) -> str:
    """A token of the run's loopback IdP for the agent surface: the API token with the MCP audience and client.
    Minted on each call (expiry five minutes ahead), never at import, so a long run cannot outlive it."""

    return web_token(email, **{"aud": MCP_URL, "azp": MCP_CLIENT, **over})


ALICE_NOW = "<alice's mcp token, minted when the request is made>"  # default of ``AgentRig.get``


def alice() -> str:
    return mcp_token(ALICE_EMAIL)


def web_alice() -> str:
    return web_token(ALICE_EMAIL)


def bearer_header(raw: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"} if raw else {}


def agent_env(valid_env: dict[str, str], **over: str) -> dict[str, str]:
    """A valid local ``api`` environment with ``MCP_RESOURCE_URL`` set (the agent routes on)."""

    return {**valid_env, "MCP_RESOURCE_URL": MCP_URL, **over}


def claim_mode_env(valid_env: dict[str, str]) -> dict[str, str]:
    """Client-audience access tokens (``AUTH_TOKEN_TYPE=claim``) on both surfaces, each with its own client."""

    return {
        **agent_env(valid_env),
        "AUTH_API_AUDIENCE": "specflow-web",
        "AUTH_API_CLIENT_IDS": f"specflow-web,{SMOKE_CLIENT}",
        "MCP_AUDIENCE": MCP_CLIENT,
        "MCP_CLIENT_IDS": MCP_CLIENT,
        "AUTH_TOKEN_TYPE": "claim",
        "AUTH_TOKEN_TYPE_CLAIM": "x_token_kind",
        "AUTH_TOKEN_TYPE_VALUE": "access",
        "AUTH_EMAIL_CLAIM": "sub",
        "AUTH_EMAIL_VERIFIED_CLAIM": "",
    }


def minted(**over: Any) -> str:
    """A token of the run's IdP built from scratch; ``None`` removes a claim (the helpers here and
    in ``specflow_api_support`` always set ``email``)."""

    idp = session_idp()
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, Any] = {"iss": idp.issuer, "iat": now, "exp": now + 300, **over}
    return idp.sign({key: value for key, value in payload.items() if value is not None})


def access_token(*, audience: str, client: str, kind: str | None = "access", **over: Any) -> str:
    """The provider's access-token shape of claim mode: no ``email``, ``typ`` or ``name``; the email is the ``sub``."""

    return minted(aud=audience, azp=client, sub=ALICE_EMAIL, x_token_kind=kind, **over)


class ScriptedExecutor(Executor):
    """An ``Executor`` answering each read by query name with ``answers[name](params)``, recording every call; an
    unscripted query fails the test."""

    def __init__(self, answers: Mapping[str, Callable[[Mapping[str, Any]], list[dict[str, Any]]]]) -> None:
        self.answers = {"read_owner_values": lambda _params: [], **answers}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.project_ids: list[str] = []

    async def read(self, query: Any, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        self.project_ids.append(project_id)
        self.calls.append((query.name, params))
        if query.name not in self.answers:
            raise AssertionError(f"unscripted query {query.name}")
        return self.answers[query.name](params)


def paged_executor(spec_ids: list[str], label: str) -> ScriptedExecutor:
    """Answers ``read_nodes_page`` like the database (ids after ``after``, sorted, the first ``limit``) and
    ``count_nodes`` with the number of ids."""

    def page_rows(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        after = params["after"]
        ordered = sorted(i for i in spec_ids if after is None or i > after)
        return [
            {
                "labels": ["SpecNode", label],
                "props": {"spec_id": i, "uuid": f"u-{i}", "name": i},
                "provenance": [],
            }
            for i in ordered[: params["limit"]]
        ]

    return ScriptedExecutor(
        {
            "read_nodes_page": page_rows,
            "count_nodes": lambda _params: [{"total": len(spec_ids)}],
        }
    )


async def owned_project(repo: FakeRepo, email: str, name: str) -> SimpleNamespace:
    """A project ``email`` owns (member row included); the user is provisioned on the way, as the routes do."""

    user = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{email}", email=email, name=None))
    return await repo.create_project(None, user.id, name)


def graph_executor(scenario: str) -> ScriptedExecutor:
    if scenario == "graph_down":
        return failing_executor(InfrastructureUnavailableError("neo4j_unreachable"))
    if scenario == "bug":
        return failing_executor(ConfigurationError("unknown_label", label="X"))
    return paged_executor(FEATURE_IDS, "Feature")


def failing_executor(error: Exception) -> ScriptedExecutor:
    def raise_it(_: Any) -> list[dict[str, Any]]:
        raise error

    return ScriptedExecutor({"count_nodes": raise_it})


async def seed_projects(repo: FakeRepo) -> dict[str, Any]:
    users: dict[str, Any] = {}
    for project_id, name, owner, day in SEEDED:
        if owner not in users:
            principal = SimpleNamespace(
                subject=f"user:{owner}@example.com",
                email=f"{owner}@example.com",
                name=None,
            )
            users[owner] = await repo.upsert_user(None, principal)
        user = users[owner]
        project = SimpleNamespace(
            id=uuid.UUID(project_id),
            name=name,
            created_by=user.id,
            created_at=datetime(2026, 3, day, 9, 30, tzinfo=UTC),
        )
        repo.projects[project.id] = project
        repo.members.add((project.id, user.id))
        repo.roles[(project.id, user.id)] = "owner"
    return users


async def seed_many_projects(repo: FakeRepo, user: Any, count: int) -> None:
    """``count`` more projects ``user`` owns (the oldest of all), for the paging bounds."""

    for number in range(count):
        project = SimpleNamespace(
            id=uuid.uuid4(),
            name=f"P{number}",
            created_by=user.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        repo.projects[project.id] = project
        repo.members.add((project.id, user.id))
        repo.roles[(project.id, user.id)] = "owner"


@dataclass
class AgentRig:
    """What a test gets: the client of the running API and the fakes behind it."""

    client: httpx.AsyncClient
    app: Any
    repo: Any  # the FakeRepo; None when a real session factory is used
    runtime: Runtime
    executor: ScriptedExecutor
    plan: FakePlanService
    users: dict[str, Any]

    @property
    def alice_id(self) -> str:
        return str(self.users["alice"].id)

    async def alice_user_id_header(self) -> str:
        """The header of an answer to alice's MCP token (works with the real database too)."""

        response = await self.get(AGENT_ROUTES["list_projects"])
        return response.headers[USER_HEADER]

    async def get(self, path: str, raw: str | None = ALICE_NOW, **params: Any) -> httpx.Response:
        """``GET path`` as ``raw`` (a fresh MCP token of alice by default; ``None`` sends no token)."""

        raw = alice() if raw == ALICE_NOW else raw
        return await self.client.get(path, params=params, headers=bearer_header(raw))

    async def post(
        self, path: str, body: Mapping[str, Any], raw: str | None = ALICE_NOW, **params: Any
    ) -> httpx.Response:
        """``POST path`` with a JSON ``body`` as ``raw`` (a fresh MCP token of alice by default)."""

        raw = alice() if raw == ALICE_NOW else raw
        return await self.client.post(path, params=params, json=body, headers=bearer_header(raw))

    async def call(self, name: str, raw: str | None = ALICE_NOW, **over: Any) -> httpx.Response:
        """The route of tool ``name`` with the gate's page-one arguments, ``over`` replacing or adding some."""

        path, params = CALLS[name]
        return await self.get(path, raw, **{**params, **over})


@asynccontextmanager
async def serve_agent(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    scenario: str = "ok",
    has_plan: bool = True,
    lazy: bool = False,
    session_factory: Any = None,
    reader: GraphReader | None = None,
    ontology: Any = None,
) -> AsyncIterator[AgentRig]:
    """The API app (real lifespan, real verifiers) over fakes; ``lazy`` leaves ``runtime.agent_reads`` unset so the
    first agent read loads the ontology and the plan rules as in production. The live tiers pass the real
    ``session_factory`` (no ``FakeRepo``: ``rig.repo`` is ``None``) and/or a real graph ``reader`` for ``ontology``."""

    repo: Any = None
    if session_factory is None:
        repo = FakeRepo()
        repo.install(monkeypatch)
    users = await seed_projects(repo) if repo is not None else {}
    settings: RoleSettings = load_settings("api", env)
    executor, plan = graph_executor(scenario), FakePlanService(has_plan=has_plan)
    ontology = ontology or shipped_ontology()

    async def ok() -> None:
        return None

    async def factory(role: RoleSettings) -> Runtime:
        return Runtime(
            settings=role,
            session_factory=session_factory or repo,
            store=InMemoryObjectStore(app_env="local"),
            temporal=FakeTemporal(),
            graph_store=executor if lazy else FakeGraphStore(),  # a lazy load builds its reader on the store
            readiness=Readiness(dict.fromkeys(("database", "temporal", "storage", "graph"), ok), ttl=0),
        )

    app = create_app(settings, runtime_factory=factory)
    async with app.router.lifespan_context(app):
        runtime: Runtime = app.state.runtime
        if not lazy:
            runtime.agent_reads = AgentReads(
                ontology=ontology,
                reader=reader or GraphReader(executor, ontology),
                plan=plan,
            )
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield AgentRig(client, app, repo, runtime, executor, plan, users)


def refuse_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres cannot be reached when the user row is provisioned (the driver's own error class)."""

    async def refused(*_: Any) -> None:
        raise ConnectionRefusedError("[Errno 111] Connect call failed")

    monkeypatch.setattr("api.deps.repo.upsert_user", refused)


def log_haystack(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record of the product's loggers as text (message and every record attribute, so a leak shows up
    anywhere). The ``httpx``/``httpcore`` records are the in-process TEST client's own request lines (they carry the
    URL the test sent, by design): the product's agent routes call neither."""

    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    return "\n".join(f"{r.name} {r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in records)


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def comparable(response: httpx.Response) -> tuple[int, bytes, str | None, str | None]:
    """What must be byte-identical between two denials: status, body, content type and the user-id header."""

    return (
        response.status_code,
        response.content,
        response.headers.get("content-type"),
        response.headers.get(USER_HEADER),
    )
