"""
GitArchiveService — archives generated code to a dedicated git branch.

Branch naming: archive/{generation_id}
Strategy (HR-9 Option B):
  1. Resolve HEAD commit SHA on the workspace repo
  2. Check if archive/{generation_id} already exists remotely
  3. If it exists AND points to the same SHA → already archived, confirm without push
  4. If it exists at a different SHA → force-push (repair case only)
  5. If it does not exist → push new branch
"""
import logging
from pathlib import Path

from app.services.git_utils import GitCommandError, run_git
from app.services.skip_mode_mock import is_skip_mode_enabled

logger = logging.getLogger(__name__)


class GitArchiveService:
    """
    Performs and verifies git archive branches for workspace repos.
    Injected into GenerationSessionStateMachine.complete() as workspace_archive_service.
    """
    archive_branch_prefix = "archive"

    def __init__(self, workspace_root: str = "/workspaces"):
        self._workspace_root = Path(workspace_root)

    def _workspace_repo_path(self, workspace_id: str) -> Path:
        """Returns the local git repo path for a workspace.
        The workspace directory itself is the git repo root — no subdirectory."""
        return self._workspace_root / workspace_id

    @staticmethod
    def branch_name(generation_id: str) -> str:
        return f"{GitArchiveService.archive_branch_prefix}/{generation_id}"

    async def archive_branch(self, workspace_id: str, generation_id: str) -> bool:
        """
        Pushes archive/{generation_id} branch to the remote.
        Returns True if archive succeeded or was already confirmed.
        Returns False if the push failed.
        """
        if is_skip_mode_enabled():
            logger.warning(
                "[SKIP_MODE] archive_branch skipped for workspace %s generation %s",
                workspace_id, generation_id,
            )
            return True

        repo = self._workspace_repo_path(workspace_id)
        branch = self.branch_name(generation_id)

        try:
            head_sha = await run_git(repo, ["rev-parse", "HEAD"])
        except GitCommandError:
            logger.error("archive_branch: cannot resolve HEAD for workspace %s", workspace_id)
            return False

        # Check if branch already exists on remote at the same SHA
        try:
            remote_sha_line = await run_git(repo, ["ls-remote", "origin", f"refs/heads/{branch}"])
        except GitCommandError:
            remote_sha_line = ""
        if remote_sha_line and remote_sha_line.split()[0] == head_sha:
            logger.info(
                "archive_branch: workspace %s branch %s already at correct SHA %s — confirmed",
                workspace_id, branch, head_sha
            )
            return True

        # Push the branch
        try:
            await run_git(repo, ["push", "origin", f"HEAD:refs/heads/{branch}"])
        except GitCommandError as e:
            logger.error(
                "archive_branch: push failed for workspace %s branch %s: %s",
                workspace_id, branch, e.stderr,
            )
            return False

        logger.info("archive_branch: workspace %s → branch %s pushed (%s)",
                    workspace_id, branch, head_sha)
        return True

    async def verify_archive_branch(self, workspace_id: str, generation_id: str) -> bool:
        """
        Verifies that archive/{generation_id} exists on the remote and
        points to the same commit as the local HEAD.
        Called by GenerationSessionStateMachine.complete() as a precondition check.

        Idempotent: can be called multiple times safely. If the branch was
        already pushed in a prior attempt, this returns True without re-pushing.
        """
        if is_skip_mode_enabled():
            logger.warning(
                "[SKIP_MODE] verify_archive_branch skipped for workspace %s generation %s — returning True",
                workspace_id, generation_id,
            )
            return True

        repo = self._workspace_repo_path(workspace_id)
        branch = self.branch_name(generation_id)

        try:
            head_sha = await run_git(repo, ["rev-parse", "HEAD"])
        except GitCommandError:
            return False

        try:
            remote_sha_line = await run_git(repo, ["ls-remote", "origin", f"refs/heads/{branch}"])
        except GitCommandError:
            remote_sha_line = ""

        if not remote_sha_line:
            # Branch does not exist yet — attempt to push now
            logger.info(
                "verify_archive_branch: branch %s not found for workspace %s — pushing now",
                branch, workspace_id
            )
            return await self.archive_branch(workspace_id, generation_id)

        remote_sha = remote_sha_line.split()[0]
        if remote_sha != head_sha:
            logger.warning(
                "verify_archive_branch: workspace %s branch %s SHA mismatch "
                "(remote %s, local %s) — force-pushing to repair",
                workspace_id, branch, remote_sha, head_sha
            )
            try:
                await run_git(repo, ["push", "--force", "origin", f"HEAD:refs/heads/{branch}"])
                return True
            except GitCommandError:
                return False

        return True
