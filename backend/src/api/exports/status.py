"""The run columns of an export target and how its stored run reads when no pass is live.

The read rules are docs/jira-sync/sync.md#triggers-and-runs: a live pass reads ``running``; a stored ``running`` with no
live pass reads ``failed`` / ``interrupted`` (``skipped`` / the destination's not-configured code when the target is
disabled). Destinations build their own status DTO from the ``RunState``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

SUCCEEDED, PARTIAL, FAILED, SKIPPED = "succeeded", "partial", "failed", "skipped"
INTERRUPTED: Final = "interrupted"  # a stored run left running by a stopped process
RUNNING: Final = "running"
IDLE: Final = "idle"


class RunColumns(Protocol):
    """The stored run of an export target (the ``last_*`` columns) and whether it is enabled."""

    @property
    def enabled(self) -> bool: ...
    @property
    def last_status(self) -> str | None: ...
    @property
    def last_finished_at(self) -> datetime | None: ...
    @property
    def last_error_code(self) -> str | None: ...
    @property
    def last_counts(self) -> Any: ...


@dataclass(frozen=True)
class RunState:
    """``finished_at``, ``error_code`` and ``counts`` are ``None`` unless a finished run is stored."""

    status: str
    finished_at: datetime | None = None
    error_code: str | None = None
    counts: Any = None


def run_state(target: RunColumns, *, live: bool, not_configured: str) -> RunState:
    """``live``: a pass of the project is queued or runs in this process."""

    stored = target.last_status
    if live:
        return RunState(RUNNING)
    if stored is None:
        return RunState(IDLE)
    if stored == RUNNING:  # the process stopped mid-run; recovery restarts an enabled target
        status, code = (FAILED, INTERRUPTED) if target.enabled else (SKIPPED, not_configured)
        return RunState(status, error_code=code)
    return RunState(stored, target.last_finished_at, target.last_error_code, target.last_counts or None)
