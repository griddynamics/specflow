"""FR-MCP-ACC-06 on the API side (docs/mcp-server/requirements.md, access-control.md Decision point): every
project-scoped tool maps to an agent route that runs the one membership check.

The structural half reads the tool contracts (``agent_contracts`` argument models, and the MCP declarations that exist
until the MCP role becomes a router), the route table and the dependency tree of each route of the built app; the
runtime half enumerates the agent routes read from the router modules and calls each project route with a foreign
project: ``404 project_not_found`` and no graph or plan read. ``ROUTER_MODULES`` is a hand-kept copy of the routers
``api.app`` includes; ``test_the_router_modules_cover_every_api_path_the_built_app_serves`` turns red when a router
is added to the app and not to that list, so a new route cannot escape the checks.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from agent_contracts import (
    AGENT_PREFIX,
    AGENT_ROUTES,
    ExpandBriefArgs,
    GetEvidenceArgs,
    GetNodeArgs,
    GetStoryBriefArgs,
    GetTaskBriefArgs,
    GetTaskPathArgs,
    ListFeaturesArgs,
    ListFeatureTasksArgs,
    ListProjectsArgs,
    ListTasksArgs,
    PathArgs,
    SearchSpecArgs,
    SimilarArgs,
    TraceArgs,
)
from api.deps import agent_principal, agent_project, current_agent_user, current_user
from api.routers import (
    agent,
    agent_spec,
    documents,
    graph_build,
    me,
    plan,
    projects,
    source_batches,
    source_removal,
    sources,
)
from auth.dependencies import current_agent_principal, current_principal
from mcp_server.registry import TOOLS
from specflow_api_agent_support import FOREIGN, USER_HEADER, agent_env, serve_agent

SRC = Path(__file__).resolve().parent.parent / "src"
TOOL_ARGUMENTS: dict[str, Any] = {
    "list_projects": ListProjectsArgs,
    "list_features": ListFeaturesArgs,
    "list_tasks": ListTasksArgs,
    "list_feature_tasks": ListFeatureTasksArgs,
    "get_task_path": GetTaskPathArgs,
    "get_task_brief": GetTaskBriefArgs,
    "get_story_brief": GetStoryBriefArgs,
    "trace": TraceArgs,
    "get_node": GetNodeArgs,
    "search_spec": SearchSpecArgs,
    "similar": SimilarArgs,
    "get_evidence": GetEvidenceArgs,
    "path": PathArgs,
    "expand_brief": ExpandBriefArgs,
}
AGENT_ONLY = (
    agent_principal,
    current_agent_principal,
    current_agent_user,
    agent_project,
)
REST_ONLY = (current_principal, current_user)


def scoped(tool: str) -> bool:
    return "project_id" in TOOL_ARGUMENTS[tool].model_fields


ROUTER_MODULES = (
    me,
    agent,
    agent_spec,
    projects,
    documents,
    graph_build,
    plan,
    sources,
    source_batches,
    source_removal,
)
REST_PREFIX = "/api/v1"  # the prefix ``api.app`` includes every router under (the health router has none)


def api_routes(_: Any = None) -> dict[str, APIRoute]:
    """Every route of every router ``api.app`` includes (except health) by its served path. The app's own route list
    is FastAPI-internal (included routers are opaque), so the modules are read; ``test_the_openapi_document...`` ties
    the agent paths read here to what the built app serves."""

    found: dict[str, APIRoute] = {}
    for module in ROUTER_MODULES:
        found |= {f"{REST_PREFIX}{r.path}": r for r in module.router.routes if isinstance(r, APIRoute)}
    return found


def calls_of(dependant: Dependant) -> set[Any]:
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= calls_of(sub)
    return found


def query_names(dependant: Dependant) -> set[str]:
    names = {param.name for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= query_names(sub)
    return names


def test_fr_mcp_acc_06_each_tool_maps_to_exactly_one_agent_route_and_the_table_names_no_other():
    routes = api_routes()
    assert set(AGENT_ROUTES) == set(TOOL_ARGUMENTS)
    assert all(path.startswith(f"{AGENT_PREFIX}/") for path in AGENT_ROUTES.values())
    assert {p for p in routes if p.startswith(f"{AGENT_PREFIX}/")} == set(AGENT_ROUTES.values())
    for tool, path in AGENT_ROUTES.items():
        assert routes[path].methods == {"GET"}, tool


def test_fr_mcp_acc_06_every_project_scoped_tool_maps_to_a_route_that_runs_the_membership_check():
    """The route's dependency tree holds ``agent_project`` exactly when the tool takes a ``project_id``; the
    handler never takes the id itself, and the repository's membership query is named only by ``api.deps``."""

    routes = api_routes()
    for tool, path in AGENT_ROUTES.items():
        route = routes[path]
        tree = calls_of(route.dependant)
        assert (agent_project in tree) == scoped(tool), tool
        assert current_agent_user in tree and agent_principal in tree, tool  # every route provisions the verified user
        assert ("project_id" in query_names(route.dependant)) == scoped(tool), tool
        assert "project_id" not in {p.name for p in route.dependant.query_params}, tool  # only the dependency reads it
        assert "project_id" not in route.endpoint.__code__.co_varnames[: route.endpoint.__code__.co_argcount], tool


def test_fr_mcp_acc_06_the_membership_query_has_one_home_on_the_api_side():
    """``get_member_project`` is called by ``agent_project`` and by no agent route or read module."""

    def names(path: str) -> set[str]:
        tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
        return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }

    assert "get_member_project" in names("api/deps.py")
    for module in ("api/routers/agent.py", "api/routers/agent_spec.py", "api/agent_reads.py", "api/agent_views.py"):
        assert "get_member_project" not in names(module), module


def test_the_agent_dependencies_are_never_used_by_a_rest_route_and_the_rest_ones_never_by_an_agent_route():
    for path, route in api_routes().items():
        tree = calls_of(route.dependant)
        if path.startswith(f"{AGENT_PREFIX}/"):
            assert not tree & set(REST_ONLY), path
        else:
            assert not tree & set(AGENT_ONLY), path


def test_fr_mcp_acc_06_the_mcp_declarations_agree_with_the_contract_models():
    """Until the MCP role is a router (session 06): a tool is declared project-scoped exactly when its arguments
    carry a ``project_id``, and every declared tool has an agent route."""

    assert {t.name for t in TOOLS} == set(TOOL_ARGUMENTS)
    for tool in TOOLS:
        assert tool.project_scoped == scoped(tool.name), tool.name
        assert tool.arguments is TOOL_ARGUMENTS[tool.name], tool.name


async def test_fr_mcp_acc_06_every_project_route_of_the_app_denies_a_foreign_project_and_reads_nothing(
    monkeypatch, valid_env
):
    """Enumerate the app's agent routes: each one with a ``project_id`` answers a foreign project (bob's) with the
    404 of an unknown one, provisioning aside, and neither the graph nor the plan is touched."""

    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        routes = {p: r for p, r in api_routes().items() if p.startswith(f"{AGENT_PREFIX}/")}
        assert len(routes) == len(AGENT_ROUTES)
        denied: dict[str, Any] = {}
        for path, route in routes.items():
            names = query_names(route.dependant)
            if "project_id" in names:
                extra = dict.fromkeys(names & {"task_id", "feature_id"}, "x")  # the other required arguments
                denied[path] = await rig.get(path, project_id=FOREIGN, **extra)
    assert len(denied) == sum(scoped(t) for t in TOOL_ARGUMENTS)
    for path, response in denied.items():
        assert response.status_code == 404 and response.json()["error"]["code"] == "project_not_found", path
        assert response.headers[USER_HEADER] == rig.alice_id
    assert rig.executor.calls == [] and rig.plan.projects == []


@pytest.mark.parametrize("tool", [t for t in TOOL_ARGUMENTS if not scoped(t)])
async def test_a_tool_without_a_project_id_lists_only_the_callers_projects(monkeypatch, valid_env, tool):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        response = await rig.call(tool)
    assert {i["name"] for i in response.json()["items"]} == {
        "Alpha",
        "Beta",
    }  # limit 2 of alice's three; never bob's
    assert "Foreign" not in response.text


async def test_the_openapi_document_serves_exactly_the_agent_paths_read_from_the_router_modules(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        document = (await rig.client.get("/openapi.json")).json()
    served = {p for p in document["paths"] if p.startswith(f"{AGENT_PREFIX}/")}
    assert served == set(AGENT_ROUTES.values()) == {p for p in api_routes() if p.startswith(f"{AGENT_PREFIX}/")}


async def test_the_router_modules_cover_every_api_path_the_built_app_serves(monkeypatch, valid_env):
    """Every ``/api/v1/*`` path of the app's OpenAPI document is one enumerated from ``ROUTER_MODULES``, and the
    other way round: a router included by ``api.app`` but missing from the list fails here."""

    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        document = (await rig.client.get("/openapi.json")).json()
    served = {p for p in document["paths"] if p.startswith(f"{REST_PREFIX}/")}
    assert served == set(api_routes())
