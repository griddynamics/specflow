"""Application-role data access for the Jira sync rows (docs/jira-sync/data-model.md).

Same rules as ``db.gap``: every function runs inside the caller's session and transaction, flushes and never commits.
Every statement filters on the project id it is given, except ``running_targets``, the one cross-project read. Rows
hold no credential and nothing here logs: issue content and keys stay out of the logs.
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

from db.models import JiraIssue, JiraTarget, Project

# The columns ``update_issue`` sets; one left out is not touched (None is a value there).
ISSUE_COLUMNS: Final = ("jira_key", "content", "content_hash", "position", "removed_at", "error_code", "synced_at")
BYTE_ORDER: Final = "C"
TARGETS_LIMIT: Final = 10_000  # the passes one process start recovers


@dataclass(frozen=True)
class JiraTargetView:
    """The target row with the project's name (the Epic summary when Specflow creates the Epic)."""

    project_id: uuid.UUID
    project_name: str
    jira_project: str
    epic_key: str | None
    enabled: bool
    updated_at: datetime
    last_status: str | None
    last_trigger: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_error_code: str | None
    last_counts: Any


@dataclass(frozen=True)
class TargetSaved:
    """What a ``save_target`` changed: ``project_changed`` (the map was deleted), ``turned_on`` (disabled or new, now
    enabled) and ``keys_changed`` (new, or ``jira_project`` or ``epic_key`` differ)."""

    project_changed: bool
    turned_on: bool
    keys_changed: bool


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> JiraTargetView | None:
    statement = (
        select(JiraTarget, Project.name)
        .join(Project, Project.id == JiraTarget.project_id)
        .where(JiraTarget.project_id == project_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    target, name = row
    columns = {column.name: getattr(target, column.name) for column in JiraTarget.__table__.columns}
    return JiraTargetView(project_name=name, **columns)


async def save_target(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    jira_project: str,
    epic_key: str | None,
    enabled: bool,
    now: datetime,
) -> TargetSaved:
    """Create or change the target. The row is locked ``FOR UPDATE`` first; a ``jira_project`` change deletes the
    project's map rows in the same transaction (FR-JIRA-CFG-04), so no pass writes a row of the old Jira project.

    Lock order: call this before any other lock of the project, never after ``lock_project`` (a pass holds the target
    ``FOR SHARE`` and then needs ``projects`` ``FOR KEY SHARE``; the other order is a deadlock)."""

    locked = (
        select(JiraTarget)
        .where(JiraTarget.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)  # a JiraTarget this session loaded earlier must show the locked row
    )
    before = (await session.execute(locked)).scalar_one_or_none()
    if before is None:
        session.add(
            JiraTarget(
                project_id=project_id, jira_project=jira_project, epic_key=epic_key, enabled=enabled, updated_at=now
            )
        )
        await session.flush()
        return TargetSaved(project_changed=False, turned_on=enabled, keys_changed=True)
    project_changed = before.jira_project != jira_project
    keys_changed = project_changed or before.epic_key != epic_key
    turned_on = enabled and not before.enabled
    if project_changed:
        await session.execute(delete(JiraIssue).where(JiraIssue.project_id == project_id))
    if keys_changed or before.enabled != enabled:
        before.updated_at = now
    before.jira_project, before.epic_key, before.enabled = jira_project, epic_key, enabled
    await session.flush()
    return TargetSaved(project_changed=project_changed, turned_on=turned_on, keys_changed=keys_changed)


async def target_unchanged(
    session: AsyncSession, project_id: uuid.UUID, *, jira_project: str, epic_key: str | None
) -> bool:
    """Lock the target row ``FOR SHARE``; true when it exists with these keys (FR-JIRA-RUN-08)."""

    statement = (
        select(JiraTarget.jira_project, JiraTarget.epic_key)
        .where(JiraTarget.project_id == project_id)
        .with_for_update(read=True)
    )
    row = (await session.execute(statement)).first()
    return row is not None and row.jira_project == jira_project and row.epic_key == epic_key


async def record_start(session: AsyncSession, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
    """A pass is queued: status ``running``; the end fields of the previous pass are cleared."""

    await session.execute(
        update(JiraTarget)
        .where(JiraTarget.project_id == project_id)
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
        update(JiraTarget)
        .where(JiraTarget.project_id == project_id)
        .values(last_status=status, last_error_code=error_code, last_counts=dict(counts), last_finished_at=finished_at)
    )


async def running_targets(session: AsyncSession) -> list[uuid.UUID]:
    """Enabled targets whose stored status is ``running``: the passes a restart interrupted."""

    statement = (
        select(JiraTarget.project_id)
        .where(JiraTarget.enabled.is_(True), JiraTarget.last_status == "running")
        .order_by(JiraTarget.project_id)
        .limit(TARGETS_LIMIT)
    )
    return list((await session.execute(statement)).scalars())


async def list_issues(session: AsyncSession, project_id: uuid.UUID) -> list[JiraIssue]:
    statement = (
        select(JiraIssue).where(JiraIssue.project_id == project_id).order_by(JiraIssue.node_id.collate(BYTE_ORDER))
    )
    return list((await session.execute(statement)).scalars())


async def add_pending(
    session: AsyncSession, project_id: uuid.UUID, *, node_id: str, kind: str, requested_at: datetime
) -> None:
    """Insert the row of a create about to be sent; a row still without a key takes the new ``requested_at`` and
    loses the error of an earlier, refused create."""

    statement = pg_insert(JiraIssue).values(
        project_id=project_id, node_id=node_id, kind=kind, requested_at=requested_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("project_id", "node_id"),
            set_={"requested_at": statement.excluded.requested_at, "error_code": None},
            where=JiraIssue.jira_key.is_(None),
        )
    )


async def update_issue(session: AsyncSession, project_id: uuid.UUID, node_id: str, **columns: Any) -> None:
    """Set the given ``ISSUE_COLUMNS`` of the row; None clears one, a column left out is untouched."""

    if unknown := set(columns) - set(ISSUE_COLUMNS):
        raise TypeError(f"not a column of jira_issues to update: {sorted(unknown)}")
    if columns:
        await session.execute(
            update(JiraIssue).where(JiraIssue.project_id == project_id, JiraIssue.node_id == node_id).values(columns)
        )


async def set_positions(session: AsyncSession, project_id: uuid.UUID, positions: Mapping[str, int]) -> None:
    """Set the rank position of each given node id (the rows of the project only)."""

    for node_id, position in positions.items():
        await update_issue(session, project_id, node_id, position=position)
