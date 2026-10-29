"""`run.sh` end to end on the neutral sample project `examples/clean_project`.

The project is copied to a temporary directory with the kit's `.semgrepignore` at its root,
the way an adopter sets it up, so no tool writes a cache into the kit."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.semgrep_support import KIT_DIR

EXAMPLE = KIT_DIR / "examples" / "clean_project"

VIOLATION = '''"""Prints the grand total."""

from __future__ import annotations

from src.orders.service import grand_total


def show_grand_total() -> None:
    print(grand_total())
'''


def _adopt(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project)
    shutil.copy(KIT_DIR / ".semgrepignore", project / ".semgrepignore")
    return project


def _run_kit(project: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash, "bash is not on PATH"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "SEMGREP_ENABLE_VERSION_CHECK": "0"}
    return subprocess.run(
        [bash, str(KIT_DIR / "run.sh"), str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.timeout(600)
def test_a_clean_project_passes_every_step(tmp_path: Path) -> None:
    result = _run_kit(_adopt(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    for step in ("ruff-format", "ruff", "mypy", "semgrep", "import-linter", "deptry"):
        assert f"==> {step}: " in result.stdout


@pytest.mark.timeout(600)
def test_a_violation_fails_the_run(tmp_path: Path) -> None:
    project = _adopt(tmp_path)
    (project / "src" / "orders" / "report.py").write_text(VIOLATION)
    result = _run_kit(project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "T201" in result.stdout


def test_a_missing_project_directory_exits_2(tmp_path: Path) -> None:
    result = _run_kit(tmp_path / "absent")
    assert result.returncode == 2
    assert "no such project directory" in result.stderr


@pytest.mark.timeout(600)
def test_a_vendored_kit_keeps_the_projects_deptry_extend_exclude(tmp_path: Path) -> None:
    project = _adopt(tmp_path)
    kit = project / "tools" / "kit"
    shutil.copytree(
        KIT_DIR,
        kit,
        ignore=shutil.ignore_patterns(".venv", "node_modules", "__pycache__", ".*_cache"),
    )
    (project / "src" / "orders" / "test_x.py").write_text(
        '"""Colocated test."""\n\nfrom __future__ import annotations\n\nimport pytest\n\n'
        "assert pytest\n"
    )
    env = {**os.environ, "KIT_SKIP": "ruff-format ruff mypy semgrep import-linter"}
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run(
        [bash, str(kit / "run.sh"), str(project)],
        capture_output=True,
        text=True,
        check=False,
        env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "==> deptry: " in result.stdout


@pytest.mark.timeout(600)
def test_the_configurable_semgrep_examples_accept_the_sample_project(tmp_path: Path) -> None:
    project = _adopt(tmp_path)
    bash = shutil.which("bash")
    assert bash
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "KIT_SEMGREP_CONFIGURABLE": str(KIT_DIR / "configurable" / "semgrep"),
    }
    result = subprocess.run(
        [bash, str(KIT_DIR / "run.sh"), str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
