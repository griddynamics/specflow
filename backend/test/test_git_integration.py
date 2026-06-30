"""
Integration tests for git command sequences.

These tests run real git commands against temporary bare remotes and workspace
directories. No mocking of run_git. They verify that the actual git argument
sequences in workspace_pool, workspace_manager, and git_archive_service produce
the correct git state.

Run with: RUN_GIT_INTEGRATION_TESTS=1 pytest -m integration
Skipped automatically when RUN_GIT_INTEGRATION_TESTS is not set (unit-test runs).
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.services.git_archive_service import GitArchiveService
from app.services.git_utils import GitCommandError, run_git
from app.services.workspace_manager import WorkspaceManager
from app.services.workspace_pool import WorkspacePoolService
from app.schemas.workspace import WorkspaceSettings
from app.database.memory import InMemoryDatabase


# Skip the entire module unless explicitly opted in.
# Set RUN_GIT_INTEGRATION_TESTS=1 to run (done automatically by make integration-tests).
pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_GIT_INTEGRATION_TESTS"),
    reason="Git integration tests skipped (set RUN_GIT_INTEGRATION_TESTS=1 to run)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    """Run a real git command synchronously. Used only for test assertions."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _setup_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """
    Create a workspace dir with git initialized and a bare remote as origin.
    Returns (workspace_path, bare_remote_path).

    The workspace has one initial commit already pushed to origin/main so
    subsequent pushes work without needing to set upstream.
    """
    bare = tmp_path / "remote.git"
    ws = tmp_path / "workspace"
    ws.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "SpecFlow System"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "gain@system.local"], cwd=ws, check=True, capture_output=True)
    # Initial commit so origin/main exists
    subprocess.run(["git", "commit", "--allow-empty", "-m", "root"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws, check=True, capture_output=True)
    return ws, bare


def _workspace_settings(ws_path: Path) -> WorkspaceSettings:
    return WorkspaceSettings(
        name="test-ws",
        workspace_path=str(ws_path),
        provider="anthropic",
        model="claude-sonnet-4-6",
    )


# ---------------------------------------------------------------------------
# run_git basics
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestRunGit:
    @pytest.mark.asyncio
    async def test_success_returns_stdout(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        result = await run_git(tmp_path, ["status", "--porcelain"])
        assert result == ""

    @pytest.mark.asyncio
    async def test_failure_raises_git_command_error(self, tmp_path):
        """Not a git repo → raises GitCommandError."""
        with pytest.raises(GitCommandError) as exc_info:
            await run_git(tmp_path, ["status", "--porcelain"])
        assert exc_info.value.returncode != 0
        assert exc_info.value.stderr != ""

    @pytest.mark.asyncio
    async def test_stdout_is_stripped(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        sha = await run_git(tmp_path, ["rev-parse", "HEAD"])
        # Should be a 40-char hex SHA, no whitespace
        assert len(sha) == 40
        assert sha == sha.strip()


# ---------------------------------------------------------------------------
# WorkspaceManager.commit_and_push_baseline
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestCommitAndPushBaseline:
    def _make_manager(self) -> WorkspaceManager:
        settings = Mock()
        settings.STANDARDS_DIR_NAME = "standards"
        settings.AGENT_BASE_PATH = "/agent"
        return WorkspaceManager(settings)

    @pytest.mark.asyncio
    async def test_uncommitted_files_are_committed_and_pushed(self, tmp_path):
        ws, _bare = _setup_repo_with_remote(tmp_path)
        (ws / "specs.txt").write_text("spec content")
        (ws / "plan.md").write_text("plan content")

        manager = self._make_manager()
        await manager.commit_and_push_baseline(_workspace_settings(ws))

        # Working tree is clean
        assert _git(ws, "status", "--porcelain") == ""
        # origin/main is up to date — no unpushed commits
        assert _git(ws, "log", "origin/main..HEAD", "--oneline") == ""
        # Baseline commit exists in log and uses SKIP_ prefix so it is excluded from P10Y
        log = _git(ws, "log", "--oneline")
        assert "SKIP_generation_baseline" in log

    @pytest.mark.asyncio
    async def test_custom_commit_message(self, tmp_path):
        ws, _bare = _setup_repo_with_remote(tmp_path)
        (ws / "leftover.py").write_text("leftover")

        manager = self._make_manager()
        await manager.commit_and_push_baseline(
            _workspace_settings(ws), commit_message="SKIP_janitor_finalize"
        )

        log = _git(ws, "log", "--oneline")
        assert "SKIP_janitor_finalize" in log
        assert _git(ws, "log", "origin/main..HEAD", "--oneline") == ""

    @pytest.mark.asyncio
    async def test_clean_workspace_no_extra_commit(self, tmp_path):
        ws, _bare = _setup_repo_with_remote(tmp_path)
        commit_count_before = len(_git(ws, "log", "--oneline").splitlines())

        manager = self._make_manager()
        await manager.commit_and_push_baseline(_workspace_settings(ws))

        commit_count_after = len(_git(ws, "log", "--oneline").splitlines())
        assert commit_count_after == commit_count_before

    @pytest.mark.asyncio
    async def test_idempotent_second_run_no_extra_commit(self, tmp_path):
        ws, _bare = _setup_repo_with_remote(tmp_path)
        (ws / "file.txt").write_text("data")

        manager = self._make_manager()
        await manager.commit_and_push_baseline(_workspace_settings(ws))
        commit_count_after_first = len(_git(ws, "log", "--oneline").splitlines())

        # Second call on already-clean workspace
        await manager.commit_and_push_baseline(_workspace_settings(ws))
        commit_count_after_second = len(_git(ws, "log", "--oneline").splitlines())

        assert commit_count_after_second == commit_count_after_first

    @pytest.mark.asyncio
    async def test_no_remote_raises(self, tmp_path):
        """Push failure (no remote) must raise — local-only commits can never be archived."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=ws, check=True, capture_output=True)
        (ws / "file.txt").write_text("data")

        manager = self._make_manager()
        with pytest.raises(GitCommandError):
            await manager.commit_and_push_baseline(_workspace_settings(ws))


# ---------------------------------------------------------------------------
# WorkspacePoolService.initialize_git_repo
# ---------------------------------------------------------------------------

_MOCK_AUTH = Mock(token="fake-token-for-tests")


@pytest.mark.integration
class TestInitializeGitRepo:
    @pytest.mark.asyncio
    async def test_initial_commit_pushed_to_remote(self, tmp_path):
        bare = tmp_path / "remote.git"
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=ws, check=True, capture_output=True)
        # Create a file so the commit is non-empty
        (ws / "readme.txt").write_text("hello")

        pool = WorkspacePoolService(db=InMemoryDatabase())
        with patch("app.services.workspace_pool.resolve_github_auth_for_generation_id", return_value=_MOCK_AUTH):
            await pool.initialize_git_repo(ws, "est-init-1")

        # Commit exists locally
        log = _git(ws, "log", "--oneline")
        assert "SKIP_initial_user_source" in log
        # Commit was pushed — origin/main exists
        remote_log = _git(ws, "log", "origin/main", "--oneline")
        assert "SKIP_initial_user_source" in remote_log

    @pytest.mark.asyncio
    async def test_repairs_when_commit_objects_missing(self, tmp_path):
        """refs/heads/main points at an OID not in .git/objects — same symptom as overwriting refs from another clone."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "SpecFlow System"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "gain@system.local"], cwd=ws, check=True, capture_output=True)
        (ws / "specs.md").write_text("# hello")
        subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "orphan-base"], cwd=ws, check=True, capture_output=True)

        ref_main = ws / ".git" / "refs" / "heads" / "main"
        ref_main.write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")

        pool = WorkspacePoolService(db=InMemoryDatabase())
        with patch(
            "app.services.workspace_pool.resolve_github_auth_for_generation_id",
            return_value=_MOCK_AUTH,
        ):
            await pool.initialize_git_repo(ws, "est-repair-missing-objects")

        log = _git(ws, "log", "--oneline")
        assert "SKIP_initial_user_source" in log

    @pytest.mark.asyncio
    async def test_seeds_gitignore_before_initial_commit(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "readme.txt").write_text("hello")

        pool = WorkspacePoolService(db=InMemoryDatabase())
        with patch("app.services.workspace_pool.resolve_github_auth_for_generation_id", return_value=_MOCK_AUTH):
            await pool.initialize_git_repo(ws, "est-gitignore-1")

        gitignore = ws / ".gitignore"
        assert gitignore.exists()
        assert "node_modules/" in gitignore.read_text(encoding="utf-8")
        tracked = _git(ws, "ls-files", ".gitignore")
        assert tracked.strip() == ".gitignore"

    @pytest.mark.asyncio
    async def test_no_remote_is_non_fatal(self, tmp_path):
        """initialize_git_repo without a remote should not raise."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        (ws / "src.py").write_text("print('hello')")

        pool = WorkspacePoolService(db=InMemoryDatabase())
        with patch("app.services.workspace_pool.resolve_github_auth_for_generation_id", return_value=_MOCK_AUTH):
            # Must not raise
            await pool.initialize_git_repo(ws, "est-no-remote")

        log = _git(ws, "log", "--oneline")
        assert "SKIP_initial_user_source" in log


# ---------------------------------------------------------------------------
# GitArchiveService.archive_branch / verify_archive_branch
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestGitArchiveServiceIntegration:
    def _setup_archive_service(self, workspace_root: Path) -> GitArchiveService:
        return GitArchiveService(workspace_root=str(workspace_root))

    def _setup_workspace_with_commits(self, workspaces_root: Path, ws_id: str) -> tuple[Path, Path]:
        """
        Create workspaces_root/ws_id as a local repo with a bare remote.
        Returns (ws_path, bare_path).
        """
        bare = workspaces_root / f"{ws_id}.git"
        ws = workspaces_root / ws_id
        ws.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "work done"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws, check=True, capture_output=True)
        return ws, bare

    @pytest.mark.asyncio
    async def test_archive_branch_pushes_to_remote(self, tmp_path):
        ws, bare = self._setup_workspace_with_commits(tmp_path, "ws-1")
        svc = self._setup_archive_service(tmp_path)

        result = await svc.archive_branch("ws-1", "est-arc-1")

        assert result is True
        # Branch exists on remote
        remote_refs = _git(ws, "ls-remote", "--heads", "origin", "archive/est-arc-1")
        assert "archive/est-arc-1" in remote_refs

    @pytest.mark.asyncio
    async def test_archive_branch_already_at_sha_no_repush(self, tmp_path):
        """archive_branch returns True immediately when remote already matches HEAD."""
        ws, bare = self._setup_workspace_with_commits(tmp_path, "ws-1")
        svc = self._setup_archive_service(tmp_path)

        # First push
        await svc.archive_branch("ws-1", "est-arc-2")
        head_sha = _git(ws, "rev-parse", "HEAD")

        # Second call — should confirm without error
        result = await svc.archive_branch("ws-1", "est-arc-2")
        assert result is True
        # SHA unchanged on remote
        remote_line = _git(ws, "ls-remote", "origin", "refs/heads/archive/est-arc-2")
        assert remote_line.split()[0] == head_sha

    @pytest.mark.asyncio
    async def test_verify_archive_branch_missing_pushes_it(self, tmp_path):
        ws, bare = self._setup_workspace_with_commits(tmp_path, "ws-1")
        svc = self._setup_archive_service(tmp_path)

        result = await svc.verify_archive_branch("ws-1", "est-verify-1")

        assert result is True
        remote_refs = _git(ws, "ls-remote", "--heads", "origin", "archive/est-verify-1")
        assert "archive/est-verify-1" in remote_refs

    @pytest.mark.asyncio
    async def test_verify_archive_branch_sha_match_returns_true(self, tmp_path):
        ws, bare = self._setup_workspace_with_commits(tmp_path, "ws-1")
        svc = self._setup_archive_service(tmp_path)

        await svc.archive_branch("ws-1", "est-verify-2")
        result = await svc.verify_archive_branch("ws-1", "est-verify-2")

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_archive_branch_sha_mismatch_force_pushes(self, tmp_path):
        """If local HEAD moves past what was archived, verify force-pushes the new SHA."""
        ws, bare = self._setup_workspace_with_commits(tmp_path, "ws-1")
        svc = self._setup_archive_service(tmp_path)

        # Archive at current HEAD
        await svc.archive_branch("ws-1", "est-verify-3")

        # Add a new commit — HEAD moves forward
        subprocess.run(["git", "commit", "--allow-empty", "-m", "extra work"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=ws, check=True, capture_output=True)
        new_head = _git(ws, "rev-parse", "HEAD")

        result = await svc.verify_archive_branch("ws-1", "est-verify-3")

        assert result is True
        # Remote archive branch now points to the new HEAD
        remote_line = _git(ws, "ls-remote", "origin", "refs/heads/archive/est-verify-3")
        assert remote_line.split()[0] == new_head


# ---------------------------------------------------------------------------
# WorkspacePoolService._reset_workspace_for_allocation
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestResetWorkspaceForAllocation:
    @pytest.mark.asyncio
    async def test_reset_clears_local_and_remote_main(self, tmp_path):
        """
        Scenario: Workspace has stale commits on both local and remote main branches.
        After reset, both should be clean (orphan main with no commits).
        """
        # Setup: Create workspace with bare remote and some commits on main
        bare = tmp_path / "remote.git"
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, check=True, capture_output=True)
        
        # Create some "stale" commits from previous generation
        subprocess.run(["git", "commit", "--allow-empty", "-m", "stale commit 1"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "stale commit 2"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws, check=True, capture_output=True)
        
        # Verify stale commits exist
        log_before = _git(ws, "log", "--oneline")
        assert "stale commit 1" in log_before
        assert "stale commit 2" in log_before
        
        # Execute: Reset workspace
        pool = WorkspacePoolService(db=InMemoryDatabase())
        await pool._reset_workspace_for_allocation(ws, "test-ws")
        
        # Verify: On main branch
        current_branch = _git(ws, "symbolic-ref", "--short", "HEAD")
        assert current_branch == "main"

        # Verify: Exactly one commit on local main — the empty reset commit.
        # (One empty commit is required so the branch ref exists for force-push.)
        log = _git(ws, "log", "--oneline")
        lines = [line for line in log.splitlines() if line.strip()]
        assert len(lines) == 1
        assert "workspace reset" in lines[0]

        # Verify: No stale commits from previous generation remain
        assert "stale commit" not in log

        # Verify: Working directory is clean
        status = _git(ws, "status", "--porcelain")
        assert status == ""

        # Verify: Remote main matches local (force-pushed)
        remote_refs = _git(ws, "ls-remote", "origin", "refs/heads/main")
        assert "refs/heads/main" in remote_refs
    
    @pytest.mark.asyncio
    async def test_reset_after_archive_branch_checkout(self, tmp_path):
        """
        Scenario: Workspace is on archive branch after previous generation cleanup.
        Reset should switch back to main and clear all commits.
        """
        # Setup: Create workspace on archive branch
        bare = tmp_path / "remote.git"
        ws = tmp_path / "workspace"
        ws.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, check=True, capture_output=True)
        
        # Create commits and archive branch (simulating previous generation)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "work"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "archive/est-old"], cwd=ws, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "archive/est-old"], cwd=ws, check=True, capture_output=True)
        
        # Verify we're on archive branch
        current_branch_before = _git(ws, "symbolic-ref", "--short", "HEAD")
        assert current_branch_before == "archive/est-old"
        
        # Execute: Reset workspace
        pool = WorkspacePoolService(db=InMemoryDatabase())
        await pool._reset_workspace_for_allocation(ws, "test-ws")
        
        # Verify: Switched to main
        current_branch_after = _git(ws, "symbolic-ref", "--short", "HEAD")
        assert current_branch_after == "main"
        
        # Verify: Exactly one commit on local main — the empty reset commit
        log = _git(ws, "log", "--oneline")
        lines = [line for line in log.splitlines() if line.strip()]
        assert len(lines) == 1
        assert "workspace reset" in lines[0]

