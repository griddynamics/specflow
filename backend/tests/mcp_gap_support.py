"""The MCP app in front of the real API over the shop gap corpus after a gap run (``gap_agent_support``), for the gap
tools' tests through the SDK's client (``test_mcp_answer_gap.py``, ``test_mcp_browse_gaps.py``). The cards link to the
web app at ``WEB``."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from gap_agent_support import GapRig, serve_gaps
from mcp_server.registry import install
from mcp_support import MCP_URL, build_app, mcp_env
from specflow_api_agent_support import agent_env

WEB = "https://app.example.test"


@dataclasses.dataclass
class Served:
    gaps: GapRig
    app: Starlette


@asynccontextmanager
async def serve_mcp_gaps(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> AsyncIterator[Served]:
    """The gap rig and the MCP app whose API calls reach the rig's app in process."""

    async with serve_gaps(monkeypatch, agent_env(valid_env, PUBLIC_URL=WEB)) as gaps:
        transport = httpx.ASGITransport(app=gaps.rig.app, raise_app_exceptions=False)

        def tools(server: MCPServer) -> None:
            install(server, resource_url=MCP_URL)

        yield Served(gaps, build_app(mcp_env(valid_env), transport=transport, tools=tools))
