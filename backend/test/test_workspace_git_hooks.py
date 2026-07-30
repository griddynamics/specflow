"""
Tests for the prepare-commit-msg hook that stamps the implementation-plan phase (pNN_).
"""
import subprocess
from pathlib import Path

import pytest

from app.utils.workspace_git_hooks import ensure_workspace_git_hooks


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "Test")
    ensure_workspace_git_hooks(repo)


def _commit(repo: Path, message: str, filename: str) -> None:
    (repo / filename).write_text(filename)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)


def _head_subject(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%s")


def test_hook_installed_executable(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "prepare-commit-msg"
    assert hook.is_file()
    assert hook.stat().st_mode & 0o111  # executable bit set


def test_hook_stamps_phase_prefix(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "specflow.phase", "7")
    _commit(tmp_path, "add neighbors endpoint", "a.txt")
    assert _head_subject(tmp_path) == "p07_add neighbors endpoint"


def test_hook_zero_pads_and_reads_current_phase(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "specflow.phase", "13")
    _commit(tmp_path, "build galaxy view", "b.txt")
    assert _head_subject(tmp_path) == "p13_build galaxy view"


def test_hook_leaves_skip_commits_untouched(tmp_path: Path) -> None:
    """SKIP_ commits must stay excluded — the hook must not turn them into pNN_SKIP_."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "specflow.phase", "24")
    _commit(tmp_path, "SKIP_janitor_finalize", "c.txt")
    assert _head_subject(tmp_path) == "SKIP_janitor_finalize"


def test_hook_is_idempotent_for_already_prefixed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "config", "specflow.phase", "7")
    _commit(tmp_path, "p03_already prefixed", "d.txt")
    # Stays p03_, not double-stamped to p07_p03_
    assert _head_subject(tmp_path) == "p03_already prefixed"


def test_hook_no_prefix_when_marker_empty(tmp_path: Path) -> None:
    """An empty marker (as cleared for the deploy loop) leaves commits unphased."""
    _init_repo(tmp_path)
    _git(tmp_path, "config", "specflow.phase", "")
    _commit(tmp_path, "deploy config change", "e.txt")
    assert _head_subject(tmp_path) == "deploy config change"


def test_hook_no_prefix_when_marker_unset(tmp_path: Path) -> None:
    _init_repo(tmp_path)  # never sets specflow.phase
    _commit(tmp_path, "pre-generation seed work", "f.txt")
    assert _head_subject(tmp_path) == "pre-generation seed work"


def test_ensure_hooks_no_git_dir_returns_false(tmp_path: Path) -> None:
    # Plain directory without a .git — safe no-op.
    assert ensure_workspace_git_hooks(tmp_path) is False
