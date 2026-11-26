"""Application-role data access for the Linear plan destination (``linear_targets``, ``linear_issues``).

Same rules as ``db.github_projects``: every function runs inside the caller's session and transaction, flushes and never
commits. Every statement filters on the project id it is given, except ``running_targets``, the one cross-project read.
Rows hold no credential and nothing here logs: issue content and identifiers stay out of the logs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LinearIssue, LinearTarget

# The columns ``update_item`` sets; one left out is not touched (None is a value there).
ITEM_COLUMNS: Final = (
    "issue_id",
    "identifier",
    "content",
    "content_hash",
    "position",
    "mirrored_state",
    "removed_at",
    "error_code",
    "synced_at",
)
BYTE_ORDER: Final = "C"
TARGETS_LIMIT: Final = 10_000  # the passes one process start recovers


@dataclass(frozen=True)
class LinearTargetView:
    """The target row: the team and the Linear project the issues go to, and its last run."""

    project_id: uuid.UUID
    team_id: str
    team_key: str
    linear_project_id: str
    linear_project_name: str
    linear_project_url: str | None
    created_by_graphico: bool
    enabled: bool
    updated_at: datetime
    last_status: str | None
    last_trigger: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_error_code: str | None
    last_counts: Any


@dataclass(frozen=True)
class ProjectChoice:
    """The Linear project of a target: its id, name and web address, and whether Graphico created it."""

    linear_project_id: str
    name: str
    url: str | None
    created_by_graphico: bool


@dataclass(frozen=True)
class TargetSaved:
    """What a ``save_target`` changed: ``destination_changed`` (the map was deleted), ``turned_on`` (disabled or new,
    now enabled) and ``keys_changed`` (new, or the team or the Linear project differ)."""

    destination_changed: bool
    turned_on: bool
    keys_changed: bool


def _view(target: LinearTarget) -> LinearTargetView:
    columns = {column.name: getattr(target, column.name) for column in LinearTarget.__table__.columns}
    return LinearTargetView(**columns)


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> LinearTargetView | None:
    statement = select(LinearTarget).where(LinearTarget.project_id == project_id)
    target = (await session.execute(statement)).scalar_one_or_none()
    return None if target is None else _view(target)


async def save_target(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    team_id: str,
    team_key: str,
    project: ProjectChoice,
    enabled: bool,
    now: datetime,
) -> TargetSaved:
    """Create or change the target. The row is locked ``FOR UPDATE`` first; a change of the team or of the Linear
    project deletes the project's map rows in the same transaction, so no pass writes a row of the old destination. A
    map left behind by a removed target is deleted too."""

    locked = (
        select(LinearTarget)
        .where(LinearTarget.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    before = (await session.execute(locked)).scalar_one_or_none()
    if before is None:
        await session.execute(delete(LinearIssue).where(LinearIssue.project_id == project_id))
        session.add(
            LinearTarget(
                project_id=project_id,
                team_id=team_id,
                team_key=team_key,
                linear_project_id=project.linear_project_id,
                linear_project_name=project.name,
                linear_project_url=project.url,
                created_by_graphico=project.created_by_graphico,
                enabled=enabled,
                updated_at=now,
            )
        )
        await session.flush()
        return TargetSaved(destination_changed=False, turned_on=enabled, keys_changed=True)
    changed = before.team_id != team_id or before.linear_project_id != project.linear_project_id
    turned_on = enabled and not before.enabled
    if changed:
        await session.execute(delete(LinearIssue).where(LinearIssue.project_id == project_id))
    if changed or before.enabled != enabled:
        before.updated_at = now
    before.team_id, before.team_key = team_id, team_key
    before.linear_project_id, before.linear_project_name = project.linear_project_id, project.name
    before.linear_project_url, before.created_by_graphico = project.url, project.created_by_graphico
    before.enabled = enabled
    await session.flush()
    return TargetSaved(destination_changed=changed, turned_on=turned_on, keys_changed=changed)


async def target_unchanged(
    session: AsyncSession, project_id: uuid.UUID, *, team_id: str, linear_project_id: str
) -> bool:
    """Lock the target row ``FOR SHARE``; true when it exists with this destination."""

    statement = (
        select(LinearTarget.team_id, LinearTarget.linear_project_id)
        .where(LinearTarget.project_id == project_id)
        .with_for_update(read=True)
    )
    row = (await session.execute(statement)).first()
    return row is not None and row.team_id == team_id and row.linear_project_id == linear_project_id


async def record_start(session: AsyncSession, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
    """A pass is queued: status ``running``; the end fields of the previous pass are cleared."""

    await session.execute(
        update(LinearTarget)
        .where(LinearTarget.project_id == project_id)
        .values(
            last_status="running",
            last_trigger=trigger,
            last_started_at=queued_at,
            last_finished_at=None,
            last_error_code=None,
            last_counts=None,
        )
    )


async def record_end(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    status: str,
    error_code: str | None,
    counts: Mapping[str, int],
    finished_at: datetime,
) -> None:
    await session.execute(
        update(LinearTarget)
        .where(LinearTarget.project_id == project_id)
        .values(last_status=status, last_error_code=error_code, last_counts=dict(counts), last_finished_at=finished_at)
    )


async def running_targets(session: AsyncSession) -> list[uuid.UUID]:
    """Enabled targets whose stored status is ``running``: the passes a restart interrupted."""

    statement = (
        select(LinearTarget.project_id)
        .where(LinearTarget.enabled.is_(True), LinearTarget.last_status == "running")
        .order_by(LinearTarget.project_id)
        .limit(TARGETS_LIMIT)
    )
    return list((await session.execute(statement)).scalars())


async def list_items(session: AsyncSession, project_id: uuid.UUID) -> list[LinearIssue]:
    statement = (
        select(LinearIssue)
        .where(LinearIssue.project_id == project_id)
        .order_by(LinearIssue.node_id.collate(BYTE_ORDER))
    )
    return list((await session.execute(statement)).scalars())


async def get_item(session: AsyncSession, project_id: uuid.UUID, node_id: str) -> LinearIssue | None:
    statement = select(LinearIssue).where(LinearIssue.project_id == project_id, LinearIssue.node_id == node_id)
    return (await session.execute(statement)).scalar_one_or_none()


async def add_pending(
    session: AsyncSession, project_id: uuid.UUID, *, node_id: str, kind: str, requested_at: datetime
) -> None:
    """Insert the row of a create about to be sent; a row still without an issue takes the new ``requested_at`` and
    loses the error of an earlier, refused create."""

    statement = pg_insert(LinearIssue).values(
        project_id=project_id, node_id=node_id, kind=kind, requested_at=requested_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("project_id", "node_id"),
            set_={"requested_at": statement.excluded.requested_at, "error_code": None},
            where=LinearIssue.issue_id.is_(None),
        )
    )


async def update_item(session: AsyncSession, project_id: uuid.UUID, node_id: str, **columns: Any) -> None:
    """Set the given ``ITEM_COLUMNS`` of the row; None clears one, a column left out is untouched."""

    if unknown := set(columns) - set(ITEM_COLUMNS):
        raise TypeError(f"not a column of linear_issues to update: {sorted(unknown)}")
    if columns:
        await session.execute(
            update(LinearIssue)
            .where(LinearIssue.project_id == project_id, LinearIssue.node_id == node_id)
            .values(columns)
        )
