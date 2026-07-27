"""
Workspace Pool Service

Manages allocation, release, and cleanup of workspace sets for generation sessions.
Uses Firestore transactions for distributed locking to prevent concurrent allocation conflicts.

Key Features:
- Atomic workspace set allocation (3 repos per session)
- Distributed locking via Firestore transactions
- Workspace cleanup and verification
- Lease-based auto-expiry for crash recovery
- Pool status monitoring
"""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional

from app.utils.workspace_gitignore import ensure_workspace_gitignore
from app.services.git_utils import GitCommandError, run_git

from app.core.config import WORKSPACE_DEFAULT_BRANCH, GIT_COMMITTER_USER_NAME, GIT_COMMITTER_USER_EMAIL
from app.core.ttl_config import GenerationLifecyclePolicy
from app.schemas.generation_workflow_enums import WorkspaceStatus
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.services.github_auth import (
    GithubAuthContext,
    GithubAuthResolutionError,
    resolve_github_auth_for_generation_id,
)
from app.services.git_provider import all_strategies
from app.database.interface import IDatabase
from app.state import WorkspaceStateMachine
from app.state.db_adapter import StateMachineDBAdapter
from app.state.exceptions import InvalidWorkspaceStateError as SMInvalidWorkspaceStateError
from app.state.transitions import TriggeredBy

logger = logging.getLogger(__name__)


class WorkspacePoolError(Exception):
    """Base exception for workspace pool errors."""
    pass


class NoAvailableWorkspacesError(WorkspacePoolError):
    """Raised when no workspace sets are available for allocation."""
    pass


class WorkspaceNotFoundError(WorkspacePoolError):
    """Raised when workspace doesn't exist in pool."""
    pass


class WorkspaceVerificationError(WorkspacePoolError):
    """Raised when workspace cleanup verification fails."""
    pass


async def _repair_orphan_main_keep_workdir(workspace_path: Path) -> None:
    """
    Recreate orphan main without git rm / git clean (preserves extracted user files).

    Used when refs/heads/main or HEAD reference OIDs not in .git/objects (e.g. user tar
    overwrote .git/refs with another clone's commits).
    Matches the safe prefix of _reset_git_repo_local only (no index wipe, no clean).
    """
    ref_name = f"refs/heads/{WORKSPACE_DEFAULT_BRANCH}"
    try:
        await run_git(workspace_path, ["update-ref", "-d", ref_name])
    except GitCommandError:
        pass
    loose = workspace_path / ".git" / "refs" / "heads" / WORKSPACE_DEFAULT_BRANCH
    try:
        loose.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        try:
            current_branch = await run_git(
                workspace_path,
                ["symbolic-ref", "--short", "HEAD"],
            )
        except GitCommandError:
            current_branch = None

        if current_branch is None or current_branch == WORKSPACE_DEFAULT_BRANCH:
            try:
                await run_git(workspace_path, ["checkout", "--detach"])
            except GitCommandError:
                pass

        try:
            await run_git(workspace_path, ["branch", "-D", WORKSPACE_DEFAULT_BRANCH])
        except GitCommandError:
            pass

        await run_git(
            workspace_path,
            ["checkout", "--orphan", WORKSPACE_DEFAULT_BRANCH],
        )
    except GitCommandError:
        raise


async def _maybe_repair_git_refs_after_user_extract(workspace_path: Path) -> None:
    """
    If HEAD references a missing commit (common after extracting another repo's .git refs),
    reset to an orphan main branch while keeping files on disk for SKIP_initial_user_source.
    """
    try:
        sha = await run_git(workspace_path, ["rev-parse", "-q", "--verify", "HEAD"])
    except GitCommandError:
        # Unborn branch (no commits yet) — normal before first commit.
        return

    corrupt = False
    try:
        if (await run_git(workspace_path, ["cat-file", "-t", sha])) != "commit":
            corrupt = True
        else:
            await run_git(workspace_path, ["cat-file", "-e", sha])
    except GitCommandError:
        corrupt = True

    if not corrupt:
        return

    logger.warning(
        "Workspace %s: git HEAD/commit objects missing or inconsistent after extract — "
        "repairing refs (orphan main, working tree preserved)",
        workspace_path,
    )
    await _repair_orphan_main_keep_workdir(workspace_path)


class WorkspacePoolService:
    """
    Service for managing the workspace pool.
    
    Handles:
    - Allocating workspace sets (3 repos per session)
    - Releasing workspaces after use
    - Cleaning workspaces (git reset)
    - Verifying workspaces are clean
    - Pool status monitoring
    """
    
    WORKSPACES_PER_SET = 3
    
    @staticmethod
    def _sanitize_token_in_message(message: str, token: Optional[str] = None) -> str:
        """
        Sanitize GitHub tokens from error messages and command outputs.
        
        This removes sensitive authentication tokens that may appear in git clone
        URLs or error messages.
        
        Args:
            message: The message to sanitize (error message, command output, etc.)
            token: Optional specific token to sanitize. If not provided, will attempt
                   to find and sanitize any tokens matching common patterns.
        
        Returns:
            Sanitized message with tokens replaced by "[REDACTED]"
        """
        if not message:
            return message
        
        sanitized = message

        # If specific token provided, replace it
        if token:
            sanitized = sanitized.replace(token, "[REDACTED]")

        # Sanitize known token/URL patterns for every provider, not just the active one —
        # cheap, and defends even if a token from another provider leaks into these logs.
        for strategy in all_strategies():
            for pattern, replacement in strategy.sanitization_patterns:
                sanitized = pattern.sub(replacement, sanitized)

        return sanitized
    
    def __init__(self, db: IDatabase, workspace_base_path: Optional[str | Path] = None):
        """
        Initialize workspace pool service.
        
        Args:
            db: Database interface (Firestore, Emulator, or InMemory)
            workspace_base_path: Optional base path for workspaces. If not provided,
                uses WORKSPACE_BASE_PATH env var or defaults to /workspaces
        """
        self._db = db
        if workspace_base_path is not None:
            self.workspace_base_path = Path(workspace_base_path)
        else:
            # Use WORKSPACE_BASE_PATH env var, or default to /workspaces
            # Note: WORKSPACE_DIR (settings) is intentionally not used here as it's for
            # single-workspace workflows, not the pool base path
            base = os.getenv("WORKSPACE_BASE_PATH", "/workspaces")
            self.workspace_base_path = Path(base)
        # State machine and adapter — wired in Phase 2
        self._db_adapter = StateMachineDBAdapter(db)
        self._workspace_sm = WorkspaceStateMachine(self._db_adapter)

    def get_workspace(self, workspace_id: str):
        """Return the raw workspace document, or None if not found."""
        return self._db.get("workspaces", workspace_id)

    async def allocate_workspace_set(
        self,
        generation_id: str,
        count: int | None = None,
        workspace_pool: str | None = None,
    ) -> List[str]:
        """
        Atomically allocate a workspace set (3 repos) for a generation session.

        Always allocates all 3 workspaces in a set as ALLOCATED. Returns only
        the first `count` workspace IDs as "active"; the remaining ones stay
        ALLOCATED (blocking the set) and are released as orphans via
        GenerationSessionStateMachine.complete() once the active workspaces are done.

        Uses Firestore transaction to prevent concurrent allocation conflicts.
        Only allocates workspaces that are:
        - Status: "available"
        - clean_verified: True
        - Same set_number (grouped set of 3)

        After allocation, ensures repositories are cloned to the NFS volume.
        If a repository doesn't exist or is invalid, it will be cloned from the
        repo_url stored in the workspace document.

        Args:
            generation_id: The generation session requesting workspaces
            count: Number of active workspaces to return (1, 2, or 3).
                   None defaults to WORKSPACES_PER_SET (3). All 3 are still
                   ALLOCATED regardless of count; only count IDs are returned.

        Returns:
            List of `count` workspace IDs (active). All 3 are ALLOCATED.

        Raises:
            ValueError: count is not 1, 2, or 3
            NoAvailableWorkspacesError: No available workspace sets
            WorkspacePoolError: Other allocation errors (including repository clone failures)

        Example:
            >>> service = WorkspacePoolService(db)
            >>> workspace_ids = await service.allocate_workspace_set("est-123")
            >>> # Returns: ["ws-01-1", "ws-01-2", "ws-01-3"]
            >>> # Repositories are now cloned and ready to use
        """
        effective_count = count if count is not None else self.WORKSPACES_PER_SET
        if effective_count not in (1, 2, 3):
            raise ValueError(
                f"workspace_count must be 1, 2, or 3, got {effective_count}"
            )

        est_doc = await self._db_adapter.get_generation_session(generation_id)
        if not est_doc:
            raise WorkspacePoolError(f"Generation {generation_id} not found for workspace allocation")
        pool = workspace_pool if workspace_pool is not None else (
            est_doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
        )

        # Find available workspace sets
        for set_num in range(1, 101):
            workspaces = await self._db_adapter.query_workspaces([
                ("workspace_pool", "==", pool),
                ("set_number", "==", set_num),
                ("status", "==", "available"),
                ("clean_verified", "==", True),
            ])

            if len(workspaces) != self.WORKSPACES_PER_SET:
                continue

            # --- Sub-step 2.2a: allocate via WorkspaceStateMachine ---
            # Each workspace_sm.allocate() runs its own transaction to validate
            # AVAILABLE → ALLOCATED. If a workspace was taken by a race, the state
            # machine raises InvalidWorkspaceStateError; we roll back and try the
            # next set.
            allocated_ids: List[str] = []
            try:
                for ws in workspaces:
                    ws_id = ws["_id"]
                    await self._workspace_sm.allocate(
                        workspace_id=ws_id,
                        generation_id=generation_id,
                        triggered_by=TriggeredBy.START,
                    )
                    allocated_ids.append(ws_id)
            except SMInvalidWorkspaceStateError:
                # Race condition: a workspace was taken by another process.
                # Rollback any workspaces we already allocated via the state machine.
                # allocation_rollback → CLEANING because no clone has been attempted yet,
                # but cleanup_workspace will verify quickly (empty dir → mark AVAILABLE).
                for ws_id in allocated_ids:
                    try:
                        await self._workspace_sm.allocation_rollback(
                            workspace_id=ws_id,
                            generation_id=generation_id,
                            triggered_by=TriggeredBy.START,
                        )
                    except Exception:
                        logger.error(
                            "allocation_rollback failed for workspace %s", ws_id, exc_info=True
                        )
                        try:
                            await self._workspace_sm.mark_stuck(
                                workspace_id=ws_id,
                                triggered_by=TriggeredBy.START,
                                reason="allocation_rollback_failed",
                            )
                        except Exception:
                            logger.critical(
                                "mark_stuck also failed for workspace %s — "
                                "workspace is invisibly ALLOCATED",
                                ws_id, exc_info=True,
                            )
                continue  # try next set

            all_workspace_ids = allocated_ids
            logger.info(
                f"Allocated workspace set {set_num} to generation session {generation_id}: "
                f"{all_workspace_ids}"
            )

            # After successful allocation, ensure repositories are cloned.
            # Only active workspaces need repos; blocked ones are released as orphans in complete().
            # Clone failures → sub-step 2.2b rollback (ALLOCATED → CLEANING).
            active_ids = all_workspace_ids[:effective_count]
            try:
                for ws_id in active_ids:
                    ws_doc = await self._db_adapter.get_workspace(ws_id)
                    if ws_doc:
                        await self._ensure_repo_cloned(ws_id, ws_doc, generation_id)
            except Exception as clone_error:
                # Sub-step 2.2b: allocation_rollback → ALLOCATED → CLEANING
                # (workspace enters cleanup pipeline rather than going directly to AVAILABLE)
                logger.error(
                    f"Repository clone failed during allocation, rolling back: {clone_error}",
                    exc_info=True,
                )
                for ws_id in all_workspace_ids:
                    try:
                        await self._workspace_sm.allocation_rollback(
                            workspace_id=ws_id,
                            generation_id=generation_id,
                            triggered_by=TriggeredBy.START,
                        )
                        logger.info(
                            f"Rolled back workspace {ws_id} → CLEANING after clone failure"
                        )
                    except Exception as rollback_error:
                        logger.error(
                            f"Failed to rollback workspace {ws_id}: {rollback_error}",
                            exc_info=True,
                        )
                raise WorkspacePoolError(
                    f"Failed to clone repositories during allocation. "
                    f"Workspaces have been rolled back. Error: {clone_error}"
                ) from clone_error

            if effective_count < self.WORKSPACES_PER_SET:
                logger.info(
                    f"Workspace count={effective_count} for {generation_id}: "
                    f"active={active_ids}, blocked={all_workspace_ids[effective_count:]}"
                )
            return active_ids

        raise NoAvailableWorkspacesError(
            f"No available workspace sets found for generation session {generation_id} "
            f"in workspace_pool={pool!r}"
        )
    
    async def cleanup_workspace(self, workspace_id: str) -> None:
        """
        Clean a workspace and mark it available.
        
        Steps:
        1. Verify workspace is in 'cleaning' state
        2. Archive session work (create branch, commit, push)
        3. Verify archive was pushed successfully
        4. Reset main branch to clean state (no commits)
        5. Verify cleanup succeeded
        6. Mark as 'available' with clean_verified=True
        
        If cleanup fails, workspace is marked as 'stuck' and requires manual intervention.
        
        Args:
            workspace_id: The workspace to clean
            
        Raises:
            WorkspaceNotFoundError: Workspace doesn't exist
            WorkspaceVerificationError: Cleanup verification failed
            
        Example:
            >>> await service.cleanup_workspace("ws-01-1")
        """
        # Get workspace document
        ws_doc = await self._db_adapter.get_workspace(workspace_id)

        if not ws_doc:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        if ws_doc["status"] != WorkspaceStatus.CLEANING:
            raise WorkspacePoolError(
                f"Workspace {workspace_id} is in '{ws_doc['status']}' state, "
                f"expected 'cleaning'"
            )
        
        workspace_path = self._get_workspace_path(workspace_id)
        generation_id = ws_doc.get("last_used_by")
        
        # CRITICAL INVARIANT: If workspace is in CLEANING state, it means the system has
        # ALREADY decided it's safe to wipe (via archive_and_release, allocation_rollback,
        # execute_scheduled_wipe, or force_release). We MUST proceed with cleanup.
        #
        # STEEL COMMANDMENT II: fail() and stuck_detected() NEVER release workspaces.
        # Workspaces that need preservation stay in ALLOCATED state.
        # Once they reach CLEANING, cleanup must proceed unconditionally.
        #
        # The previous logic that checked PENDING/FAILED status and blocked cleanup
        # was fundamentally incorrect - it caused workspaces to remain stuck in CLEANING
        # forever, draining the pool. The decision to preserve or wipe must be made
        # BEFORE transitioning to CLEANING (in the state machine), not here.
        
        # If workspace doesn't exist, there's nothing to clean
        # This can happen if workspace was never created or was deleted by admin
        if not workspace_path.exists():
            logger.warning(
                f"Workspace {workspace_id} directory does not exist at {workspace_path}. "
                f"Since there's nothing to archive or clean, marking workspace as available."
            )
            await self._workspace_sm.mark_clean(
                workspace_id=workspace_id,
                triggered_by=TriggeredBy.STUCK_CLEANING,
            )
            now = datetime.now(timezone.utc)
            await self._db_adapter.update_workspace(workspace_id, {
                "last_cleaned_at": now,
                "error": None,
            })
            logger.info(
                f"Workspace {workspace_id} marked as available (no directory to clean)"
            )
            return
        
        try:
            # 1. Archive session work (if generation_id is available)
            # CRITICAL: If archive fails, we must NOT continue with cleanup
            # to prevent data loss. Workspace will be marked as stuck.
            if generation_id:
                await self._archive_generation_session_work(workspace_path, generation_id)
                
                # 2. Verify archive was successfully pushed to remote
                # This ensures the branch actually exists on GitHub before we destroy local data
                if not await self._verify_archive_pushed(workspace_path, generation_id):
                    error_msg = (
                        f"Archive branch '{generation_id}' not found on remote after push. "
                        f"This indicates a push failure. Data preservation failed."
                    )
                    logger.error(
                        f"Archive verification failed for workspace {workspace_id}: {error_msg}"
                    )
                    raise WorkspacePoolError(error_msg)
                
                logger.info(
                    f"Archive verified: branch '{generation_id}' exists on remote "
                    f"for workspace {workspace_id}"
                )
            
            # 3. Reset git repository to clean state (main branch with no commits)
            # Only reached if archive succeeded and was verified
            await self._reset_git_repo(workspace_path, ws_doc["repo_url"], generation_id)
            
            # 4. Verify cleanup succeeded
            is_clean = await self.verify_workspace_clean(workspace_path)
            
            if not is_clean:
                raise WorkspaceVerificationError(
                    f"Workspace {workspace_id} verification failed after cleanup"
                )
            
            # 5. Sub-step 2.2c: mark_clean via WorkspaceStateMachine (CLEANING → AVAILABLE)
            now = datetime.now(timezone.utc)
            await self._workspace_sm.mark_clean(
                workspace_id=workspace_id,
                triggered_by=TriggeredBy.STUCK_CLEANING,
            )
            # Write additional fields not managed by the state machine
            await self._db_adapter.update_workspace(workspace_id, {
                "last_cleaned_at": now,
                "error": None,
            })
            logger.info(f"Cleaned and verified workspace {workspace_id}")

        except Exception as e:
            # Cleanup failed — mark as stuck via WorkspaceStateMachine (CLEANING → STUCK)
            logger.error(
                f"Workspace {workspace_id} cleanup failed: {e}",
                exc_info=True
            )

            try:
                await self._workspace_sm.mark_stuck(
                    workspace_id=workspace_id,
                    triggered_by=TriggeredBy.STUCK_CLEANING,
                    reason=str(e),
                )
                # Write additional fields not managed by the state machine
                now = datetime.now(timezone.utc)
                await self._db_adapter.update_workspace(workspace_id, {
                    "error": str(e),
                    "stuck_at": now,
                })
            except Exception as mark_stuck_err:
                logger.error(
                    f"Failed to mark workspace {workspace_id} as stuck: {mark_stuck_err}. "
                    f"Workspace may remain in CLEANING state; "
                    f"stuck_cleaning_recovery will handle it.",
                    exc_info=True,
                )

            # Send Slack alert for stuck workspace
            self._send_stuck_workspace_alert(workspace_id, generation_id, str(e))

            raise WorkspacePoolError(
                f"Workspace {workspace_id} cleanup failed and marked as stuck. "
                f"Manual intervention required to recover data before cleanup. Error: {e}"
            ) from e
    
    async def verify_workspace_clean(self, workspace_path: Path) -> bool:
        """
        Verify workspace is clean and safe to use.
        
        Checks:
        - Git status is clean (no uncommitted changes)
        - On correct branch (main)
        - No P10Y artifact markers present (``.specflow_estimation``, ``estimation_*.json``)
        
        Args:
            workspace_path: Path to workspace directory
            
        Returns:
            True if workspace is clean, False otherwise
        """
        if not workspace_path.exists():
            logger.warning(f"Workspace path {workspace_path} does not exist")
            return False
        
        try:
            # Check git status is clean
            result = await run_git(
                workspace_path,
                ["status", "--porcelain"]
            )

            if result:
                logger.warning(
                    f"Workspace {workspace_path} has uncommitted changes:\n"
                    f"{result}"
                )
                return False

            # Check on correct branch
            result = await run_git(
                workspace_path,
                ["branch", "--show-current"]
            )

            current_branch = result
            if current_branch != WORKSPACE_DEFAULT_BRANCH:
                logger.warning(
                    f"Workspace {workspace_path} is on branch '{current_branch}', "
                    f"expected '{WORKSPACE_DEFAULT_BRANCH}'"
                )
                return False
            
            # Check no P10Y artifact markers on disk
            artifacts = [
                ".specflow_estimation",
                ".specflow_session",
                "estimation_*.json"
            ]

            for pattern in artifacts:
                matching_files = list(workspace_path.glob(pattern))
                if matching_files:
                    logger.warning(
                        f"Workspace {workspace_path} has P10Y artifact markers: "
                        f"{matching_files}"
                    )
                    return False

            # Belt-and-suspenders filesystem check: the working directory must be empty
            # (only .git is allowed). git clean -ffdx can still miss files in edge cases
            # (e.g. NFS permission errors) and git status --porcelain does not always
            # report nested git repositories as untracked.
            non_git_entries = [p for p in workspace_path.iterdir() if p.name != ".git"]
            if non_git_entries:
                logger.warning(
                    f"Workspace {workspace_path} has unexpected filesystem entries "
                    f"after git clean (not caught by git status): "
                    f"{[str(p) for p in non_git_entries[:10]]}"
                )
                return False

            return True
        
        except Exception as e:
            logger.error(
                f"Workspace verification error: {e}",
                exc_info=True
            )
            return False
    
    async def check_workspace_disk_state(self, workspace_id: str) -> Dict[str, Any]:
        """
        Check the disk and git state of a workspace.
        
        This checks the actual filesystem/git state, not just Firestore state.
        Useful for detecting workspaces that are marked "available" but have stale data.
        
        Args:
            workspace_id: Workspace ID to check
            
        Returns:
            Dictionary with check results:
            - workspace_id: Workspace ID
            - status: Firestore status
            - directory_exists: Whether workspace directory exists
            - git_repo_exists: Whether .git directory exists
            - git_status: Git status check result
            - current_branch: Current git branch (or None)
            - has_uncommitted_changes: Whether there are uncommitted changes
            - has_commits_on_main: Whether main branch has commits
            - has_estimation_artifacts: Whether P10Y marker files exist (API field name unchanged)
            - is_clean: Overall clean status
            - issues: List of issues found
            - error: Error message if check failed
        """
        result = {
            "workspace_id": workspace_id,
            "status": None,
            "directory_exists": False,
            "git_repo_exists": False,
            "git_status": None,
            "current_branch": None,
            "has_uncommitted_changes": False,
            "has_commits_on_main": False,
            "has_estimation_artifacts": False,
            "is_clean": False,
            "issues": [],
            "error": None,
        }
        
        try:
            # Get workspace document
            ws_doc = await self._db_adapter.get_workspace(workspace_id)
            if not ws_doc:
                result["error"] = f"Workspace {workspace_id} not found in database"
                return result
            
            result["status"] = ws_doc.get("status")
            
            workspace_path = self._get_workspace_path(workspace_id)
            
            # Check if directory exists
            if workspace_path.exists():
                result["directory_exists"] = True
                
                # Check if git repo exists
                git_dir = workspace_path / ".git"
                if git_dir.exists() and git_dir.is_dir():
                    result["git_repo_exists"] = True
                    
                    try:
                        # Check git status
                        status_result = await run_git(
                            workspace_path,
                            ["status", "--porcelain"]
                        )
                        if status_result:
                            result["has_uncommitted_changes"] = True
                            result["issues"].append("Has uncommitted changes")
                            result["git_status"] = status_result

                        # Check current branch
                        branch_result = await run_git(
                            workspace_path,
                            ["branch", "--show-current"]
                        )
                        current_branch = branch_result
                        result["current_branch"] = current_branch if current_branch else None

                        # Check if main branch has commits
                        if current_branch == WORKSPACE_DEFAULT_BRANCH:
                            try:
                                log_result = await run_git(
                                    workspace_path,
                                    ["log", "--oneline", WORKSPACE_DEFAULT_BRANCH, "-1"]
                                )
                                if log_result:
                                    result["has_commits_on_main"] = True
                                    result["issues"].append("Main branch has commits")
                            except GitCommandError:
                                # No commits on main - that's fine
                                pass
                        else:
                            result["issues"].append(f"Not on main branch (on {current_branch})")

                    except GitCommandError as e:
                        result["error"] = f"Git command failed: {e.stderr or str(e)}"
                        result["issues"].append("Git repository appears corrupted")
                else:
                    result["issues"].append("Directory exists but is not a git repository")
                
                # Check for P10Y artifact markers
                artifacts = [
                    ".specflow_estimation",
                    ".specflow_session",
                    "estimation_*.json"
                ]
                
                for pattern in artifacts:
                    matching_files = list(workspace_path.glob(pattern))
                    if matching_files:
                        result["has_estimation_artifacts"] = True
                        result["issues"].append(f"Has P10Y artifact markers: {[str(f.name) for f in matching_files]}")
                        break
            else:
                result["issues"].append("Workspace directory does not exist")
            
            # Determine overall clean status
            if not result["issues"] and not result["error"]:
                result["is_clean"] = True
            elif result["status"] == "available" and (result["has_uncommitted_changes"] or result["has_commits_on_main"] or result["has_estimation_artifacts"]):
                result["issues"].append("Marked as available but has stale data")
        
        except Exception as e:
            result["error"] = str(e)
            logger.error(
                f"Error checking workspace {workspace_id} disk state: {e}",
                exc_info=True
            )
        
        return result
    
    async def check_all_available_workspaces(self) -> List[Dict[str, Any]]:
        """
        Check disk/git state of all available workspaces.
        
        Returns:
            List of check results for each available workspace
        """
        # Get all available workspaces
        available_workspaces = await self._db_adapter.query_workspaces(
            [("status", "==", "available")]
        )
        
        results = []
        for ws in available_workspaces:
            ws_id = ws["_id"]
            check_result = await self.check_workspace_disk_state(ws_id)
            results.append(check_result)
        
        return results
    
    async def force_clean_available_workspace(
        self,
        workspace_id: str,
        reason: str = "manual_cleanup"
    ) -> Dict[str, Any]:
        """
        Force clean an available workspace that has stale data.
        
        This is an admin operation to clean workspaces that are marked "available"
        but have stale files/commits on disk.
        
        Process:
        1. Verify workspace is "available" (safety check)
        2. Set status to "cleaning"
        3. Run cleanup (archive + reset + verify)
        4. Set back to "available" with clean_verified=True
        
        Args:
            workspace_id: Workspace ID to clean
            reason: Reason for cleaning (for audit trail)
            
        Returns:
            Dictionary with cleanup result:
            - workspace_id: Workspace ID
            - success: Whether cleanup succeeded
            - message: Status message
            - error: Error message if failed
            
        Raises:
            WorkspacePoolError: If workspace is not available or cleanup fails
        """
        # Get workspace document
        ws_doc = await self._db_adapter.get_workspace(workspace_id)
        if not ws_doc:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        if ws_doc["status"] != WorkspaceStatus.AVAILABLE:
            raise WorkspacePoolError(
                f"Workspace {workspace_id} is in '{ws_doc['status']}' state, "
                f"expected 'available'. Use appropriate cleanup method for this state."
            )
        
        workspace_path = self._get_workspace_path(workspace_id)
        
        # If workspace doesn't exist, just mark as clean
        if not workspace_path.exists():
            logger.info(
                f"Workspace {workspace_id} directory does not exist. "
                f"Marking as clean."
            )
            now = datetime.now(timezone.utc)
            await self._db_adapter.update_workspace(workspace_id, {
                "clean_verified": True,
                "last_cleaned_at": now,
            })
            return {
                "workspace_id": workspace_id,
                "success": True,
                "message": "Workspace directory does not exist - marked as clean",
                "error": None,
            }
        
        try:
            # Transition to CLEANING via state machine (AVAILABLE → CLEANING)
            await self._workspace_sm.admin_clean_available(
                workspace_id=workspace_id,
                reason=reason,
                triggered_by=TriggeredBy.ADMIN_CLEAN,
            )

            # Run cleanup (this will archive, reset, and verify)
            await self.cleanup_workspace(workspace_id)
            
            return {
                "workspace_id": workspace_id,
                "success": True,
                "message": "Workspace cleaned successfully",
                "error": None,
            }
        
        except Exception as e:
            # Cleanup failed - workspace is now "stuck"
            error_msg = str(e)
            logger.error(
                f"Failed to clean workspace {workspace_id}: {error_msg}",
                exc_info=True
            )
            
            return {
                "workspace_id": workspace_id,
                "success": False,
                "message": "Cleanup failed - workspace marked as stuck",
                "error": error_msg,
            }
    
    async def get_pool_status(self) -> Dict[str, Any]:
        """
        Get current workspace pool status.
        
        Returns:
            Dictionary with pool statistics:
            - total: Total workspaces in pool
            - available: Available workspace count
            - allocated: Allocated workspace count
            - cleaning: Cleaning workspace count
            - stuck: Stuck workspace count
            - available_sets: Number of complete available sets
            
        Example:
            >>> status = await service.get_pool_status()
            >>> print(f"Available sets: {status['available_sets']}")
        """
        all_workspaces = await self._db_adapter.query_workspaces([])
        
        # Count by status. Normalise the raw DB string into a WorkspaceStatus so
        # all comparisons in this method are enum-based; unrecognised values are
        # ignored (same as the previous string-membership check).
        status_counts: Dict[WorkspaceStatus, int] = {
            WorkspaceStatus.AVAILABLE: 0,
            WorkspaceStatus.ALLOCATED: 0,
            WorkspaceStatus.CLEANING: 0,
            WorkspaceStatus.STUCK: 0,
        }

        for ws in all_workspaces:
            try:
                status = WorkspaceStatus(ws.get("status"))
            except ValueError:
                continue
            if status in status_counts:
                status_counts[status] += 1
        
        # Count available sets per (workspace_pool, set_number) so mixed pools do not merge.
        available_sets = 0
        available_workspaces_by_set: Dict[tuple[str, int], int] = {}

        for ws in all_workspaces:
            if ws.get("status") == WorkspaceStatus.AVAILABLE and ws.get("clean_verified") is True:
                set_num = ws.get("set_number")
                pool = ws.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
                if set_num:
                    key = (pool, set_num)
                    available_workspaces_by_set[key] = available_workspaces_by_set.get(key, 0) + 1

        for _, count in available_workspaces_by_set.items():
            if count == self.WORKSPACES_PER_SET:
                available_sets += 1

        # Compute per-set grace countdown for workspaces currently in CLEANING state.
        # Group by set_number; for each set use the minimum remaining_grace_seconds across
        # all members (i.e. the workspace that has been cleaning longest determines the set).
        grace_seconds_total = GenerationLifecyclePolicy.STUCK_CLEANING_HOURS * 3600
        now = datetime.now(timezone.utc)
        # Map set_number → minimum remaining_grace_seconds seen so far
        cleaning_set_grace: Dict[int, int] = {}
        for ws in all_workspaces:
            if ws.get("status") != WorkspaceStatus.CLEANING:
                continue
            set_num = ws.get("set_number")
            if set_num is None:
                continue
            cleaning_started_at = ws.get("cleaning_started_at")
            if cleaning_started_at is None:
                remaining = 0
            else:
                # Ensure timezone-aware comparison
                if cleaning_started_at.tzinfo is None:
                    cleaning_started_at = cleaning_started_at.replace(tzinfo=timezone.utc)
                elapsed = int((now - cleaning_started_at).total_seconds())
                remaining = max(0, grace_seconds_total - elapsed)
            current = cleaning_set_grace.get(set_num)
            if current is None or remaining < current:
                cleaning_set_grace[set_num] = remaining

        cleaning_sets = [
            {"set_number": sn, "remaining_grace_seconds": rem}
            for sn, rem in sorted(cleaning_set_grace.items())
        ]

        return {
            "total": len(all_workspaces),
            "available": status_counts[WorkspaceStatus.AVAILABLE],
            "allocated": status_counts[WorkspaceStatus.ALLOCATED],
            "cleaning": status_counts[WorkspaceStatus.CLEANING],
            "stuck": status_counts[WorkspaceStatus.STUCK],
            "available_sets": available_sets,
            "cleaning_sets": cleaning_sets,
        }
    
    async def force_deallocate_workspace(
        self,
        workspace_id: str,
        reason: str = "manual_deallocation"
    ) -> None:
        """
        Force deallocate a workspace that's stuck in allocated state.
        
        This is a manual recovery operation for workspaces that got stuck
        allocated due to failures (e.g., git clone failures during allocation).
        
        CAUTION: Only use this when you're sure no generation session is actively using
        the workspace. Check session status first.
        
        Args:
            workspace_id: The workspace to deallocate
            reason: Reason for forced deallocation (for audit trail)
            
        Raises:
            WorkspaceNotFoundError: Workspace doesn't exist
            WorkspacePoolError: Other errors
            
        Example:
            >>> await service.force_deallocate_workspace("ws-01-1", "git_clone_failure")
        """
        ws_doc = await self._db_adapter.get_workspace(workspace_id)

        if not ws_doc:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        current_status = ws_doc.get("status")
        locked_by = ws_doc.get("locked_by")

        # Refuse if there is anything on the filesystem — skipping CLEANING here means no archive
        # step runs. If code was written, the next allocation will find it on the NFS volume.
        # Run POST /api/v1/workspaces/{id}/force-clean first to archive and wipe the workspace.
        workspace_path = self._get_workspace_path(workspace_id)
        if workspace_path.exists():
            is_clean = await self.verify_workspace_clean(workspace_path)
            if not is_clean:
                raise WorkspacePoolError(
                    f"Workspace {workspace_id} filesystem is not clean — refusing admin_deallocate "
                    f"to prevent data leakage to the next tenant. "
                    f"Call POST /api/v1/workspaces/{workspace_id}/force-clean first."
                )

        logger.warning(
            f"Force deallocating workspace {workspace_id} "
            f"(status={current_status}, locked_by={locked_by}, reason={reason})"
        )

        now = datetime.now(timezone.utc)

        # ALLOCATED → AVAILABLE via state machine (skips CLEANING — operator confirms no code written)
        await self._workspace_sm.admin_deallocate(
            workspace_id=workspace_id,
            reason=reason,
            triggered_by=TriggeredBy.ADMIN_DEALLOCATE,
        )

        # Append to allocation_history (non-status metadata — legal outside state machine)
        history_entry = {
            "generation_id": locked_by,
            "released_at": now,
            "outcome": f"force_deallocated_{reason}",
        }
        current_history = ws_doc.get("allocation_history", [])
        current_history.append(history_entry)
        await self._db_adapter.update_workspace(workspace_id, {
            "allocation_history": current_history
        })

        logger.info(
            f"Force deallocated workspace {workspace_id} from {current_status} -> available "
            f"(was locked_by={locked_by}, reason={reason})"
        )
    
    async def force_deallocate_workspaces_batch(
        self,
        workspace_ids: List[str],
        reason: str = "manual_deallocation"
    ) -> Dict[str, Any]:
        """
        Force deallocate multiple workspaces in batch.
        
        Useful for recovering from system-wide failures where multiple
        workspaces got stuck.
        
        Args:
            workspace_ids: List of workspace IDs to deallocate
            reason: Reason for forced deallocation
            
        Returns:
            Dictionary with success/failure counts and details
            
        Example:
            >>> result = await service.force_deallocate_workspaces_batch(
            ...     ["ws-01-1", "ws-01-2", "ws-01-3"],
            ...     "git_clone_failures"
            ... )
            >>> print(f"Deallocated {result['success']} workspaces")
        """
        results = {
            "total": len(workspace_ids),
            "success": 0,
            "failed": 0,
            "details": [],
        }
        
        for ws_id in workspace_ids:
            try:
                await self.force_deallocate_workspace(ws_id, reason)
                results["success"] += 1
                results["details"].append({
                    "workspace_id": ws_id,
                    "status": "success",
                })
            except Exception as e:
                results["failed"] += 1
                results["details"].append({
                    "workspace_id": ws_id,
                    "status": "failed",  # state-ok: batch result dict, not a Firestore status write
                    "error": str(e),
                })
                logger.error(
                    f"Failed to force deallocate workspace {ws_id}: {e}",
                    exc_info=True
                )
        
        return results
    
    async def force_release_stuck_workspace(
        self,
        workspace_id: str,
        reason: str = "manual_release_stuck",
        verify_clean: bool = True
    ) -> None:
        """
        Force release a workspace that's stuck in 'stuck' state, converting it to 'available'.
        
        This is a graceful recovery operation for workspaces that got stuck during cleanup.
        Unlike force_deallocate_workspace, this handles workspaces that are already in 'stuck'
        state (cleanup failed) rather than 'allocated' state.
        
        The method can optionally verify the workspace is clean before marking it available.
        If verification fails, the workspace remains in 'stuck' state.
        
        CAUTION: Only use this when you're confident the workspace can be safely released.
        If the workspace has unarchived data, you should manually recover it first.
        
        Args:
            workspace_id: The workspace to release
            reason: Reason for forced release (for audit trail)
            verify_clean: If True, verify workspace is clean before marking available (default: True)
            
        Raises:
            WorkspaceNotFoundError: Workspace doesn't exist
            WorkspacePoolError: Workspace is not in 'stuck' state, or verification failed
            
        Example:
            >>> await service.force_release_stuck_workspace("ws-01-1", "manual_intervention")
        """
        ws_doc = await self._db_adapter.get_workspace(workspace_id)

        if not ws_doc:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        logger.warning(
            f"Force releasing stuck workspace {workspace_id} "
            f"(reason={reason}, verify_clean={verify_clean})"
        )
        
        workspace_path = self._get_workspace_path(workspace_id)
        
        # Optionally verify workspace is clean before releasing
        if verify_clean:
            if workspace_path.exists():
                is_clean = await self.verify_workspace_clean(workspace_path)
                if not is_clean:
                    error_msg = (
                        f"Workspace {workspace_id} verification failed. "
                        f"Workspace is not clean and cannot be safely released. "
                        f"Please manually clean the workspace or set verify_clean=False."
                    )
                    logger.error(error_msg)
                    raise WorkspaceVerificationError(error_msg)
                logger.info(f"Workspace {workspace_id} verified clean before release")
            else:
                logger.info(
                    f"Workspace {workspace_id} directory does not exist, "
                    f"skipping verification"
                )
        
        now = datetime.now(timezone.utc)

        await self._workspace_sm.admin_release_stuck(
            workspace_id=workspace_id,
            reason=reason,
            triggered_by=TriggeredBy.ADMIN_RELEASE_STUCK,
            clean_verified=verify_clean,
        )

        # Add to allocation history for audit trail
        last_used_by = ws_doc.get("last_used_by")
        history_entry = {
            "generation_id": last_used_by,
            "released_at": now,
            "outcome": f"force_released_stuck_{reason}",
        }
        
        current_history = ws_doc.get("allocation_history", [])
        current_history.append(history_entry)

        await self._db_adapter.update_workspace(workspace_id, {
            "allocation_history": current_history
        })

        logger.info(
            f"Force released stuck workspace {workspace_id} from stuck -> available "
            f"(was last_used_by={last_used_by}, reason={reason}, verify_clean={verify_clean})"
        )
    
    async def force_release_stuck_workspaces_batch(
        self,
        workspace_ids: List[str],
        reason: str = "manual_release_stuck",
        verify_clean: bool = True
    ) -> Dict[str, Any]:
        """
        Force release multiple stuck workspaces in batch.
        
        Useful for recovering from system-wide failures where multiple
        workspaces got stuck during cleanup.
        
        Args:
            workspace_ids: List of workspace IDs to release
            reason: Reason for forced release
            verify_clean: If True, verify each workspace is clean before marking available
            
        Returns:
            Dictionary with success/failure counts and details
            
        Example:
            >>> result = await service.force_release_stuck_workspaces_batch(
            ...     ["ws-01-1", "ws-01-2", "ws-01-3"],
            ...     "cleanup_failure_recovery"
            ... )
            >>> print(f"Released {result['success']} workspaces")
        """
        results = {
            "total": len(workspace_ids),
            "success": 0,
            "failed": 0,
            "details": [],
        }
        
        for ws_id in workspace_ids:
            try:
                await self.force_release_stuck_workspace(ws_id, reason, verify_clean)
                results["success"] += 1
                results["details"].append({
                    "workspace_id": ws_id,
                    "status": "success",
                })
            except Exception as e:
                results["failed"] += 1
                results["details"].append({
                    "workspace_id": ws_id,
                    "status": "failed",  # state-ok: batch result dict, not a Firestore status write
                    "error": str(e),
                })
                logger.error(
                    f"Failed to force release stuck workspace {ws_id}: {e}",
                    exc_info=True
                )
        
        return results

    async def initialize_git_repo(
        self,
        workspace_path: Path,
        generation_id: str
    ) -> None:
        """
        Initialize git repository in a workspace and create initial commit.

        This method is called after extracting files to a workspace to set up version control.
        It performs the following operations:
        1. Initialize git repo if not already initialized
        2. Create/checkout main branch
        3. Add all files
        4. Configure git user
        5. Create initial commit

        Args:
            workspace_path: Path to workspace directory
            generation_id: Generation ID for commit message

        Raises:
            WorkspacePoolError: If git operations fail
        """
        try:
            redact_token = resolve_github_auth_for_generation_id(self._db, generation_id).token
        except GithubAuthResolutionError as auth_err:
            logger.error(
                "initialize_git_repo for generation session %s: auth resolution failed, "
                "cannot proceed safely. Auth error: %s",
                generation_id,
                auth_err,
            )
            raise WorkspacePoolError(
                f"initialize_git_repo failed for generation session {generation_id}: "
                f"credentials could not be resolved ({auth_err})"
            ) from auth_err

        try:
            git_dir = workspace_path / ".git"

            # Initialize git if not already initialized
            if not git_dir.exists():
                await run_git(
                    workspace_path,
                    ["init"]
                )
                # Set default branch to main
                await run_git(
                    workspace_path,
                    ["checkout", "-b", WORKSPACE_DEFAULT_BRANCH]
                )
            else:
                await _maybe_repair_git_refs_after_user_extract(workspace_path)

            ensure_workspace_gitignore(workspace_path)

            # Add all files
            await run_git(
                workspace_path,
                ["add", "-A"]
            )

            # Configure git user (required for commit)
            await run_git(
                workspace_path,
                ["config", "user.name", GIT_COMMITTER_USER_NAME]
            )
            await run_git(
                workspace_path,
                ["config", "user.email", GIT_COMMITTER_USER_EMAIL]
            )

            # Create initial commit
            await run_git(
                workspace_path,
                [
                    "commit",
                    "-m", "SKIP_initial_user_source",
                    "--allow-empty"
                ]
            )

            logger.info(
                f"Initialized git repo and created initial commit "
                f"in {workspace_path} for generation session {generation_id}"
            )

            # Push initial commit to remote: establishes "before agents" boundary on origin/main.
            # Force-push required: local main is an orphan (no history) after _reset_git_repo,
            # but remote main may have accumulated history from previous sessions.
            # Safe to force-push: workspace repos are dedicated per-workspace; generated code
            # is preserved on archive/{generation_id} branches, not on main.
            # Non-fatal: warn if remote doesn't exist (test / dev environments).
            try:
                await run_git(workspace_path, ["push", "--force", "origin", WORKSPACE_DEFAULT_BRANCH])
                logger.info(f"Pushed initial commit to origin/{WORKSPACE_DEFAULT_BRANCH} for {workspace_path}")
            except GitCommandError as push_err:
                logger.warning(
                    f"Could not push initial commit to remote (remote may not exist): {push_err}"
                )

        except Exception as e:
            msg = self._sanitize_token_in_message(str(e), redact_token)
            logger.error(
                "Failed to initialize git repo in %s: %s",
                workspace_path,
                msg,
                exc_info=True,
            )
            raise WorkspacePoolError(
                f"Failed to initialize git repository: {msg}"
            ) from e

    # Private helper methods
    
    def _get_workspace_path(self, workspace_id: str) -> Path:
        """Get filesystem path for a workspace."""
        return self.workspace_base_path / workspace_id
    
    def _get_authenticated_repo_url(self, repo_url: str, auth: GithubAuthContext) -> str:
        """
        Construct authenticated git URL with username:token from the resolved auth context.
        """
        if "@" in repo_url.split("://")[1] if "://" in repo_url else False:
            return repo_url

        if repo_url.startswith("https://"):
            url_part = repo_url[8:]
            return f"https://{auth.git_user_name}:{auth.token}@{url_part}"

        sanitized_repo_url = self._sanitize_token_in_message(repo_url, auth.token)
        logger.warning("Could not inject credentials into URL format: %s", sanitized_repo_url)
        return repo_url

    async def _ensure_repo_cloned(
        self, workspace_id: str, ws_doc: Dict[str, Any], generation_id: str
    ) -> None:
        """
        Ensure repository is cloned to workspace path.
        
        Checks if repository exists at workspace path. If not, clones it from repo_url.
        If repository exists but is not a valid git repo, removes it and clones fresh.
        
        Args:
            workspace_id: Workspace ID
            ws_doc: Workspace document from database
            
        Raises:
            WorkspacePoolError: If cloning fails
        """
        workspace_path = self._get_workspace_path(workspace_id)
        repo_url = ws_doc.get("repo_url")
        
        if not repo_url:
            raise WorkspacePoolError(
                f"Workspace {workspace_id} has no repo_url configured"
            )

        try:
            auth = resolve_github_auth_for_generation_id(self._db, generation_id)
        except GithubAuthResolutionError as e:
            raise WorkspacePoolError(str(e)) from e

        authenticated_repo_url = self._get_authenticated_repo_url(repo_url, auth)

        git_dir = workspace_path / ".git"
        
        # Check if repository already exists and is valid
        if workspace_path.exists():
            if git_dir.exists() and git_dir.is_dir():
                try:
                    # Verify it's a valid git repository by checking remote URL
                    result = await run_git(
                        workspace_path,
                        ["remote", "get-url", "origin"]
                    )
                    existing_url = result

                    # If remote URL matches (plain or authenticated), repository is already cloned
                    plain_match = existing_url == repo_url or existing_url == repo_url.replace(".git", "")
                    auth_match = existing_url == authenticated_repo_url or existing_url == authenticated_repo_url.replace(".git", "")
                    if plain_match or auth_match:
                        logger.debug(
                            f"Repository already cloned for workspace {workspace_id} "
                            f"at {workspace_path}"
                        )
                        # CRITICAL: Reset git state to ensure workspace is pristine
                        # This fixes the bug where allocated workspaces have stale commits
                        # from previous sessions on both local and remote main branches
                        await self._reset_workspace_for_allocation(workspace_path, workspace_id)
                        return
                    # Sanitize URLs before logging to prevent token exposure
                    sanitized_repo_url = self._sanitize_token_in_message(repo_url, auth.token)
                    sanitized_existing_url = self._sanitize_token_in_message(
                        existing_url, auth.token
                    )
                    # Remote URL doesn't match - log warning but don't fail
                    logger.warning(
                        f"Workspace {workspace_id} has different remote URL "
                        f"(expected: {sanitized_repo_url}, found: {sanitized_existing_url}). "
                        f"Will clone fresh repository."
                    )
                except GitCommandError:
                    # Git command failed - repository might be corrupted
                    logger.warning(
                        f"Workspace {workspace_id} has invalid git repository. "
                        f"Will clone fresh repository."
                    )
                except Exception as e:
                    logger.warning(
                        f"Error checking existing repository for workspace {workspace_id}: {e}. "
                        f"Will clone fresh repository."
                    )
            else:
                # Directory exists but is not a git repository
                logger.info(
                    f"Workspace {workspace_id} directory exists but is not a git repository. "
                    f"Will clone fresh repository."
                )
            
            # Remove existing directory to clone fresh
            logger.info(f"Removing existing directory for workspace {workspace_id}")
            shutil.rmtree(workspace_path)
        
        sanitized_repo_url = self._sanitize_token_in_message(repo_url, auth.token)
        logger.info(
            "Cloning repository %s to workspace %s at %s - with authentication",
            sanitized_repo_url,
            workspace_id,
            workspace_path,
        )
        masked_url = authenticated_repo_url.replace(
            f"{auth.git_user_name}:{auth.token}@",
            f"{auth.git_user_name}:***@",
        )
        logger.debug("Using authenticated URL format: %s", masked_url)
        
        # Ensure parent directory exists
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Clone repository directly into workspace path using authenticated URL
            # Try cloning main branch first, fallback to default branch if main doesn't exist
            try:
                await run_git(
                    workspace_path.parent,  # Run from parent directory
                    [
                        "clone",
                        "--branch", WORKSPACE_DEFAULT_BRANCH,  # Clone default workspace branch
                        authenticated_repo_url,  # Use authenticated URL
                        str(workspace_path.name)  # Clone to workspace_id directory name
                    ]
                )
            except GitCommandError:
                # Main branch doesn't exist, clone default branch instead
                logger.info(
                    f"Main branch not found for {repo_url}, cloning default branch instead"
                )
                await run_git(
                    workspace_path.parent,
                    [
                        "clone",
                        authenticated_repo_url,  # Use authenticated URL
                        str(workspace_path.name)
                    ]
                )

            # Configure git to trust this directory (for safety)
            await run_git(
                workspace_path,
                ["config", "--global", "--add", "safe.directory", str(workspace_path)]
            )
            
            # Reset to clean state (both local and remote) after cloning
            # This ensures pristine state even if remote main has old commits from previous uses
            logger.info(
                f"Resetting cloned repository to clean state (orphan main branch) "
                f"for workspace {workspace_id}"
            )
            await self._reset_workspace_for_allocation(workspace_path, workspace_id)
            
            sanitized_repo_url = self._sanitize_token_in_message(repo_url, auth.token)
            logger.info(
                f"Successfully cloned and reset repository {sanitized_repo_url} to workspace {workspace_id}"
            )

        except GitCommandError as e:
            error_output = e.stderr or e.stdout or str(e)

            sanitized_error = self._sanitize_token_in_message(error_output, auth.token)

            sanitized_repo_url = self._sanitize_token_in_message(repo_url, auth.token)

            error_msg = f"Failed to clone repository {sanitized_repo_url}: {sanitized_error}"

            if "could not read Username" in error_output or "Authentication failed" in error_output:
                error_msg += (
                    " GitHub authentication failed. Verify platform default PAT, per-key PAT "
                    "(PUT /api/v1/auth/github-token), and repository access."
                )
            
            logger.error(
                f"Git clone failed for workspace {workspace_id}: {error_msg}",
                exc_info=True
            )
            raise WorkspacePoolError(error_msg) from e
        except Exception as e:
            error_str = str(e)
            sanitized_error = self._sanitize_token_in_message(error_str, auth.token)
            error_msg = f"Unexpected error cloning repository {repo_url}: {sanitized_error}"
            logger.error(
                f"Unexpected error cloning repository for workspace {workspace_id}: {error_msg}",
                exc_info=True
            )
            raise WorkspacePoolError(error_msg) from e
    
    async def _archive_generation_session_work(
        self,
        workspace_path: Path,
        generation_id: str
    ) -> None:
        """
        Archive generation session work by creating a branch and committing all changes.
        
        Steps:
        1. Create branch named after generation_id
        2. Add all files (git add -A)
        3. Commit with message "Archive {generation_id}"
        4. Push branch to remote
        
        Args:
            workspace_path: Path to workspace directory
            generation_id: Generation ID to use as branch name
            
        Raises:
            WorkspacePoolError: If archiving fails
        """
        if not workspace_path.exists():
            logger.warning(
                f"Workspace path {workspace_path} does not exist, skipping archive"
            )
            return
        
        git_dir = workspace_path / ".git"
        if not git_dir.exists():
            logger.warning(
                f"Workspace {workspace_path} is not a git repository, skipping archive"
            )
            return

        try:
            redact_token = resolve_github_auth_for_generation_id(self._db, generation_id).token
        except GithubAuthResolutionError as auth_err:
            logger.error(
                "_archive_generation_session_work for generation session %s: auth resolution failed, "
                "error output suppressed to prevent credential exposure. Auth error: %s",
                generation_id,
                auth_err,
            )
            raise WorkspacePoolError(
                f"_archive_generation_session_work failed for generation session {generation_id}: "
                f"credentials could not be resolved ({auth_err})"
            ) from auth_err

        try:
            # Ensure we're on a valid branch (not detached HEAD)
            try:
                current_branch_result = await run_git(
                    workspace_path,
                    ["branch", "--show-current"]
                )
                current_branch = current_branch_result
                logger.info(f"Current branch: {current_branch}")
            except GitCommandError:
                # Detached HEAD - checkout main or create it
                logger.info("Detached HEAD state, checking out main")
                try:
                    await run_git(
                        workspace_path,
                        ["checkout", WORKSPACE_DEFAULT_BRANCH]
                    )
                    current_branch = WORKSPACE_DEFAULT_BRANCH
                except GitCommandError:
                    # Default branch doesn't exist, create it
                    await run_git(
                        workspace_path,
                        ["checkout", "-b", WORKSPACE_DEFAULT_BRANCH]
                    )
                    current_branch = WORKSPACE_DEFAULT_BRANCH

            # Create archive branch from current state (includes all current changes)
            # If branch already exists, delete it first to start fresh
            try:
                await run_git(
                    workspace_path,
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{generation_id}"]
                )
                # Branch exists - delete it to start fresh
                logger.info(
                    f"Archive branch '{generation_id}' already exists, deleting it"
                )
                await run_git(
                    workspace_path,
                    ["branch", "-D", generation_id]
                )
            except GitCommandError:
                # Branch doesn't exist - that's fine
                pass

            # Create archive branch from current branch (includes all changes)
            logger.info(
                f"Creating archive branch '{generation_id}' from '{current_branch}'"
            )
            await run_git(
                workspace_path,
                ["checkout", "-b", generation_id]
            )

            # Add all files
            logger.info(f"Staging all files for archive branch '{generation_id}'")
            await run_git(
                workspace_path,
                ["add", "-A"]
            )

            # Commit with archive message (use --allow-empty in case there are no changes)
            logger.info(f"Committing archive for generation session {generation_id}")
            try:
                await run_git(
                    workspace_path,
                    [
                        "commit",
                        "-m", f"Archive {generation_id}",
                        "--allow-empty"
                    ]
                )
            except GitCommandError as e:
                # If commit fails (e.g., no changes and --allow-empty didn't work),
                # that's okay - we'll still push the branch with existing commits
                logger.info(
                    f"Could not create commit for generation session {generation_id} "
                    f"(may have no changes): {e}. Branch will be pushed with existing state."
                )

            # Check if remote branch already exists before pushing
            # This prevents non-fast-forward errors when branch exists on remote
            remote_branch_exists = False
            try:
                remote_check = await run_git(
                    workspace_path,
                    ["ls-remote", "--heads", "origin", generation_id]
                )
                remote_branch_exists = bool(remote_check)
            except GitCommandError:
                # ls-remote failed (e.g., no remote configured) - will try push anyway
                logger.debug(
                    f"Could not check remote for branch '{generation_id}', "
                    f"will attempt push"
                )

            if remote_branch_exists:
                # Remote branch exists - data already archived
                logger.info(
                    f"Archive branch '{generation_id}' already exists on remote. "
                    f"Data is already archived, skipping push."
                )
            else:
                # Remote branch doesn't exist - push it
                logger.info(f"Pushing archive branch '{generation_id}' to remote")
                try:
                    await run_git(
                        workspace_path,
                        ["push", "origin", generation_id]
                    )
                    logger.info(
                        f"Successfully pushed archive branch '{generation_id}' to remote"
                    )
                except GitCommandError as push_error:
                    # Handle non-fast-forward error gracefully
                    # This can happen if branch was created on remote between check and push
                    error_output = push_error.stderr or push_error.stdout or str(push_error)
                    if "non-fast-forward" in error_output.lower():
                        # Verify remote branch exists now (race condition)
                        try:
                            remote_check = await run_git(
                                workspace_path,
                                ["ls-remote", "--heads", "origin", generation_id]
                            )
                            if remote_check:
                                # Remote branch exists - data already archived, treat as success
                                logger.info(
                                    f"Push rejected (non-fast-forward), but archive branch "
                                    f"'{generation_id}' now exists on remote. "
                                    f"Data is already archived."
                                )
                            else:
                                # Remote doesn't exist - re-raise original error
                                raise push_error
                        except GitCommandError:
                            # Can't verify remote - re-raise original push error
                            raise push_error
                    else:
                        # Different error - re-raise
                        raise
            
            logger.info(
                f"Successfully archived generation session {generation_id} "
                f"to branch '{generation_id}'"
            )
        
        except GitCommandError as e:
            error_output = e.stderr or e.stdout or str(e)
            sanitized_error = self._sanitize_token_in_message(error_output, redact_token)
            error_msg = (
                f"Failed to archive generation session {generation_id}: {sanitized_error}"
            )
            logger.error(error_msg, exc_info=True)
            raise WorkspacePoolError(error_msg) from e
        except Exception as e:
            error_str = str(e)
            sanitized_error = self._sanitize_token_in_message(error_str, redact_token)
            error_msg = f"Unexpected error archiving generation session {generation_id}: {sanitized_error}"
            logger.error(error_msg, exc_info=True)
            raise WorkspacePoolError(error_msg) from e
    
    async def _verify_archive_pushed(
        self,
        workspace_path: Path,
        generation_id: str
    ) -> bool:
        """
        Verify that archive branch was successfully pushed to remote.
        
        This is a critical safety check to ensure data is backed up before
        we destroy the local workspace. Without this check, a silent push
        failure could result in permanent data loss.
        
        Args:
            workspace_path: Path to workspace directory
            generation_id: Generation ID (used as branch name)
            
        Returns:
            True if branch exists on remote, False otherwise
            
        Example:
            >>> is_pushed = await self._verify_archive_pushed(workspace_path, "est-123")
            >>> if not is_pushed:
            ...     # Don't proceed with cleanup!
        """
        # If workspace doesn't exist, there's nothing to verify
        # This can happen if workspace was never created or was deleted by admin
        if not workspace_path.exists():
            logger.warning(
                f"Workspace path {workspace_path} does not exist, "
                f"skipping archive verification for branch '{generation_id}'. "
                f"Since there's nothing to archive, allowing cleanup to proceed."
            )
            return True
        
        git_dir = workspace_path / ".git"
        if not git_dir.exists():
            logger.warning(
                f"Workspace {workspace_path} is not a git repository, "
                f"skipping archive verification for branch '{generation_id}'. "
                f"Since there's nothing to archive, allowing cleanup to proceed."
            )
            return True

        try:
            redact_token = resolve_github_auth_for_generation_id(self._db, generation_id).token
        except GithubAuthResolutionError as auth_err:
            logger.error(
                "_verify_archive_pushed for generation session %s: auth resolution failed, "
                "error output suppressed to prevent credential exposure. Auth error: %s",
                generation_id,
                auth_err,
            )
            # Cannot safely run ls-remote without credentials; fail safe (no cleanup)
            return False

        try:
            # Check if branch exists on remote using ls-remote
            # This is safer than checking local refs as it confirms GitHub has the data
            result = await run_git(
                workspace_path,
                ["ls-remote", "--heads", "origin", generation_id]
            )

            # Output format: "<sha> refs/heads/<branch-name>"
            # Empty output means branch doesn't exist on remote
            remote_exists = bool(result)

            if remote_exists:
                logger.info(
                    f"Archive verification PASSED: branch '{generation_id}' exists on remote"
                )
            else:
                logger.error(
                    f"Archive verification FAILED: branch '{generation_id}' NOT found on remote. "
                    f"This indicates git push failed silently."
                )

            return remote_exists

        except GitCommandError as e:
            error_output = e.stderr or e.stdout or str(e)
            sanitized_error = self._sanitize_token_in_message(error_output, redact_token)
            logger.error(
                f"Failed to verify archive push for branch '{generation_id}': {sanitized_error}",
                exc_info=True
            )
            # On verification error, assume branch doesn't exist (fail safe)
            return False
        
        except Exception as e:
            error_str = str(e)
            sanitized_error = self._sanitize_token_in_message(error_str, redact_token)
            logger.error(
                f"Unexpected error verifying archive push for branch '{generation_id}': {sanitized_error}",
                exc_info=True
            )
            # On verification error, assume branch doesn't exist (fail safe)
            return False
    
    async def _reset_workspace_for_allocation(self, workspace_path: Path, workspace_id: str) -> None:
        """
        Reset workspace git state for fresh allocation.
        
        Called after verifying repository exists with correct remote URL.
        Ensures both LOCAL and REMOTE main branches are clean (no commits).
        
        This is different from cleanup reset:
        - Cleanup: Only resets local (remote preserved for archive access)
        - Allocation: Resets both local AND remote (workspace must be pristine)
        
        Steps:
        1. Reset local git to orphan main (no commits)
        2. Create empty commit so the branch ref exists
        3. Force push to remote to reset remote main
        
        Args:
            workspace_path: Path to workspace directory
            workspace_id: Workspace ID (for logging)
            
        Raises:
            WorkspacePoolError: If reset fails critically
        """
        try:
            logger.info(f"Resetting git state for workspace {workspace_id}")
            
            # Step 1: Reset local git to orphan main (reuse logic from cleanup)
            await self._reset_git_repo_local(workspace_path)
            
            # Step 2: Create an empty commit so the orphan main branch ref exists.
            # Without at least one commit, "git push --force origin main" fails with
            # "src refspec main does not match any".
            # Set identity explicitly — orphan checkout wipes local git config.
            await run_git(workspace_path, ["config", "user.name", GIT_COMMITTER_USER_NAME])
            await run_git(workspace_path, ["config", "user.email", GIT_COMMITTER_USER_EMAIL])
            await run_git(
                workspace_path,
                ["commit", "--allow-empty", "-m", f"chore: workspace reset for {workspace_id}"]
            )

            # Step 3: Force push to remote to reset remote main.
            # This is CRITICAL — without this the remote retains stale commits from prior
            # sessions.  All agent commits must be pushable; a workspace whose remote
            # cannot be reset is not allocatable.  Raise so the caller can try another workspace.
            await run_git(
                workspace_path,
                ["push", "--force", "origin", WORKSPACE_DEFAULT_BRANCH]
            )
            logger.info(f"Force pushed clean {WORKSPACE_DEFAULT_BRANCH} branch to remote for workspace {workspace_id}")
            
            logger.info(f"Workspace {workspace_id} git state reset complete")
            
        except Exception as e:
            logger.error(
                f"Failed to reset git state for workspace {workspace_id}: {e}",
                exc_info=True
            )
            raise WorkspacePoolError(
                f"Failed to reset workspace {workspace_id} git state during allocation"
            ) from e

    async def _reset_git_repo_local(self, workspace_path: Path) -> None:
        """
        Reset LOCAL git repository to clean state with no commits on main branch.
        
        Extracted shared logic for both cleanup and allocation reset.
        
        Handles multiple scenarios:
        - After archive: workspace is on archive branch (generation_id), main exists
        - Retry cleanup: might be on orphan main or any other state
        - Other edge cases: detached HEAD, etc.
        
        Steps:
        1. Detach HEAD if we're on main (so we can delete it)
        2. Delete existing main branch if it exists
        3. Create fresh orphan branch main (removes all commit history)
        4. Remove all files from index
        5. Clean untracked files and directories
        
        Args:
            workspace_path: Path to workspace directory
        """
        try:
            # 1. Ensure we're not on main branch so we can delete it
            # Strategy: Switch to a temp branch first, then delete main, then create orphan main
            try:
                # Get current branch (or detect orphan/detached HEAD)
                result = await run_git(
                    workspace_path,
                    ["symbolic-ref", "--short", "HEAD"]
                )
                current_branch = result
                logger.debug(f"Current branch: {current_branch}")
            except GitCommandError:
                # Detached HEAD or orphan branch with no commits
                # In this case, HEAD doesn't point to a ref
                current_branch = None
                logger.debug("Detached HEAD or orphan branch detected")

            # If we're on main (or can't determine), switch to temp branch first
            # This ensures we can delete main if it exists
            if current_branch is None or current_branch == WORKSPACE_DEFAULT_BRANCH:
                try:
                    await run_git(
                        workspace_path,
                        ["checkout", "--detach"]
                    )
                    logger.debug(f"Detached HEAD to prepare for {WORKSPACE_DEFAULT_BRANCH} branch deletion")
                except GitCommandError:
                    # Already detached or some other state - that's fine
                    logger.debug("HEAD already detached or in suitable state")

            # Delete default branch if it exists
            try:
                await run_git(
                    workspace_path,
                    ["branch", "-D", WORKSPACE_DEFAULT_BRANCH]
                )
                logger.debug(f"Deleted existing {WORKSPACE_DEFAULT_BRANCH} branch")
            except GitCommandError:
                # Default branch doesn't exist - that's fine
                logger.debug(f"{WORKSPACE_DEFAULT_BRANCH} branch doesn't exist (or already deleted)")

            # 2. Create fresh orphan branch (removes all commit history)
            logger.debug(f"Creating orphan branch '{WORKSPACE_DEFAULT_BRANCH}' with no commits")
            await run_git(
                workspace_path,
                ["checkout", "--orphan", WORKSPACE_DEFAULT_BRANCH]
            )

            # 3. Remove all files from index (they're still in working directory)
            try:
                await run_git(
                    workspace_path,
                    ["rm", "-rf", "."]
                )
            except GitCommandError:
                # No files to remove - that's okay
                pass

            # 4. Clean untracked files and directories.
            # -ff (double force) is required to also remove nested git repositories
            # (directories that contain their own .git). A single -f skips them silently,
            # which can leave stale workspace snapshots on NFS across allocation cycles.
            await run_git(
                workspace_path,
                ["clean", "-ffdx"]
            )
            
        except GitCommandError:
            raise  # Re-raise to be handled by caller
    
    async def _reset_git_repo(
        self,
        workspace_path: Path,
        repo_url: str,
        generation_id: Optional[str] = None,
    ) -> None:
        """
        Reset LOCAL git repository to clean state with no commits on main branch.
        
        IMPORTANT: This only resets LOCAL state. Remote main branch is NOT reset.
        This is intentional - remote reset operations are fragile (network, auth, 
        rate limits) and unnecessary for cleanup since workspaces are cloned fresh on allocation.
        
        Note: For allocation, use _reset_workspace_for_allocation() which also resets remote.
        
        Args:
            workspace_path: Path to workspace directory
            repo_url: Repository URL (for reference, not used)
            generation_id: If set, used to redact tokens from error messages
        """
        redact_token: Optional[str] = None
        if generation_id:
            try:
                redact_token = resolve_github_auth_for_generation_id(self._db, generation_id).token
            except GithubAuthResolutionError as auth_err:
                logger.warning(
                    "_reset_git_repo for generation session %s: auth resolution failed, "
                    "git error output will use regex-only redaction. Auth error: %s",
                    generation_id,
                    auth_err,
                )
                # _reset_git_repo only resets local state (no push), so proceeding
                # with regex-only redaction is acceptable here.

        try:
            await self._reset_git_repo_local(workspace_path)
            
            # Skip remote reset during cleanup - too fragile (network, auth, rate limits)
            # Remote state doesn't matter because:
            # - Workspaces are cloned fresh on allocation
            # - Local reset ensures clean state for next use
            # - Remote commits don't affect local workspace after reset
            # If remote cleanup is needed, it can be done via separate admin task
            logger.info(
                "Local git repository reset to clean state (orphan main branch). "
                "Remote reset skipped for reliability (workspaces cloned fresh on allocation)."
            )
        
        except GitCommandError as e:
            error_output = e.stderr or e.stdout or str(e)
            sanitized_error = self._sanitize_token_in_message(error_output, redact_token)
            error_msg = f"Failed to reset git repository: {sanitized_error}"
            logger.error(error_msg, exc_info=True)
            raise WorkspacePoolError(error_msg) from e
    
    def _send_stuck_workspace_alert(
        self,
        workspace_id: str,
        generation_id: Optional[str],
        error: str
    ) -> None:
        """
        Send Slack alert when a workspace gets stuck.
        
        This is a critical alert that requires manual intervention.
        The workspace data is preserved and needs admin recovery.
        
        Args:
            workspace_id: The stuck workspace ID
            generation_id: The generation session that was using it (if any)
            error: The error that caused the stuck state
        """
        try:
            # Lazy import to avoid circular dependency and logging setup issues
            from app.core.notifications import notifications
            
            # Sanitize error message to remove any tokens.
            # Use regex-only redaction (no specific token available here).
            # Do not include raw git error output — use a safe summary instead.
            sanitized_error = self._sanitize_token_in_message(error, None)
            # Truncate to avoid accidentally logging long git error blobs
            sanitized_error = sanitized_error[:500] if sanitized_error else "unknown error"
            
            # Format a clear, actionable alert message
            alert_parts = [
                "🚨 *Workspace Stuck - Manual Intervention Required*",
                "",
                f"*Workspace ID:* `{workspace_id}`",
            ]
            
            if generation_id:
                alert_parts.append(f"*Run ID:* `{generation_id}`")
            
            alert_parts.extend([
                "*Status:* `stuck` (cleanup failed)",
                "",
                "*Error:*",
                f"```{sanitized_error}```",
                "",
                "*Action Required:*",
                "1. Check workspace data in Firestore",
                "2. Inspect workspace directory if needed",
                "3. Manually recover data before cleanup",
                "4. Use GCP Console to investigate",
                "",
                f"*Workspace Path:* `/workspaces/{workspace_id}`"
            ])
            
            message = "\n".join(alert_parts)
            
            # Send notification (will use Slack if configured)
            notifications.notify(message)
            
            logger.info(
                f"Sent stuck workspace alert for {workspace_id} "
                f"(generation session: {generation_id})"
            )
        
        except Exception as e:
            # Don't fail cleanup process if notification fails
            logger.error(
                f"Failed to send stuck workspace alert for {workspace_id}: {e}",
                exc_info=True
            )
