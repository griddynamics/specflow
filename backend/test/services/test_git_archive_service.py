"""
Tests for GitArchiveService.
Covers all test cases from implementation-plan.md section 1.11.
"""
import pytest
from unittest.mock import patch
from pathlib import Path

from app.services.git_archive_service import GitArchiveService
from app.services.git_utils import GitCommandError, run_git


def make_service(workspace_root="/workspaces"):
    return GitArchiveService(workspace_root=workspace_root)


def _mock_run_git(responses: dict):
    """
    Build an AsyncMock side_effect for run_git(repo, args).
    responses maps args[0] (the git sub-command) to return value or exception.
    Raise GitCommandError when the value is an exception instance.
    """
    async def _side_effect(repo, args):
        key = args[0]
        value = responses.get(key)
        if isinstance(value, Exception):
            raise value
        return value if value is not None else ""
    return _side_effect


class TestVerifyArchiveBranch:
    @pytest.mark.asyncio
    async def test_branch_exists_at_matching_sha_returns_true_no_push(self):
        """Branch exists and SHA matches → returns True, no push called."""
        svc = make_service()

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "ls-remote":
                return "abc123\trefs/heads/archive/est-1"
            raise AssertionError(f"unexpected git call: {args}")

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.verify_archive_branch("ws-1", "est-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_branch_does_not_exist_calls_push(self):
        """Branch doesn't exist → calls archive_branch which pushes."""
        svc = make_service()
        push_called = []

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "ls-remote":
                return ""  # branch doesn't exist
            if args[0] == "push":
                push_called.append(True)
                return ""
            return ""

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.verify_archive_branch("ws-1", "est-1")
        assert result is True
        assert len(push_called) > 0

    @pytest.mark.asyncio
    async def test_branch_sha_mismatch_force_pushes(self):
        """Branch exists at different SHA → force-pushes, returns True."""
        svc = make_service()
        force_push_called = []

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "new_sha_456"
            if args[0] == "ls-remote":
                return "old_sha_123\trefs/heads/archive/est-1"
            if args[0] == "push" and "--force" in args:
                force_push_called.append(True)
                return ""
            return ""

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.verify_archive_branch("ws-1", "est-1")
        assert result is True
        assert len(force_push_called) > 0


class TestArchiveBranch:
    @pytest.mark.asyncio
    async def test_push_succeeds_returns_true(self):
        svc = make_service()
        push_called = []

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "ls-remote":
                return ""  # branch doesn't exist
            if args[0] == "push":
                push_called.append(True)
                return ""
            return ""

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.archive_branch("ws-1", "est-1")
        assert result is True
        assert len(push_called) == 1

    @pytest.mark.asyncio
    async def test_push_fails_returns_false_no_raise(self):
        """Push failure → returns False, does not raise."""
        svc = make_service()

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "ls-remote":
                return ""
            if args[0] == "push":
                raise GitCommandError(args, 128, "push failed")
            return ""

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.archive_branch("ws-1", "est-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_archive_branch_already_confirmed_no_push(self):
        """Branch already at matching SHA → returns True without re-pushing."""
        svc = make_service()
        push_called = []

        async def mock_run_git(repo, args):
            if args[0] == "rev-parse":
                return "abc123"
            if args[0] == "ls-remote":
                return "abc123\trefs/heads/archive/est-1"
            if args[0] == "push":
                push_called.append(True)
                return ""
            raise AssertionError(f"unexpected git call: {args}")

        with patch("app.services.git_archive_service.run_git", side_effect=mock_run_git):
            result = await svc.archive_branch("ws-1", "est-1")
        assert result is True
        assert len(push_called) == 0


class TestRunGitHelper:
    """Tests for the shared run_git utility (replaces TestGitHelper)."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_git_command_error(self):
        """run_git with non-zero exit code raises GitCommandError."""
        class FakeProc:
            returncode = 1
            async def communicate(self):
                return b"", b"error output"

        with patch("app.services.git_utils.asyncio.create_subprocess_exec", return_value=FakeProc()):
            with pytest.raises(GitCommandError) as exc_info:
                await run_git(Path("/fake/repo"), ["status"])
        assert exc_info.value.returncode == 1
        assert "error output" in exc_info.value.stderr

    @pytest.mark.asyncio
    async def test_correct_git_args_passed(self):
        """run_git passes 'git' + args to subprocess."""
        captured_args = []

        class FakeProc:
            returncode = 0
            async def communicate(self):
                return b"sha123\n", b""

        async def fake_subprocess(*args, **kwargs):
            captured_args.extend(args)
            return FakeProc()

        with patch("app.services.git_utils.asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            result = await run_git(Path("/repo"), ["rev-parse", "HEAD"])

        assert result == "sha123"
        assert captured_args[0] == "git"
        assert "rev-parse" in captured_args
        assert "HEAD" in captured_args

    @pytest.mark.asyncio
    async def test_git_uses_correct_repo_directory(self):
        """run_git passes cwd=repo_path to subprocess."""
        captured_cwd = []

        class FakeProc:
            returncode = 0
            async def communicate(self):
                return b"output\n", b""

        async def fake_subprocess(*args, **kwargs):
            captured_cwd.append(kwargs.get("cwd"))
            return FakeProc()

        repo_path = Path("/workspaces/ws-1/repo")
        with patch("app.services.git_utils.asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            await run_git(repo_path, ["status"])

        assert captured_cwd[0] == str(repo_path)
