"""``specflow mcp``: parsing, ``--check-config`` with minimal variables, the lazy server module (FR-MCP-OPS-01)."""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

import cli
from logger.logger import ERROR
from mcp_support import BACKEND, MCP_URL

MINIMAL = {
    "APP_ENV": "local",
    "DATABASE_HOST": "localhost",
    "DATABASE_NAME": "specflow",
    "DATABASE_USER": "specflow",
    "DATABASE_PASSWORD": "pw-for-tests",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "pw-for-tests",
    "OIDC_ISSUER": "http://localhost:8180/realms/specflow",
    "AUTH_ALLOWED_EMAIL_DOMAINS": "example.com",
    "MCP_RESOURCE_URL": MCP_URL,
}
CHECK = "import sys; sys.path.insert(0, 'src'); import cli; sys.exit(cli.main(['mcp', '--check-config']))"


def check_config(**over: str) -> subprocess.CompletedProcess[str]:
    """``specflow mcp --check-config`` in a process with ONLY the minimal variables (``env -i``)."""

    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **MINIMAL, **over}
    return subprocess.run(
        [sys.executable, "-c", CHECK], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def test_check_config_exits_0_with_only_the_minimal_variables():
    """FR-MCP-OPS-01: no Temporal, storage, LLM or agent variable is needed."""

    assert check_config().returncode == 0


def test_check_config_names_the_bad_resource_url():
    """FR-MCP-OPS-01 / FR-MCP-AUTH-09: a path other than /mcp exits 2 naming MCP_RESOURCE_URL."""

    done = check_config(MCP_RESOURCE_URL="http://localhost:8002/other")

    assert done.returncode == 2
    assert "MCP_RESOURCE_URL" in done.stdout + done.stderr


@pytest.fixture
def mcp_cli(clean_specflow_env, valid_env, monkeypatch):
    for name, value in {**valid_env, "MCP_RESOURCE_URL": MCP_URL}.items():
        clean_specflow_env.setenv(name, value)
    events: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(cli, "close_logger", lambda: None)
    monkeypatch.setattr(cli, "log_event", lambda _l, level, event, _m=None, /, **f: events.append((level, event, f)))
    return events


def test_the_mcp_command_parses_check_config():
    parsed = cli._parser().parse_args(["mcp", "--check-config"])

    assert (parsed.role, parsed.check_config) == ("mcp", True)


def test_check_config_loads_no_server_module(mcp_cli, monkeypatch):
    monkeypatch.delitem(sys.modules, "api.mcp.server", raising=False)
    monkeypatch.setitem(sys.modules, "api.mcp", types.ModuleType("api.mcp"))  # a real import would find no server

    assert cli.main(["mcp", "--check-config"]) == 0
    assert mcp_cli == []
    assert "api.mcp.server" not in sys.modules


def test_a_bad_environment_exits_2_and_logs_the_variable(mcp_cli, monkeypatch):
    monkeypatch.setenv("MCP_RESOURCE_URL", "http://localhost:8002/other")

    assert cli.main(["mcp", "--check-config"]) == 2
    assert [(level, event, f.get("variables")) for level, event, f in mcp_cli] == [
        (ERROR, "config.validation.failed", ["MCP_RESOURCE_URL"])
    ]


def test_the_mcp_command_runs_the_server_with_the_settings(mcp_cli, monkeypatch):
    calls: list[object] = []
    server = types.ModuleType("api.mcp.server")
    vars(server)["run"] = lambda settings: calls.append(settings) or 0
    monkeypatch.setitem(sys.modules, "api.mcp.server", server)

    assert cli.main(["mcp"]) == 0
    assert calls[0].mcp.resource_url == MCP_URL
