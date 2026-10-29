from __future__ import annotations

import sys
from pathlib import Path

from llm.evidence import ensure_git_workspace, run_command, snapshot_text_files


def test_run_command_captures_success_and_failure(tmp_path: Path) -> None:
    ok = run_command(
        (sys.executable, "-c", "print('hello')"),
        cwd=tmp_path,
    )
    failed = run_command(
        (sys.executable, "-c", "raise SystemExit(7)"),
        cwd=tmp_path,
    )

    assert ok.passed is True
    assert ok.stdout.strip() == "hello"
    assert failed.passed is False
    assert failed.returncode == 7


def test_snapshot_text_files_excludes_runtime_and_binary_files(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    (tmp_path / "module.pyc").write_bytes(b"\x00")
    (tmp_path / "data.bin").write_bytes(b"\x00\xff")

    snapshot = snapshot_text_files(tmp_path)

    assert snapshot == {"module.py": "VALUE = 1\n"}


def test_snapshot_text_files_respects_total_budget(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("aaaa")
    (tmp_path / "b.txt").write_text("bbbb")

    snapshot = snapshot_text_files(
        tmp_path,
        max_file_chars=4,
        max_total_chars=4,
    )

    assert snapshot == {"a.txt": "aaaa"}


def test_run_command_captures_timeout(tmp_path: Path) -> None:
    evidence = run_command(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        cwd=tmp_path,
        timeout=0.05,
    )

    assert evidence.passed is False
    assert evidence.returncode == -1
    assert "timed out" in evidence.stderr


def test_run_command_captures_missing_binary(tmp_path: Path) -> None:
    evidence = run_command(("definitely-not-a-command-xyz",), cwd=tmp_path)

    assert evidence.passed is False
    assert evidence.returncode == -1
    assert evidence.stderr


def test_ensure_git_workspace_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    ensure_git_workspace(workspace)
    head = (workspace / ".git").is_dir()
    ensure_git_workspace(workspace)

    assert head is True
    assert (workspace / ".git").is_dir()
