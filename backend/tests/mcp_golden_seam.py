"""The backend-double seam of the MCP contract gate (``test_mcp_golden.py``).

This is the ONE module the split's later sessions edit for the gate: session 05 changes the import paths, session 06
swaps the data source for a fake HTTP API. The golden tests import only ``backend_double`` (registered as a plugin by
``conftest.py``), the fixture ids below and the ``BackendDouble.serve(scenario)`` entry point; they name no product
module. Today the data comes from the REST tests' ``FakeRepo`` (Postgres), ``paged_executor`` (the graph) and
``FakePlanService`` (the plan).

Scenarios: ``ok``, ``not_ready``, ``no_plan``, ``graph_down``, ``database_down``, ``bug``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from api.mcp.registry import install
from graph.core import ConfigurationError, GraphReader, InfrastructureUnavailableError
from graph_support import shipped_ontology
from mcp_support import MCP_URL, build_app, fake_runtime, mcp_env, serving
from test_graph_reader import ScriptedExecutor, paged_executor
from test_mcp_plan_tools import FakePlanService
from test_specflow_api_fakes import FakeRepo

# Fixed fixture data (QA-seed shaped): alice owns three projects, bob one.
ALPHA, BETA, GAMMA, FOREIGN = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3, 4))
UNKNOWN_PROJECT = "00000000-0000-4000-8000-0000000000ff"
FEATURE_IDS = ["FEAT::F1", "FEAT::F2", "FEAT::F3"]
SEEDED = (
    (ALPHA, "Alpha", "alice", 3),
    (BETA, "Beta", "alice", 2),
    (GAMMA, "Gamma", "alice", 1),
    (FOREIGN, "Foreign", "bob", 4),
)
SCENARIOS = ("ok", "not_ready", "no_plan", "graph_down", "database_down", "bug")

Serve = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]
Answer = Callable[[Any], list[dict[str, Any]]]


@dataclass
class BackendDouble:
    """What the tests get from the seam: ``serve(scenario)`` yields a client of the running app, whose backend behaves
    as ``scenario`` says."""

    serve: Serve


def _failing(error: Exception) -> Answer:
    def raise_it(_: Any) -> list[dict[str, Any]]:
        raise error

    return raise_it


def graph_answers(scenario: str) -> ScriptedExecutor:
    if scenario == "graph_down":
        return ScriptedExecutor({"count_nodes": _failing(InfrastructureUnavailableError("neo4j_unreachable"))})
    if scenario == "bug":
        return ScriptedExecutor({"count_nodes": _failing(ConfigurationError("unknown_label", label="X"))})
    return paged_executor(FEATURE_IDS, "Feature")


async def _user(repo: FakeRepo, owner: str) -> Any:
    principal = SimpleNamespace(subject=f"user:{owner}@example.com", email=f"{owner}@example.com", name=None)
    return await repo.upsert_user(None, principal)


async def seed(repo: FakeRepo) -> None:
    users: dict[str, Any] = {}
    for project_id, name, owner, day in SEEDED:
        if owner not in users:
            users[owner] = await _user(repo, owner)
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


def refuse_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(*_: Any) -> None:
        raise ConnectionRefusedError("[Errno 111] Connect call failed")

    monkeypatch.setattr("api.mcp.access.repositories.upsert_user", refused)


def build_runtime(scenario: str, repo: FakeRepo) -> Any:
    ontology = shipped_ontology()
    return fake_runtime(
        graph_ok=scenario != "not_ready",
        reader=GraphReader(graph_answers(scenario), ontology),
        session_factory=repo,
        ontology=ontology,
        plan=FakePlanService(has_plan=scenario != "no_plan"),
    )


@pytest.fixture
def backend_double(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> BackendDouble:
    """The data behind the MCP tools; see the module docstring."""

    @asynccontextmanager
    async def serve(scenario: str = "ok") -> AsyncIterator[httpx.AsyncClient]:
        assert scenario in SCENARIOS, scenario
        repo = FakeRepo()
        repo.install(monkeypatch)
        await seed(repo)
        if scenario == "database_down":
            refuse_database(monkeypatch)
        runtime = build_runtime(scenario, repo)

        def tools(server: Any) -> None:
            install(server, resource_url=MCP_URL)

        async with serving(build_app(mcp_env(valid_env), runtime=runtime, tools=tools)) as client:
            yield client

    return BackendDouble(serve=serve)
