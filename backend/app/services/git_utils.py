"""
Shared async git helper used across all services.

Single implementation of the git subprocess call so workspace_pool,
workspace_manager, and git_archive_service all behave identically.
"""
import asyncio
import os
from pathlib import Path


class GitCommandError(Exception):
    """Raised when a git command exits with a non-zero return code."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str, stdout: str = "") -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(f"git {' '.join(cmd)} failed (exit {returncode}): {stderr.strip()}")


async def run_git(repo: Path, args: list[str]) -> str:
    """
    Run a git command in repo directory. Returns stdout (stripped) on success.
    Raises GitCommandError on non-zero exit code.

    GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS=echo prevent git from blocking on
    credential prompts in non-interactive environments.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"

    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GitCommandError(
            cmd=args,
            returncode=proc.returncode,
            stderr=stderr.decode(),
            stdout=stdout.decode(),
        )
    return stdout.decode().strip()
