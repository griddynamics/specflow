"""What the Linear plan runner exposes: the pure status and item builders. The read rules are
``api.exports.status``: a live pass reads ``running``; a stored ``running`` with no live pass reads ``failed`` /
``interrupted`` (``skipped`` / ``linear_not_configured`` when the target is disabled)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, cast

from agent_contracts.linear_models import (
    LinearItemKind,
    LinearSyncCounts,
    LinearSyncItem,
    LinearSyncState,
    LinearSyncStatus,
)
from api.exports.status import run_state
from api.linear_plan.sync import NOT_CONFIGURED
from db.linear_plan import LinearTargetView
from db.models import LINEAR_ISSUE_KINDS, LinearIssue

_WORKSPACE: Final = re.compile(r"^(https://linear\.app/[^/]+)/")


def status_of(target: LinearTargetView, *, live: bool, queued: bool) -> LinearSyncStatus:
    """The sync status of a target. ``live``: a pass of the project is queued or runs in this process; ``queued``:
    another pass follows it."""

    run = run_state(target, live=live, not_configured=NOT_CONFIGURED)
    return LinearSyncStatus(
        project_id=str(target.project_id),
        enabled=target.enabled,
        status=cast(LinearSyncState, run.status),
        queued=live and queued,
        trigger=target.last_trigger,
        started_at=target.last_started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
        counts=LinearSyncCounts(**run.counts) if run.counts else None,
        team_key=target.team_key,
        linear_project=target.linear_project_name,
        linear_project_url=target.linear_project_url,
    )


def not_configured(project_id: str, target: LinearTargetView | None) -> LinearSyncStatus:
    """The answer of a start with no enabled target: nothing was queued."""

    return LinearSyncStatus(
        project_id=project_id,
        enabled=False,
        status="skipped",
        queued=False,
        trigger=None,
        started_at=None,
        finished_at=None,
        error_code=NOT_CONFIGURED,
        counts=None,
        team_key="" if target is None else target.team_key,
        linear_project="" if target is None else target.linear_project_name,
        linear_project_url=None if target is None else target.linear_project_url,
    )


def issue_url(project_url: str | None, identifier: str | None) -> str | None:
    """The web address of an issue: Linear redirects ``/<workspace>/issue/<identifier>``; the workspace is the one of
    the project's address."""

    found = _WORKSPACE.match(project_url or "")
    return f"{found.group(1)}/issue/{identifier}" if found and identifier else None


def items_of(rows: Iterable[LinearIssue], project_url: str | None) -> list[LinearSyncItem]:
    """The map rows as items, ordered by kind (story, subtask) then node id."""

    order = {kind: index for index, kind in enumerate(LINEAR_ISSUE_KINDS)}
    items = [
        LinearSyncItem(
            node_id=row.node_id,
            kind=cast(LinearItemKind, row.kind),
            identifier=row.identifier,
            url=issue_url(project_url, row.identifier),
            removed=row.removed_at is not None,
            error_code=row.error_code,
        )
        for row in rows
    ]
    return sorted(items, key=lambda item: (order[item.kind], item.node_id))
