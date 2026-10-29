#!/usr/bin/env python3
"""Self-tests of ../run-all.sh, the kit's track runner (option handling and the track loop).

Run with the CI report tests: `env -u VIRTUAL_ENV uv run --project validation/python/lint pytest validation/ci`.
Only paths that start no track tool run here: a project without the track's input file is
skipped by the track itself, so no uv, npm or Docker call is made.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUN_ALL = Path(__file__).resolve().parent.parent / "run-all.sh"
SH = shutil.which("sh") or "/bin/sh"
TRACKS = ["python-lint", "python-scripts", "python-arch", "typescript-lint", "typescript-scripts", "cypher", "sql"]


def _run_all(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([SH, str(RUN_ALL), *args], capture_output=True, text=True, check=False, timeout=60)


def test_list_prints_every_track_in_order() -> None:
    result = _run_all("--list")
    assert result.returncode == 0
    assert result.stdout.split() == TRACKS


def test_help_prints_the_usage_block() -> None:
    result = _run_all("--help")
    assert result.returncode == 0
    assert "--project DIR" in result.stdout
    assert "--selftest" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "unknown option: --bogus"),
        (["--selftest", "--tracks", "python-lint,nope"], "unknown track: nope"),
        (["--selftest", "--skip", "nope"], "unknown track: nope"),
        ([], "--project is required"),
        (["--project"], "--project needs a value"),
        (["--tracks"], "--tracks needs a value"),
        (["--skip"], "--skip needs a value"),
    ],
)
def test_a_usage_error_exits_2_with_its_reason(args: list[str], message: str) -> None:
    result = _run_all(*args)
    assert result.returncode == 2
    assert message in result.stderr


def test_a_project_that_is_not_a_directory_exits_2(tmp_path: Path) -> None:
    result = _run_all("--project", str(tmp_path / "missing"))
    assert result.returncode == 2
    assert "not a directory" in result.stderr


def test_tracks_without_their_input_file_are_skipped_and_the_run_passes(tmp_path: Path) -> None:
    result = _run_all("--project", str(tmp_path), "--tracks", "python-arch,cypher,sql")
    assert result.returncode == 0, result.stderr
    for track, needed in [("python-arch", "archkit.toml"), ("cypher", "cypher-checks.toml"), ("sql", "sqlcheck.toml")]:
        assert f"skip {track}: {tmp_path}/{needed} not found" in result.stdout
        assert f"== {track}: ok" in result.stdout
    assert result.stdout.rstrip().endswith("== all selected tracks passed")


def test_a_skipped_track_is_reported_and_not_run(tmp_path: Path) -> None:
    result = _run_all("--project", str(tmp_path), "--tracks", "sql,cypher", "--skip", "sql")
    assert result.returncode == 0, result.stderr
    assert "== sql: skipped" in result.stdout
    assert "skip sql:" not in result.stdout
    assert "== cypher: ok" in result.stdout
