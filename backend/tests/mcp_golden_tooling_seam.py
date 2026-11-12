"""The backend-double seam of the golden gate of the MCP tooling tools (``test_mcp_golden_tooling.py``, D29).

Like ``mcp_golden_seam`` (the gate of the MCP split, whose goldens stay frozen) but behind the router is the API over
the shop graph of ``spec_reads_support``: the real readers and libraries over a fixture graph, a plan, findings and the
project's members. The golden tests import only ``tooling_double`` and ``BackendDouble.serve(scenario)``.

Scenarios: ``ok``, ``no_plan``, ``graph_down``, ``database_down``, ``bug``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from graph.core import ConfigurationError, InfrastructureUnavailableError
from mcp_golden_seam import ALPHA, UNKNOWN_PROJECT
from mcp_server.registry import install
from mcp_support import MCP_URL, build_app, mcp_env, serving
from spec_reads_support import CHECKOUT, serve_spec
from specflow_api_agent_support import agent_env, refuse_database

__all__ = ["ALPHA", "CHECKOUT", "SCENARIOS", "UNKNOWN_PROJECT", "BackendDouble", "tooling_double"]

SCENARIOS = ("ok", "no_plan", "graph_down", "database_down", "bug")
FAILURES: dict[str, Exception] = {
    "graph_down": InfrastructureUnavailableError("neo4j_unreachable"),
    "bug": ConfigurationError("unknown_label", label="X"),
}

Serve = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@dataclass
class BackendDouble:
    """``serve(scenario)`` yields a client of the running MCP app over the API over the shop graph."""

    serve: Serve


@pytest.fixture
def tooling_double(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> BackendDouble:
    @asynccontextmanager
    async def serve(scenario: str = "ok") -> AsyncIterator[httpx.AsyncClient]:
        assert scenario in SCENARIOS, scenario
        async with serve_spec(
            monkeypatch, agent_env(valid_env), has_plan=scenario != "no_plan", fail=FAILURES.get(scenario)
        ) as spec:
            if scenario == "database_down":  # after the rig installed its FakeRepo, which it would replace
                refuse_database(monkeypatch)
            transport = httpx.ASGITransport(app=spec.rig.app, raise_app_exceptions=False)

            def tools(server: MCPServer) -> None:
                install(server, resource_url=MCP_URL)

            async with serving(build_app(mcp_env(valid_env), transport=transport, tools=tools)) as client:
                yield client

    return BackendDouble(serve=serve)
