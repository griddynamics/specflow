from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0
DEFAULT_COMMAND_OUTPUT_CHARS = 20_000
DEFAULT_SNAPSHOT_FILE_CHARS = 12_000
DEFAULT_SNAPSHOT_TOTAL_CHARS = 80_000
_SKIPPED_SNAPSHOT_PARTS = frozenset({".git", "__pycache__"})
_FAILED_COMMAND_RETURNCODE = -1


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    """Captured result of one local command used as evaluation evidence."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_COMMAND_OUTPUT_CHARS,
) -> CommandEvidence:
    """Run a command without raising, clipping captured output for reports."""

    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandEvidence(
            command=tuple(command),
            returncode=_FAILED_COMMAND_RETURNCODE,
            stdout=_clipped_output(exc.stdout, max_output_chars),
            stderr=_clipped_output(
                exc.stderr or f"command timed out after {timeout:g} seconds",
                max_output_chars,
            ),
        )
    except OSError as exc:
        return CommandEvidence(
            command=tuple(command),
            returncode=_FAILED_COMMAND_RETURNCODE,
            stdout="",
            stderr=str(exc),
        )
    return CommandEvidence(
        command=tuple(command),
        returncode=completed.returncode,
        stdout=_clipped_output(completed.stdout, max_output_chars),
        stderr=_clipped_output(completed.stderr, max_output_chars),
    )


def snapshot_text_files(
    root: Path,
    *,
    max_file_chars: int = DEFAULT_SNAPSHOT_FILE_CHARS,
    max_total_chars: int = DEFAULT_SNAPSHOT_TOTAL_CHARS,
) -> dict[str, str]:
    """Collect a bounded UTF-8 snapshot of files under ``root``."""

    snapshot: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _SKIPPED_SNAPSHOT_PARTS.intersection(path.parts) or path.suffix == ".pyc":
            continue
        relative = str(path.relative_to(root))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        clipped = content[:max_file_chars]
        if total + len(clipped) > max_total_chars:
            break
        snapshot[relative] = clipped
        total += len(clipped)
    return snapshot


def ensure_git_workspace(
    workspace: Path,
    *,
    user_name: str = "LLM Workspace",
    user_email: str = "workspace@example.invalid",
    commit_message: str = "Initialize coding workspace",
) -> None:
    """Create ``workspace`` and initialize an empty Git repository if needed."""

    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").is_dir():
        return
    commands = (
        ("git", "init"),
        ("git", "add", "."),
        (
            "git",
            "-c",
            f"user.name={user_name}",
            "-c",
            f"user.email={user_email}",
            "commit",
            "--allow-empty",
            "-m",
            commit_message,
        ),
    )
    for command in commands:
        evidence = run_command(command, cwd=workspace)
        if not evidence.passed:
            raise RuntimeError(f"workspace initialization failed: {command!r}\n{evidence.stderr}")


def _clipped_output(value: str | bytes | None, max_chars: int) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[-max_chars:]
