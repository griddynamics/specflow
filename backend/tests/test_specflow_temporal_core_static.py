"""
Tier 1 tests of ``worker --check`` (AC-2.3, fake health) and the AC-16.0 static ban.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import cli

BACKEND = Path(__file__).resolve().parents[1]


# Worker --check (AC-2.3, fake).
@pytest.fixture
def cli_env(clean_specflow_env, valid_env, monkeypatch):
    for name, value in valid_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "close_logger", lambda: None)


@pytest.mark.parametrize(("result", "code"), [(True, 0), (False, 1)])
def test_worker_check_runs_the_health_checks_for_the_worker(cli_env, monkeypatch, result, code):
    seen: list[str] = []
    health = types.ModuleType("health")

    def run_checks(role, **_options):
        seen.append(role)
        return result

    health.__dict__["run_checks"] = run_checks
    monkeypatch.setitem(sys.modules, "health", health)

    assert cli.main(["worker", "--check"]) == code
    assert seen == ["worker"]


# --------------------------------------------------- AC-16.0 static ban
_BANNED = {"start_time_skipping", "start_local"}


def _banned_calls(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in _BANNED:
                    hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_no_test_server_is_started_anywhere_under_src_or_tests():
    """AC-16.0: ``start_time_skipping()`` and ``start_local()`` download a server."""

    assert _banned_calls(BACKEND / "src") == []
    assert _banned_calls(Path(__file__).parent) == []


def test_the_ban_scanner_finds_a_call_and_ignores_text(tmp_path):
    (tmp_path / "bad.py").write_text(
        "async def f(WorkflowEnvironment):\n    return await WorkflowEnvironment.start_local()\n"
    )
    (tmp_path / "ok.py").write_text('"""start_local() and start_time_skipping() are banned."""\n')

    assert _banned_calls(tmp_path) == ["bad.py:2"]
