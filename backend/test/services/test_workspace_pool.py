"""
Tests for WorkspacePoolService

Covers state management scenarios from docs/deployment/state-management.md:
- Workspace allocation and release
- Transaction rollback on concurrent allocation
- Workspace cleanup and verification
- Lease expiration
- Dirty workspace blocked from allocation
"""

import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

from cryptography.fernet import Fernet

from app.core.github_platform_secrets import (
    init_github_platform_secrets_for_tests,
    reset_github_platform_secrets,
)
from app.database.memory import InMemoryDatabase
from app.services.workspace_pool import (
    WorkspacePoolService,
    NoAvailableWorkspacesError,
    WorkspacePoolError,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.fixture
def db():
    """Create a fresh in-memory database for each test."""
    database = InMemoryDatabase()
    yield database
    database.clear()


@pytest.fixture(autouse=True)
def _platform_secrets_and_generation_sessions(db):
    """Git resolution + generation stubs for allocate_workspace_set.

    Each generation gets a key_uid that matches an api_keys doc using the
    platform-default PAT (no per-key ciphertext). This satisfies the
    Phase 2 no-legacy mandate: auth resolution requires key_uid on every
    generation that goes through git operations.
    """
    init_github_platform_secrets_for_tests(
        fernet_key=Fernet.generate_key(),
        github_token_default="unit-test-default-github-token",
        git_user_name_default="unit-test-git-user",
    )
    for eid, kuid in [
        ("est-123", "uid-est-123"),
        ("est-456", "uid-est-456"),
        ("est-789", "uid-est-789"),
    ]:
        db.set(
            COL_GENERATION_SESSIONS,
            eid,
            {
                "generation_id": eid,
                "workspace_pool": "default",
                "user_email": "unit@test.com",
                "key_uid": kuid,
            },
        )
        # Matching api_key doc — uses platform default PAT (no ciphertext)
        db.set(
            "api_keys",
            f"gain_testkey_{eid}",
            {
                "api_key": f"gain_testkey_{eid}",
                "key_uid": kuid,
                "workspace_pool": "default",
                "user_id": "unit@test.com",
                "is_active": True,
            },
        )
    yield
    reset_github_platform_secrets()


@pytest.fixture
def temp_workspace_dir():
    """Create a temporary directory for workspace operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_pool(db, temp_workspace_dir):
    """Create workspace pool service with temporary workspace directory."""
    service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
    
    # Mock git clone operations to avoid actual network calls in tests
    async def mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        """Mock repository cloning - just create the workspace directory."""
        workspace_path = service._get_workspace_path(workspace_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        # Create a mock .git directory to simulate cloned repo
        (workspace_path / ".git").mkdir(exist_ok=True)
    
    # Replace the method with our mock
    service._ensure_repo_cloned = AsyncMock(side_effect=mock_ensure_repo_cloned)
    
    return service


@pytest.fixture
def sample_workspaces(db):
    """Create sample workspaces in the database."""
    now = datetime.now(timezone.utc)
    
    # Create 2 complete sets (6 workspaces total)
    for set_num in [1, 2]:
        for i in range(1, 4):
            ws_id = f"ws-{set_num:02d}-{i}"
            db.set("workspaces", ws_id, {
                "repo_url": f"https://github.com/org/workspace-{set_num}-{i}",
                "p10y_repository_id": 74900 + (set_num * 10) + i,
                "workspace_pool": "default",
                "set_number": set_num,
                "status": "available",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
                "clean_verified": True,
                "last_used_by": None,
                "last_cleaned_at": now,
                "allocation_history": [],
                "error": None,
            })


class TestWorkspaceAllocation:
    """Test workspace allocation scenarios."""
    
    @pytest.mark.asyncio
    async def test_happy_path_allocation(self, workspace_pool, sample_workspaces):
        """Happy path: allocate workspace set successfully."""
        workspace_ids = await workspace_pool.allocate_workspace_set(
            generation_id="est-123"
        )
        
        assert len(workspace_ids) == 3
        
        # Verify all workspaces are allocated
        db = workspace_pool._db
        for ws_id in workspace_ids:
            ws = db.get("workspaces", ws_id)
            assert ws["status"] == "allocated"
            assert ws["locked_by"] == "est-123"
            assert ws["clean_verified"] is False
    
    @pytest.mark.asyncio
    async def test_no_available_workspaces(self, workspace_pool, db):
        """Error: No available workspace sets."""
        with pytest.raises(NoAvailableWorkspacesError):
            await workspace_pool.allocate_workspace_set("est-123")
    
    @pytest.mark.asyncio
    async def test_dirty_workspace_blocked(self, workspace_pool, sample_workspaces):
        """Dirty workspace (clean_verified=False) blocked from allocation."""
        db = workspace_pool._db
        
        # Mark one workspace as dirty
        db.update("workspaces", "ws-01-1", {"clean_verified": False})
        
        # Allocation should skip set 1 and use set 2
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        
        # Should get set 2, not set 1
        assert all("ws-02" in ws_id for ws_id in workspace_ids)
    
    @pytest.mark.asyncio
    async def test_partial_set_not_allocated(self, workspace_pool, sample_workspaces):
        """Partial set (only 2 available) not allocated."""
        db = workspace_pool._db

        # Mark one workspace in set 1 as allocated
        db.update("workspaces", "ws-01-1", {"status": "allocated", "locked_by": "other"})

        # Allocation should skip set 1 and use set 2
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")

        # Should get set 2, not set 1
        assert all("ws-02" in ws_id for ws_id in workspace_ids)

    @pytest.mark.asyncio
    async def test_cleaning_workspace_blocked_from_allocation(
        self, workspace_pool, sample_workspaces
    ):
        """Allocation from CLEANING raises InvalidWorkspaceStateError (state machine)."""
        db = workspace_pool._db

        # Put set 1 into CLEANING; the state machine's allocate() only allows AVAILABLE
        for i in range(1, 4):
            db.update("workspaces", f"ws-01-{i}", {"status": "cleaning"})

        # Should skip set 1 (CLEANING → InvalidWorkspaceStateError caught internally)
        # and successfully allocate set 2
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        assert all("ws-02" in ws_id for ws_id in workspace_ids)

    @pytest.mark.asyncio
    async def test_double_allocation_race_handled(
        self, workspace_pool, sample_workspaces
    ):
        """Double-allocation: second attempt on already-allocated workspace is rejected."""
        db = workspace_pool._db

        # Allocate set 1 for est-123
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")

        # Simulate concurrent process: mark all set-1 workspaces as ALLOCATED to est-xyz
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {"status": "available", "locked_by": None,
                                             "clean_verified": True})

        # Re-allocate — should succeed since workspaces are available again
        workspace_ids_2 = await workspace_pool.allocate_workspace_set("est-456")
        assert len(workspace_ids_2) == 3

        # Attempting to allocate when ALL sets are now exhausted → error
        # Mark set-2 workspaces as allocated too so nothing is available
        for i in range(1, 4):
            db.update("workspaces", f"ws-02-{i}", {"status": "allocated"})

        with pytest.raises(NoAvailableWorkspacesError):
            await workspace_pool.allocate_workspace_set("est-789")
    
    @pytest.mark.asyncio
    async def test_existing_repo_gets_git_state_reset(self, db, temp_workspace_dir):
        """
        Scenario: When workspace is allocated and repository already exists,
        the git state must be reset to ensure both local and remote main branches are clean.
        
        This prevents baseline push failures caused by stale commits from previous generations.
        """
        # Setup: Create workspace document with repo URL
        ws_id = "ws-test-reset"
        db.set("workspaces", ws_id, {
            "repo_url": "https://github.com/org/test-workspace",
            "p10y_repository_id": 12345,
            "workspace_pool": "default",
            "set_number": 99,
            "status": "available",
            "locked_by": None,
            "locked_at": None,
            "lease_expires_at": None,
            "clean_verified": True,
            "last_used_by": None,
            "last_cleaned_at": datetime.now(timezone.utc),
            "allocation_history": [],
            "error": None,
        })
        
        # Create workspace pool service
        service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
        workspace_path = service._get_workspace_path(ws_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        
        # Simulate existing repository with stale commits (workspace already exists)
        (workspace_path / ".git").mkdir(exist_ok=True)
        
        # Mock git commands to verify the reset sequence is called
        reset_called = False

        async def mock_reset(ws_path, ws_id_arg):
            nonlocal reset_called
            reset_called = True
            # Verify arguments
            assert ws_path == workspace_path
            assert ws_id_arg == ws_id
        
        service._reset_workspace_for_allocation = AsyncMock(side_effect=mock_reset)
        
        # Mock run_git for remote check to return matching URL
        async def mock_run_git(repo, args):
            if args[0] == "remote" and args[1] == "get-url":
                return "https://github.com/org/test-workspace"
            return ""
        
        with patch("app.services.workspace_pool.run_git", side_effect=mock_run_git):
            # Execute: Call _ensure_repo_cloned
            ws_doc = db.get("workspaces", ws_id)
            await service._ensure_repo_cloned(ws_id, ws_doc, "est-123")
        
        # Verify: Reset method was called
        assert reset_called, "Git reset should be called for existing repositories"


class TestWorkspaceCleanup:
    """Test workspace cleanup and verification."""
    
    @pytest.mark.asyncio
    async def test_cleanup_marks_available(self, workspace_pool, sample_workspaces, tmp_path):
        """Successful cleanup marks workspace as available."""
        # Setup: allocate, seed into cleaning state
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        for _ws in workspace_ids:
            workspace_pool._db.update("workspaces", _ws, {"status": "cleaning", "last_used_by": "est-123"})

        ws_id = workspace_ids[0]
        
        # Mock workspace path and git commands
        workspace_path = tmp_path / ws_id
        workspace_path.mkdir()
        (workspace_path / ".git").mkdir()
        
        with patch.object(workspace_pool, "_get_workspace_path", return_value=workspace_path):
            with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock):
                # Mock the new verification method to return True (archive succeeded)
                with patch.object(workspace_pool, "_verify_archive_pushed", return_value=True):
                    with patch.object(workspace_pool, "verify_workspace_clean", return_value=True):
                        await workspace_pool.cleanup_workspace(ws_id)
        
        # Verify workspace is available
        db = workspace_pool._db
        ws = db.get("workspaces", ws_id)
        assert ws["status"] == "available"
        assert ws["clean_verified"] is True
    
    @pytest.mark.asyncio
    async def test_cleanup_failure_marks_stuck(self, workspace_pool, sample_workspaces, tmp_path):
        """Cleanup failure marks workspace as stuck."""
        # Setup: allocate, seed into cleaning state
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        for _ws in workspace_ids:
            workspace_pool._db.update("workspaces", _ws, {"status": "cleaning", "last_used_by": "est-123"})

        ws_id = workspace_ids[0]
        
        # Mock workspace path and git commands to fail
        workspace_path = tmp_path / ws_id
        workspace_path.mkdir()
        
        with patch.object(workspace_pool, "_get_workspace_path", return_value=workspace_path):
            with patch("app.services.workspace_pool.run_git", side_effect=Exception("Git error")):
                with pytest.raises(WorkspacePoolError):
                    await workspace_pool.cleanup_workspace(ws_id)
        
        # Verify workspace is stuck
        db = workspace_pool._db
        ws = db.get("workspaces", ws_id)
        assert ws["status"] == "stuck"
        assert ws["error"] is not None
        assert ws.get("stuck_at") is not None
    
    @pytest.mark.asyncio
    async def test_cleanup_archive_verification_failure_marks_stuck(self, workspace_pool, sample_workspaces, tmp_path):
        """Archive verification failure prevents cleanup and marks workspace as stuck."""
        # Setup: allocate, seed into cleaning state
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        for _ws in workspace_ids:
            workspace_pool._db.update("workspaces", _ws, {"status": "cleaning", "last_used_by": "est-123"})

        ws_id = workspace_ids[0]
        
        # Mock workspace path
        workspace_path = tmp_path / ws_id
        workspace_path.mkdir()
        (workspace_path / ".git").mkdir()
        
        with patch.object(workspace_pool, "_get_workspace_path", return_value=workspace_path):
            # Archive succeeds (no exception)
            with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock):
                # But verification fails (branch not found on remote)
                with patch.object(workspace_pool, "_verify_archive_pushed", return_value=False):
                    with pytest.raises(WorkspacePoolError) as exc_info:
                        await workspace_pool.cleanup_workspace(ws_id)
        
        # Verify workspace is stuck (NOT cleaned)
        db = workspace_pool._db
        ws = db.get("workspaces", ws_id)
        assert ws["status"] == "stuck"
        assert ws["error"] is not None
        assert "not found on remote" in ws["error"]
        assert ws.get("stuck_at") is not None
        
        # Verify error message is clear
        error_msg = str(exc_info.value)
        assert "Manual intervention required" in error_msg
        assert "not found on remote" in error_msg
    
    @pytest.mark.asyncio
    async def test_cleanup_missing_directory_uses_state_machine(self, workspace_pool, sample_workspaces):
        """cleanup_workspace() with missing directory uses mark_clean() not direct DB write (Commandment VII)."""
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        for _ws in workspace_ids:
            workspace_pool._db.update("workspaces", _ws, {"status": "cleaning", "last_used_by": "est-123"})
        ws_id = workspace_ids[0]

        # Point workspace path to a non-existent directory
        missing_path = Path("/tmp/does-not-exist-workspace")
        with patch.object(workspace_pool, "_get_workspace_path", return_value=missing_path):
            await workspace_pool.cleanup_workspace(ws_id)

        ws = workspace_pool._db.get("workspaces", ws_id)
        assert ws["status"] == "available"
        assert ws["clean_verified"] is True
        assert ws.get("last_cleaned_at") is not None

    @pytest.mark.asyncio
    async def test_verify_clean_workspace(self, workspace_pool, tmp_path):
        """Verify clean workspace returns True."""
        workspace_path = tmp_path / "test-workspace"
        workspace_path.mkdir()
        (workspace_path / ".git").mkdir()
        
        with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock) as mock_git:
            # Mock git status (clean) - first call
            # Mock git branch (main) - second call
            mock_git.side_effect = [
                "",      # git status --porcelain (clean)
                "main",  # git branch --show-current
            ]

            result = await workspace_pool.verify_workspace_clean(workspace_path)

        assert result is True
        assert mock_git.call_count == 2  # Should call git status and git branch
    
    @pytest.mark.asyncio
    async def test_verify_dirty_workspace(self, workspace_pool, tmp_path):
        """Verify dirty workspace returns False."""
        workspace_path = tmp_path / "test-workspace"
        workspace_path.mkdir()
        (workspace_path / ".git").mkdir()
        
        with patch("app.services.workspace_pool.run_git", new_callable=AsyncMock) as mock_git:
            # Mock git status (dirty)
            mock_git.return_value = "M file.txt"

            result = await workspace_pool.verify_workspace_clean(workspace_path)
        
        assert result is False


class TestPoolStatus:
    """Test pool status monitoring."""
    
    @pytest.mark.asyncio
    async def test_get_pool_status(self, workspace_pool, sample_workspaces):
        """Get pool status returns correct counts."""
        status = await workspace_pool.get_pool_status()
        
        assert status["total"] == 6
        assert status["available"] == 6
        assert status["allocated"] == 0
        assert status["cleaning"] == 0
        assert status["stuck"] == 0
        assert status["available_sets"] == 2
    
    @pytest.mark.asyncio
    async def test_pool_status_after_allocation(self, workspace_pool, sample_workspaces):
        """Pool status updates after allocation."""
        await workspace_pool.allocate_workspace_set("est-123")
        
        status = await workspace_pool.get_pool_status()
        
        assert status["available"] == 3
        assert status["allocated"] == 3
        assert status["available_sets"] == 1


class TestWorkspaceCountConfig:
    """
    Tests for workspace_count parameter (configurable workspace count feature).

    Design: pool always allocates all 3 workspaces in a set as ALLOCATED.
    allocate_workspace_set(count=N) returns only N active IDs.
    The remaining (3-N) workspaces stay ALLOCATED (blocking the set) and are
    released as orphans by GenerationSessionStateMachine.complete().
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [1, 2])
    async def test_allocate_returns_only_active_workspaces(
        self, workspace_pool, sample_workspaces, count
    ):
        """
        Scenario: allocate_workspace_set(count=N) returns N IDs but allocates all 3.
        Setup: 2 full sets (6 workspaces) are available.
        Steps:
          1. Allocate with count=N for est-123.
          2. Assert only N IDs returned.
          3. Assert all 3 in the set are ALLOCATED with locked_by=est-123.
          4. Assert set 2 can still be fully allocated by a second generation.
        """
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123", count=count)
        assert len(workspace_ids) == count

        db = workspace_pool._db
        # All 3 in set 1 must be ALLOCATED (pool blocks the entire set)
        for i in range(1, 4):
            ws = db.get("workspaces", f"ws-01-{i}")
            assert ws["status"] == "allocated"
            assert ws["locked_by"] == "est-123"

        # Set 2 is still fully available — a second generation can allocate it
        workspace_ids_2 = await workspace_pool.allocate_workspace_set("est-456", count=3)
        assert len(workspace_ids_2) == 3

    @pytest.mark.asyncio
    async def test_allocate_count_none_defaults_to_three(
        self, workspace_pool, sample_workspaces
    ):
        """count=None (default) allocates a full set of 3 — backward compat."""
        workspace_ids = await workspace_pool.allocate_workspace_set("est-123")
        assert len(workspace_ids) == 3

    @pytest.mark.asyncio
    async def test_allocate_invalid_count_raises(self, workspace_pool, sample_workspaces):
        """workspace_count outside 1-3 raises ValueError before touching the DB."""
        with pytest.raises(ValueError, match="workspace_count must be 1, 2, or 3"):
            await workspace_pool.allocate_workspace_set("est-123", count=0)
        with pytest.raises(ValueError, match="workspace_count must be 1, 2, or 3"):
            await workspace_pool.allocate_workspace_set("est-123", count=4)


class TestGitCloneErrorSanitization:
    """Test that GitHub tokens are sanitized in git clone error messages."""
    
    @pytest.mark.asyncio
    async def test_git_clone_error_sanitizes_token(self, db, temp_workspace_dir):
        """Git clone errors should not expose GitHub tokens."""
        from unittest.mock import patch
        from app.services.git_utils import GitCommandError
        from app.services.github_auth import GithubAuthContext

        # Create a fresh workspace pool without mocking
        workspace_pool = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)

        token = "ghp_some_test_token" # gitleaks:allow

        with patch(
            "app.services.workspace_pool.resolve_github_auth_for_generation_id",
            return_value=GithubAuthContext(git_user_name="testuser", token=token),
        ):
            # Mock run_git to raise an error with token in message
            async def mock_run_git(workspace_path, args):
                error_msg = (
                    f"Command '['git', 'clone', 'https://testuser:{token}@github.com/"
                    f"org/repo', 'ws-01-1']' returned non-zero exit status 128."
                )
                raise GitCommandError(args, 128, stderr=error_msg)

            with patch("app.services.workspace_pool.run_git", side_effect=mock_run_git):
                # Try to clone - should fail but sanitize token
                ws_doc = {
                    "repo_url": "https://github.com/org/repo",
                    "status": "available"
                }
                with pytest.raises(WorkspacePoolError) as exc_info:
                    await workspace_pool._ensure_repo_cloned(
                        workspace_id="ws-01-1",
                        ws_doc=ws_doc,
                        generation_id="est-123",
                    )

                # Verify error message doesn't contain token
                error_message = str(exc_info.value)
                assert token not in error_message
                assert "[REDACTED]" in error_message
                assert "github.com" in error_message
    
    @pytest.mark.asyncio
    async def test_archive_error_sanitizes_token(self, workspace_pool, temp_workspace_dir):
        """Archive errors should not expose GitHub tokens."""
        from unittest.mock import patch
        from app.services.git_utils import GitCommandError
        from app.services.github_auth import GithubAuthContext

        token = "ghp_TestToken123"
        workspace_path = temp_workspace_dir / "test-workspace"
        workspace_path.mkdir()
        (workspace_path / ".git").mkdir()

        with patch(
            "app.services.workspace_pool.resolve_github_auth_for_generation_id",
            return_value=GithubAuthContext(git_user_name="u", token=token),
        ):
            # Mock run_git to raise error with token on push
            async def mock_run_git(ws_path, args):
                if "push" in args:
                    error_msg = f"Authentication failed for 'https://user:{token}@github.com/org/repo'"
                    raise GitCommandError(args, 128, stderr=error_msg)
                # For other commands, return success
                return ""

            with patch("app.services.workspace_pool.run_git", side_effect=mock_run_git):
                with pytest.raises(WorkspacePoolError) as exc_info:
                    await workspace_pool._archive_generation_session_work(
                        workspace_path=workspace_path,
                        generation_id="est-123"
                    )

                # Verify error message doesn't contain token
                error_message = str(exc_info.value)
                assert token not in error_message
                assert "[REDACTED]" in error_message


# ---------------------------------------------------------------------------
# Phase 1: allocation race rollback silent pass bug
# ---------------------------------------------------------------------------

class TestRaceRollbackErrorHandling:
    """
    Phase 1: When allocation_rollback() raises during the race-condition rollback,
    the exception must NOT be silently swallowed. Instead:
      - An error-level log must be emitted.
      - mark_stuck() must be called so the workspace is visible to operators.
      - If mark_stuck() also fails, a critical-level log is the final fallback.
    """

    @pytest.fixture
    def pool_with_mocked_sm(self, db, temp_workspace_dir, sample_workspaces):
        """Workspace pool with a fully mocked WorkspaceStateMachine."""
        service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
        service._ensure_repo_cloned = AsyncMock()
        service._workspace_sm = AsyncMock()
        service._workspace_sm.allocation_rollback = AsyncMock()
        service._workspace_sm.mark_stuck = AsyncMock()
        return service

    def _make_flaky_allocate(self, fail_on_call_n=2):
        """Returns an allocate side_effect that raises on the Nth call."""
        from app.state.exceptions import InvalidWorkspaceStateError
        call_count = [0]

        async def flaky(workspace_id, generation_id, triggered_by):
            call_count[0] += 1
            if call_count[0] == fail_on_call_n:
                raise InvalidWorkspaceStateError(
                    workspace_id=workspace_id,
                    current_status="allocated",
                    attempted_transition="allocate",
                    allowed_from=["available"],
                )
            return {"status": "allocated"}

        return flaky

    @pytest.mark.asyncio
    async def test_rollback_failure_logs_error(self, pool_with_mocked_sm, caplog):
        """allocation_rollback() raising must emit an error-level log, not be silent.

        Set 1 races (ws-01-2 taken), allocation_rollback raises for ws-01-1.
        Set 2 then succeeds, so allocate_workspace_set returns normally.
        The error must still have been logged during set 1 rollback.
        """
        import logging
        service = pool_with_mocked_sm
        # allocate fails on 2nd call (second workspace in set 1 → race)
        service._workspace_sm.allocate = AsyncMock(side_effect=self._make_flaky_allocate())
        service._workspace_sm.allocation_rollback = AsyncMock(
            side_effect=RuntimeError("DB write failed")
        )

        with caplog.at_level(logging.ERROR):
            await service.allocate_workspace_set("est-123")  # succeeds via set 2

        assert any(
            "allocation_rollback" in r.message and r.levelno >= logging.ERROR
            for r in caplog.records
        ), "Expected ERROR log when allocation_rollback fails"

    @pytest.mark.asyncio
    async def test_rollback_failure_calls_mark_stuck(self, pool_with_mocked_sm):
        """allocation_rollback() raising must call mark_stuck() to surface the leak."""
        service = pool_with_mocked_sm
        service._workspace_sm.allocate = AsyncMock(side_effect=self._make_flaky_allocate())
        service._workspace_sm.allocation_rollback = AsyncMock(
            side_effect=RuntimeError("DB write failed")
        )

        await service.allocate_workspace_set("est-123")  # succeeds via set 2

        service._workspace_sm.mark_stuck.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollback_and_mark_stuck_both_fail_logs_critical(
        self, pool_with_mocked_sm, caplog
    ):
        """If both allocation_rollback and mark_stuck fail, a CRITICAL log is the fallback."""
        import logging
        service = pool_with_mocked_sm
        service._workspace_sm.allocate = AsyncMock(side_effect=self._make_flaky_allocate())
        service._workspace_sm.allocation_rollback = AsyncMock(
            side_effect=RuntimeError("DB write failed")
        )
        service._workspace_sm.mark_stuck = AsyncMock(
            side_effect=RuntimeError("mark_stuck also failed")
        )

        with caplog.at_level(logging.CRITICAL):
            await service.allocate_workspace_set("est-123")  # succeeds via set 2

        assert any(
            r.levelno >= logging.CRITICAL for r in caplog.records
        ), "Expected CRITICAL log when both rollback and mark_stuck fail"


# ============================================================
# LV2.1 — cleaning_sets computation
# ============================================================


class TestCleaningSetsComputation:
    """Tests for the cleaning_sets additive field in get_pool_status."""

    @pytest.mark.asyncio
    async def test_cleaning_sets_empty_when_nothing_cleaning(
        self, workspace_pool, sample_workspaces
    ):
        """No CLEANING workspaces → cleaning_sets is empty list."""
        result = await workspace_pool.get_pool_status()
        assert result["cleaning_sets"] == []

    @pytest.mark.asyncio
    async def test_cleaning_sets_single_set(self, workspace_pool, db):
        """One set of 3 CLEANING workspaces → one CleaningSetInfo entry."""
        now = datetime.now(timezone.utc)
        # Put 3 workspaces from set 1 in CLEANING state, started 30 min ago
        started_at = datetime(
            now.year, now.month, now.day,
            now.hour, now.minute, now.second,
            tzinfo=timezone.utc,
        )
        import datetime as dt
        started_at = now - dt.timedelta(minutes=30)
        for i in range(1, 4):
            db.set("workspaces", f"ws-01-{i}", {
                "workspace_pool": "default",
                "set_number": 1,
                "status": "cleaning",
                "cleaning_started_at": started_at,
            })
        result = await workspace_pool.get_pool_status()
        cleaning_sets = result["cleaning_sets"]
        assert len(cleaning_sets) == 1
        entry = cleaning_sets[0]
        assert entry["set_number"] == 1
        # 2h grace - 30min elapsed = 90min remaining = 5400s (allow ±5s for runtime)
        assert 5395 <= entry["remaining_grace_seconds"] <= 5405

    @pytest.mark.asyncio
    async def test_cleaning_sets_multiple_sets(self, workspace_pool, db):
        """Two sets in CLEANING → two entries, sorted by set_number."""
        import datetime as dt
        now = datetime.now(timezone.utc)
        # Set 1 started 1h30m ago → 30min remaining = 1800s
        for i in range(1, 4):
            db.set("workspaces", f"ws-01-{i}", {
                "workspace_pool": "default",
                "set_number": 1,
                "status": "cleaning",
                "cleaning_started_at": now - dt.timedelta(hours=1, minutes=30),
            })
        # Set 2 started 26min ago → about 94min remaining = 5640s
        for i in range(1, 4):
            db.set("workspaces", f"ws-02-{i}", {
                "workspace_pool": "default",
                "set_number": 2,
                "status": "cleaning",
                "cleaning_started_at": now - dt.timedelta(minutes=26),
            })
        result = await workspace_pool.get_pool_status()
        cleaning_sets = result["cleaning_sets"]
        assert len(cleaning_sets) == 2
        assert cleaning_sets[0]["set_number"] == 1
        assert cleaning_sets[1]["set_number"] == 2
        # Set 1: 1h30m elapsed → ~30min = 1800s remaining (allow ±5s)
        assert 1795 <= cleaning_sets[0]["remaining_grace_seconds"] <= 1805
        # Set 2: 26min elapsed → ~94min = 5640s remaining (allow ±5s)
        assert 5635 <= cleaning_sets[1]["remaining_grace_seconds"] <= 5645

    @pytest.mark.asyncio
    async def test_cleaning_sets_grace_clamped_to_zero(self, workspace_pool, db):
        """CLEANING workspace past the 2h grace → remaining_grace_seconds == 0."""
        import datetime as dt
        now = datetime.now(timezone.utc)
        # Started 3 hours ago → grace long expired
        for i in range(1, 4):
            db.set("workspaces", f"ws-01-{i}", {
                "workspace_pool": "default",
                "set_number": 1,
                "status": "cleaning",
                "cleaning_started_at": now - dt.timedelta(hours=3),
            })
        result = await workspace_pool.get_pool_status()
        cleaning_sets = result["cleaning_sets"]
        assert len(cleaning_sets) == 1
        assert cleaning_sets[0]["remaining_grace_seconds"] == 0

    @pytest.mark.asyncio
    async def test_cleaning_sets_none_cleaning_started_at(self, workspace_pool, db):
        """CLEANING workspace with missing cleaning_started_at → remaining = 0."""
        db.set("workspaces", "ws-01-1", {
            "workspace_pool": "default",
            "set_number": 1,
            "status": "cleaning",
            "cleaning_started_at": None,
        })
        result = await workspace_pool.get_pool_status()
        cleaning_sets = result["cleaning_sets"]
        assert len(cleaning_sets) == 1
        assert cleaning_sets[0]["remaining_grace_seconds"] == 0

    @pytest.mark.asyncio
    async def test_cleaning_sets_uses_minimum_per_set(self, workspace_pool, db):
        """Within one set, the member with the oldest cleaning_started_at governs."""
        import datetime as dt
        now = datetime.now(timezone.utc)
        # ws-01-1: started 1h ago → 3600s remaining
        db.set("workspaces", "ws-01-1", {
            "workspace_pool": "default",
            "set_number": 1,
            "status": "cleaning",
            "cleaning_started_at": now - dt.timedelta(hours=1),
        })
        # ws-01-2: started 90min ago → 1800s remaining (older → governs)
        db.set("workspaces", "ws-01-2", {
            "workspace_pool": "default",
            "set_number": 1,
            "status": "cleaning",
            "cleaning_started_at": now - dt.timedelta(hours=1, minutes=30),
        })
        # ws-01-3: started 10min ago → 6600s remaining
        db.set("workspaces", "ws-01-3", {
            "workspace_pool": "default",
            "set_number": 1,
            "status": "cleaning",
            "cleaning_started_at": now - dt.timedelta(minutes=10),
        })
        result = await workspace_pool.get_pool_status()
        cleaning_sets = result["cleaning_sets"]
        assert len(cleaning_sets) == 1
        # Governed by ws-01-2 (oldest): ~1800s remaining
        assert 1795 <= cleaning_sets[0]["remaining_grace_seconds"] <= 1805

    @pytest.mark.asyncio
    async def test_existing_pool_status_fields_unchanged(
        self, workspace_pool, sample_workspaces
    ):
        """Existing fields in get_pool_status are not altered by the new cleaning_sets field."""
        result = await workspace_pool.get_pool_status()
        assert result["total"] == 6
        assert result["available"] == 6
        assert result["allocated"] == 0
        assert result["cleaning"] == 0
        assert result["stuck"] == 0
        assert result["available_sets"] == 2
        assert "cleaning_sets" in result


# ============================================================
# LV2.3 — on-demand cleanup_workspace (CLEANING → AVAILABLE)
# ============================================================


class TestOnDemandClearWorkspace:
    """Tests for the on-demand clear path via cleanup_workspace."""

    @pytest.mark.asyncio
    async def test_cleanup_workspace_cleaning_to_available(
        self, workspace_pool, db, temp_workspace_dir
    ):
        """
        A CLEANING workspace with no directory is marked AVAILABLE via mark_clean.
        (No filesystem/git ops needed — workspace_path does not exist.)
        """
        import datetime as dt
        now = datetime.now(timezone.utc)
        db.set("workspaces", "ws-01-1", {
            "repo_url": "https://github.com/org/ws-01-1",
            "workspace_pool": "default",
            "set_number": 1,
            "status": "cleaning",
            "locked_by": None,
            "last_used_by": None,
            "cleaning_started_at": now - dt.timedelta(minutes=10),
        })
        await workspace_pool.cleanup_workspace("ws-01-1")
        ws = db.get("workspaces", "ws-01-1")
        assert ws["status"] == "available"

    @pytest.mark.asyncio
    async def test_cleanup_workspace_not_found_raises(self, workspace_pool):
        """Attempting to clear a non-existent workspace raises WorkspaceNotFoundError."""
        from app.services.workspace_pool import WorkspaceNotFoundError
        with pytest.raises(WorkspaceNotFoundError):
            await workspace_pool.cleanup_workspace("ws-nonexistent")

    @pytest.mark.asyncio
    async def test_cleanup_workspace_wrong_state_raises(self, workspace_pool, db):
        """Attempting to clear an AVAILABLE workspace raises WorkspacePoolError."""
        from app.services.workspace_pool import WorkspacePoolError
        db.set("workspaces", "ws-01-1", {
            "workspace_pool": "default",
            "set_number": 1,
            "status": "available",
        })
        with pytest.raises(WorkspacePoolError, match="expected 'cleaning'"):
            await workspace_pool.cleanup_workspace("ws-01-1")
