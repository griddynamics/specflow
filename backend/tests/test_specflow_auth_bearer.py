"""FR-MCP-AUTH-07: the bearer parser lives in a module that never loads FastAPI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import auth.bearer
import auth.dependencies

BACKEND = Path(__file__).resolve().parents[1]


def test_importing_the_bearer_module_loads_no_fastapi() -> None:
    code = (
        "import sys\n"
        "import auth.bearer\n"
        "bad = [m for m in sys.modules if m == 'fastapi' or m.startswith('fastapi.')]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=BACKEND,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-500:]


def test_the_rest_dependency_uses_the_same_parser() -> None:
    assert auth.dependencies.bearer_token is auth.bearer.bearer_token
