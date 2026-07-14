"""Status polling and milestone-notification logic for the TUI.

Polling itself is a one-shot call (``poll_once``); the Textual app drives the
cadence with its own timer. ``MilestoneTracker`` is pure state-machine logic —
it remembers what it has already announced and returns the notifications to
fire when the checkpoint advances or the run reaches a terminal state. It does
no I/O, so it is unit-testable; the app passes its output to ``fire_milestones``
which reuses the shared ``notify_desktop`` helper.

This fixes the "fragile, end-only" notification problem: a desktop ping fires on
every checkpoint advance, not just at completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.cli_service import notify_desktop
from services.tool_helpers import check_status_safe
from tui.constants import CHECKPOINT_ORDER, TERMINAL_STATUSES

ACTIVE_STATUSES = {"pending", "initializing", "running"}


@dataclass(frozen=True)
class Milestone:
    """A desktop notification to fire once."""

    title: str
    message: str


@dataclass(frozen=True)
class WorkspacePhaseSnapshot:
    """One workspace's phase progress as seen in a status payload."""

    completed: int
    phase_name: str


async def poll_once(generation_id: str) -> dict[str, Any] | None:
    """Fetch the latest ``/status`` payload, or None on any error.

    Thin pass-through to the shared ``check_status_safe`` so the TUI and the
    CLI share one status-fetch path (never raises).
    """
    return await check_status_safe(generation_id)


def _checkpoint_index(checkpoint: str | None) -> int:
    try:
        return CHECKPOINT_ORDER.index(checkpoint or "")
    except ValueError:
        return -1


def _workspace_phase_snapshot(payload: dict[str, Any]) -> dict[str, WorkspacePhaseSnapshot]:
    workspace_phases = payload.get("workspace_phases")
    if not workspace_phases:
        workspace_phases = (payload.get("progress") or {}).get("workspace_phases") or {}

    snapshot: dict[str, WorkspacePhaseSnapshot] = {}
    for workspace_id, raw in workspace_phases.items():
        data = raw or {}
        try:
            completed = int(data.get("last_completed_phase") or 0)
        except (TypeError, ValueError):
            completed = 0
        phase_name = str(data.get("phase_name") or "").strip()
        snapshot[str(workspace_id)] = WorkspacePhaseSnapshot(
            completed=completed,
            phase_name=phase_name,
        )
    return snapshot


def _workspace_phase_message(
    short_generation_id: str,
    workspace_id: str,
    phase: WorkspacePhaseSnapshot,
) -> str:
    label = f"phase {phase.completed}" if phase.completed else "phase update"
    if phase.phase_name:
        label = f"{label}: {phase.phase_name}"
    return f"{short_generation_id}... {workspace_id} -> {label}"


class MilestoneTracker:
    """Emits milestone notifications on checkpoint advance and terminal state.

    Stateful but pure (no I/O): feed each polled payload to ``process`` and it
    returns the notifications that should fire this tick, each at most once.
    """

    def __init__(self, generation_id: str) -> None:
        self._gid = generation_id
        self._last_checkpoint_idx = -1
        self._terminal_announced = False
        self._last_status: str | None = None
        self._observed_payload = False
        self._workspace_phases: dict[str, WorkspacePhaseSnapshot] = {}

    def process(self, payload: dict[str, Any] | None) -> list[Milestone]:
        if not payload:
            return []

        milestones: list[Milestone] = []
        short = self._gid[:12]
        status = (payload.get("status") or "").lower()

        # Run start/queue — useful for local users with Slack/email disabled.
        # Only announce a genuine start: a fresh/queued run or a real transition
        # into an active state. Attaching to an already-running session (first
        # observed status == "running") is a silent baseline, mirroring the
        # checkpoint/workspace replay-suppression below.
        if status in ACTIVE_STATUSES and self._last_status not in ACTIVE_STATUSES:
            attaching_to_running = self._last_status is None and status == "running"
            if not attaching_to_running:
                phase = payload.get("current_phase") or payload.get("checkpoint") or status
                milestones.append(
                    Milestone("Generation started", f"{short}... {phase}")
                )

        # Checkpoint advance — one ping per newly reached checkpoint.
        idx = _checkpoint_index(payload.get("checkpoint"))
        if idx > self._last_checkpoint_idx:
            # Only announce advances after the first observed poll, so attaching
            # to an already-running session does not replay old checkpoints.
            if self._last_checkpoint_idx >= 0:
                phase = payload.get("current_phase") or payload.get("checkpoint") or ""
                milestones.append(Milestone("Milestone reached", f"{short}... -> {phase}"))
            self._last_checkpoint_idx = idx

        # Workspace phase progress — one ping per actual completed-phase advance.
        # Different status sources may project phase names differently; a label
        # flip with the same completed count is not user-visible progress.
        workspace_phases = _workspace_phase_snapshot(payload)
        for workspace_id, phase in sorted(workspace_phases.items()):
            previous = self._workspace_phases.get(workspace_id)
            if previous is None:
                if self._observed_payload:
                    milestones.append(
                        Milestone(
                            "Workspace phase progressed",
                            _workspace_phase_message(short, workspace_id, phase),
                        )
                    )
                continue
            if phase.completed > previous.completed:
                milestones.append(
                    Milestone(
                        "Workspace phase progressed",
                        _workspace_phase_message(short, workspace_id, phase),
                    )
                )
        if workspace_phases:
            self._workspace_phases.update(workspace_phases)

        # Terminal state — fire exactly once.
        if status in TERMINAL_STATUSES and not self._terminal_announced:
            self._terminal_announced = True
            if status == "completed":
                milestones.append(
                    Milestone("Generation completed", f"{short}... completed")
                )
            elif status == "cancelled":
                # User-initiated stop — announce neutrally, not as a failure.
                milestones.append(
                    Milestone("Generation cancelled", f"{short}... cancelled by user")
                )
            else:
                milestones.append(Milestone("Generation failed", f"{short}... failed"))
        self._last_status = status
        self._observed_payload = True
        return milestones


def fire_milestones(milestones: list[Milestone]) -> None:
    """Fire each milestone via the shared best-effort desktop notifier."""
    for m in milestones:
        notify_desktop(title=m.title, message=m.message)
