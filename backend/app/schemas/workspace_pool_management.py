"""Operator pool-management types shared by the read path and the reclaim path.

The listing endpoint has to tell an operator *whether* a workspace can be reclaimed and
why not; the reclaim endpoint has to *dispatch* on the same judgement. Both call
:func:`classify_reclaim`, so the badge shown in the UI and the action actually taken can
never disagree — and the blocked reason the user reads is the literal reason the check
refused (no second, prettier copy of the rule).

:func:`classify_reclaim` is pure and does no IO. It deliberately reads ``clean_verified``
off the document rather than inspecting the filesystem: that flag is what allocation
itself gates on (``allocate_workspace_set`` queries ``clean_verified == True``), and a
listing must not run a git subprocess per workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus


class ReclaimAction(str, Enum):
    """What reclaiming a given workspace would actually do.

    Each member maps to exactly one ``WorkspacePoolService`` primitive; see
    ``WorkspacePoolService.reclaim_workspace`` for the dispatch.
    """

    ALREADY_AVAILABLE = "already_available"
    FORCE_CLEAN = "force_clean"
    FINISH_CLEANING = "finish_cleaning"
    RELEASE_STUCK = "release_stuck"
    RELEASE_AND_CLEAN = "release_and_clean"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ReclaimPlan:
    """The outcome of classifying one workspace for reclaim."""

    action: ReclaimAction
    blocked_reason: Optional[str] = None
    retry_lost: bool = False

    @property
    def reclaimable(self) -> bool:
        """True if reclaiming would change anything and is permitted."""
        return self.action not in (ReclaimAction.BLOCKED, ReclaimAction.ALREADY_AVAILABLE)


@dataclass(frozen=True)
class RemovalCheck:
    """Whether a workspace may leave the pool, and why not."""

    removable: bool
    reason: Optional[str] = None


def classify_removal(workspace: Mapping[str, Any]) -> RemovalCheck:
    """Decide whether ``workspace`` may be removed from the pool (shrink).

    Only a clean, idle workspace may go: anything ALLOCATED/CLEANING/STUCK could still hold
    work, and an AVAILABLE-but-unverified one still has files on disk. Shared by the shrink
    service and the listing endpoint so the UI never offers a removal the server will refuse.

    Note this is stricter than "not in use" — it is the same ``AVAILABLE + clean_verified``
    pair that allocation requires, so a removable workspace is exactly an idle one.
    """
    workspace_id = workspace.get("id") or workspace.get("_id") or workspace.get("workspace_id")
    status = workspace.get("status")
    try:
        parsed = WorkspaceStatus(status)
    except ValueError:
        return RemovalCheck(False, f"{workspace_id} has an unrecognised status {status!r}")

    if parsed is not WorkspaceStatus.AVAILABLE:
        return RemovalCheck(False, f"{workspace_id} is {parsed.value}")
    if workspace.get("clean_verified") is not True:
        return RemovalCheck(False, f"{workspace_id} is not clean-verified")
    return RemovalCheck(True)


def classify_reclaim(
    workspace: Mapping[str, Any],
    owner_status: Optional[str] = None,
    owner_code_archived: Optional[bool] = None,
) -> ReclaimPlan:
    """Decide what reclaiming ``workspace`` would do.

    ``owner_status`` / ``owner_code_archived`` come from the generation named by
    ``locked_by`` and are only consulted for ALLOCATED workspaces. A missing owner
    document counts as terminal: the generation is gone, so nothing can still be running.

    An unrecognised workspace status is BLOCKED rather than assumed safe.
    """
    try:
        status = WorkspaceStatus(workspace.get("status"))
    except ValueError:
        return ReclaimPlan(
            ReclaimAction.BLOCKED,
            blocked_reason=f"Unrecognised workspace status {workspace.get('status')!r}.",
        )

    if status is WorkspaceStatus.CLEANING:
        return ReclaimPlan(ReclaimAction.FINISH_CLEANING)

    if status is WorkspaceStatus.STUCK:
        return ReclaimPlan(ReclaimAction.RELEASE_STUCK)

    if status is WorkspaceStatus.AVAILABLE:
        if workspace.get("clean_verified") is True:
            return ReclaimPlan(ReclaimAction.ALREADY_AVAILABLE)
        return ReclaimPlan(ReclaimAction.FORCE_CLEAN)

    # ALLOCATED — safe only when the owning generation has stopped for good.
    locked_by = workspace.get("locked_by")
    if locked_by and owner_status is not None and not GenerationStatus.is_terminal(owner_status):
        return ReclaimPlan(
            ReclaimAction.BLOCKED,
            blocked_reason=(
                f"Generation {locked_by} is {owner_status} — still using this workspace."
            ),
        )

    # Retry-from-checkpoint is only ever offered for FAILED runs (CANCELLED is never
    # retryable), and only while the code has not been archived yet.
    retry_lost = (
        owner_status == GenerationStatus.FAILED.value and owner_code_archived is False
    )
    return ReclaimPlan(ReclaimAction.RELEASE_AND_CLEAN, retry_lost=retry_lost)
