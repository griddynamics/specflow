"""The backend-double seam of the golden gate of the MCP tooling tools (``test_mcp_golden_tooling.py``, D29).

Like ``mcp_golden_seam`` (the gate of the MCP split, whose goldens stay frozen) but behind the router is the API over
the shop graph of ``spec_reads_support``: the real readers and libraries over a fixture graph, a plan, findings and the
project's members. The golden tests import only ``tooling_double`` and ``BackendDouble.serve(scenario)``.

Scenarios: ``ok``, ``no_plan``, ``graph_down``, ``database_down``, ``bug``; and, with the plan in the in-memory
executor so the story calls can write: ``free`` (as it is), ``claimed`` (Checkout held by Ben), ``applying`` (a plan
persist running), ``held_applying`` (Checkout held by Alice, token 1, and a plan persist running); ``gaps``: the API
over the shop gap corpus after a gap run and the mini plan of ``gap_agent_support`` (open gaps in briefs,
``list_gaps``, the rows of ``browse_gaps``, the card of ``open_gap`` and the cards of ``list_open_gaps``, which link
to the web app at ``https://app.example.test``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from gap_agent_support import serve_gaps
from graph.core import ConfigurationError, InfrastructureUnavailableError
from mcp_golden_seam import ALPHA, UNKNOWN_PROJECT
from mcp_server.registry import install
from mcp_support import MCP_URL, build_app, mcp_env, serving
from spec_reads_support import CHECKOUT, serve_spec
from specflow_api_agent_support import agent_env, refuse_database

__all__ = ["ALPHA", "CHECKOUT", "SCENARIOS", "UNKNOWN_PROJECT", "BackendDouble", "tooling_double"]

GAP_RUN_INSTANT = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)

SCENARIOS = (
    "ok",
    "no_plan",
    "graph_down",
    "database_down",
    "bug",
    "free",
    "claimed",
    "applying",
    "held_applying",
    "gaps",
)
FAILURES: dict[str, Exception] = {
    "graph_down": InfrastructureUnavailableError("neo4j_unreachable"),
    "bug": ConfigurationError("unknown_label", label="X"),
}

WRITABLE: dict[str, str] = {name: name for name in ("free", "claimed", "applying", "held_applying")}

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
        if scenario == "gaps":
            # One instant for the whole gap run: the gaps' order and cursors depend on their creation times.
            monkeypatch.setattr("graph.gap.run._now", lambda: GAP_RUN_INSTANT)
            async with (
                serve_gaps(monkeypatch, agent_env(valid_env, PUBLIC_URL="https://app.example.test")) as gaps,
                serving(_mcp_app(valid_env, gaps.rig.app)) as client,
            ):
                yield client
            return
        async with serve_spec(
            monkeypatch,
            agent_env(valid_env),
            has_plan=scenario != "no_plan",
            fail=FAILURES.get(scenario),
            writable=WRITABLE.get(scenario),
        ) as spec:
            if scenario == "database_down":  # after the rig installed its FakeRepo, which it would replace
                refuse_database(monkeypatch)
            async with serving(_mcp_app(valid_env, spec.rig.app)) as client:
                yield client

    return BackendDouble(serve=serve)


def _mcp_app(valid_env: dict[str, str], api: Any) -> Any:
    """The MCP app whose API calls go to the in-process ``api`` app."""

    transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)

    def tools(server: MCPServer) -> None:
        install(server, resource_url=MCP_URL)

    return build_app(mcp_env(valid_env), transport=transport, tools=tools)
