"""Optional agent-activity tail (self-host only).

In local self-host, agent logs are bind-mounted to ``./agent_logs`` (see
QUICKSTART § Configuration). When that directory is present this tails the most
relevant log file to show what the agent is doing right now — the most
Claude-Code-like feature. It is a *progressive enhancement*: every function
degrades to an empty result when the path is absent (e.g. hosted) so it never
becomes a hard dependency.

File I/O only — no network, no Textual import — so it stays simple and testable.
"""

from __future__ import annotations

from pathlib import Path

AGENT_LOGS_DIRNAME = "agent_logs"
_DEFAULT_TAIL_LINES = 8


def logs_dir(root: Path) -> Path:
    """Path to the agent-logs directory under the project root."""
    return root / AGENT_LOGS_DIRNAME


def find_log_for_workspace(directory: Path, workspace_id: str | None) -> Path | None:
    """Pick the log file for a workspace, or the most recently modified one.

    Logs are written NESTED (``agent_logs/<generation>/<workspace>-…phase<N>/<ts>.log``),
    so the search is recursive over ``*.log`` only — a stray top-level binary file
    (e.g. macOS ``.DS_Store``) must never be tailed into the activity panel.
    Prefers files whose path (relative to the logs dir) contains ``workspace_id``;
    falls back to the newest log. Returns None when the directory is missing or
    empty, or on any access error.
    """
    try:
        if not directory.is_dir():
            return None
        files = [p for p in directory.rglob("*.log") if p.is_file()]
        if not files:
            return None
        if workspace_id:
            matches = [p for p in files if workspace_id in str(p.relative_to(directory))]
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime)
        return max(files, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def tail(path: Path, lines: int = _DEFAULT_TAIL_LINES) -> list[str]:
    """Return the last ``lines`` non-empty lines of a file (best-effort)."""
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return []
    stripped = [ln.rstrip("\n") for ln in content.splitlines() if ln.strip()]
    return stripped[-lines:]


def recent_activity(
    root: Path,
    workspace_id: str | None = None,
    lines: int = _DEFAULT_TAIL_LINES,
) -> list[str]:
    """Tail the active workspace's agent log, or [] when unavailable."""
    log_path = find_log_for_workspace(logs_dir(root), workspace_id)
    if log_path is None:
        return []
    return tail(log_path, lines)
