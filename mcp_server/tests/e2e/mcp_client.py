"""Thin async MCP stdio client for headless E2E tests."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).parents[3]
_MCP_SERVER_DIR = REPO_ROOT / "mcp_server"


@asynccontextmanager
async def mcp_session(workspace_count: int = 3, extra_env: dict | None = None):
    """Spawn the MCP server as a subprocess and yield an initialised MCP ClientSession."""
    env: dict[str, str] = dict(os.environ)
    env["WORKSPACE_COUNT"] = str(workspace_count)
    env["BACKEND_URL"] = os.getenv("BACKEND_URL", "http://localhost:8000")
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v is not None})

    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "server"],
        env=env,
        cwd=str(_MCP_SERVER_DIR),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, args: dict) -> dict:
    """Call an MCP tool and parse the JSON response."""
    result = await session.call_tool(name, args)
    if not result.content:
        raise AssertionError(f"Tool {name!r} returned empty content")
    text = result.content[0].text
    return json.loads(text)
