"""The backend-double seam of the MCP contract gate (``test_mcp_golden.py``).

This is the ONE module the split's sessions edit for the gate: session 05 changed the import paths, session 06 swaps
the data source. The golden tests import only ``backend_double`` (registered as a plugin by ``conftest.py``), the
fixture ids below and the ``BackendDouble.serve(scenario)`` entry point; they name no product module.

Now the MCP app is the router it ships as: its API client talks, in-process (``httpx.ASGITransport``), to the REAL API
app, built over the same fake data as the API agent tests (``specflow_api_agent_support.serve_agent``: ``FakeRepo`` for
Postgres, the scripted graph, ``FakePlanService``). The goldens did not change: they are the proof that agents see
no difference.

Scenarios: ``ok``, ``not_ready``, ``no_plan``, ``graph_down``, ``database_down``, ``bug``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from health import Readiness
from mcp_server.registry import install
from mcp_support import MCP_URL, build_app, mcp_env, serving
from specflow_api_agent_support import (
    ALPHA,
    BETA,
    UNKNOWN_PROJECT,
    agent_env,
    refuse_database,
    serve_agent,
)

__all__ = ["ALPHA", "BETA", "SCENARIOS", "UNKNOWN_PROJECT", "BackendDouble", "backend_double"]

SCENARIOS = ("ok", "not_ready", "no_plan", "graph_down", "database_down", "bug")

Serve = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@dataclass
class BackendDouble:
    """What the tests get from the seam: ``serve(scenario)`` yields a client of the running MCP app, whose API (the real
    app over fake data) behaves as ``scenario`` says."""

    serve: Serve


async def _ok() -> None:
    return None


async def _down() -> None:
    raise ConnectionError("down")


@pytest.fixture
def backend_double(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> BackendDouble:
    """The data behind the MCP tools; see the module docstring."""

    @asynccontextmanager
    async def serve(scenario: str = "ok") -> AsyncIterator[httpx.AsyncClient]:
        assert scenario in SCENARIOS, scenario
        async with serve_agent(
            monkeypatch, agent_env(valid_env), scenario=scenario, has_plan=scenario != "no_plan"
        ) as rig:
            if scenario == "database_down":  # after the rig installed its FakeRepo, which it would replace
                refuse_database(monkeypatch)
            if scenario == "not_ready":
                probes = {"database": _ok, "temporal": _ok, "storage": _ok, "graph": _down}
                rig.runtime.readiness = Readiness(probes, ttl=0)
            transport = httpx.ASGITransport(app=rig.app, raise_app_exceptions=False)

            def tools(server: MCPServer) -> None:
                install(server, resource_url=MCP_URL)

            async with serving(build_app(mcp_env(valid_env), transport=transport, tools=tools)) as client:
                yield client

    return BackendDouble(serve=serve)
