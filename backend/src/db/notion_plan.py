"""Application-role data access for the Notion plan destination (docs/notion/plan.md "Data"). Stream E1 owns this file;
the functions below are final and complete (the same shapes as ``db.github_projects``).

Same rules as ``db.jira``: every function runs inside the caller's session and transaction, flushes and never commits.
Every statement filters on the project id it is given, except ``running_targets``, the one cross-project read. Rows hold
no credential and nothing here logs: titles and content stay out of the logs.
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

from db.models import NotionPlanItem, NotionPlanTarget, Project

# The columns ``update_item`` sets; one left out is not touched (None is a value there).
ITEM_COLUMNS: Final = ("page_id", "content", "content_hash", "removed_at", "error_code", "synced_at")
# The columns ``update_target`` sets: what the first pass learns of the database it made.
DATABASE_COLUMNS: Final = ("database_id", "data_source_id", "database_url")
BYTE_ORDER: Final = "C"
TARGETS_LIMIT: Final = 10_000  # the passes one process start recovers


@dataclass(frozen=True)
class PlanTargetView:
    """The target row: the parent page and, once the first pass made it, the database."""

    project_id: uuid.UUID
    project_name: str  # the project's own name (the plan database is titled after it)
    parent_page_id: str
    parent_page_title: str
    parent_page_url: str
    database_id: str | None
    data_source_id: str | None
    database_url: str | None
    enabled: bool
    updated_at: datetime
    last_status: str | None
    last_trigger: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_error_code: str | None
    last_counts: Any


@dataclass(frozen=True)
class ParentChoice:
    """The parent page of a target: its id, title and web address."""

    page_id: str
    title: str
    url: str


@dataclass(frozen=True)
class TargetSaved:
    """What a ``save_target`` changed: ``destination_changed`` (the map was deleted), ``turned_on`` (disabled or new,
    now enabled) and ``keys_changed`` (new, or the parent page differs)."""

    destination_changed: bool
    turned_on: bool
    keys_changed: bool


def _view(target: NotionPlanTarget, project_name: str) -> PlanTargetView:
    columns = {column.name: getattr(target, column.name) for column in NotionPlanTarget.__table__.columns}
    return PlanTargetView(project_name=project_name, **columns)


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> PlanTargetView | None:
    statement = (
        select(NotionPlanTarget, Project.name)
        .join(Project, Project.id == NotionPlanTarget.project_id)
        .where(NotionPlanTarget.project_id == project_id)
    )
    row = (await session.execute(statement)).first()
    return None if row is None else _view(*row)


async def save_target(
    session: AsyncSession, project_id: uuid.UUID, *, parent: ParentChoice, enabled: bool, now: datetime
) -> TargetSaved:
    """Create or change the target. The row is locked ``FOR UPDATE`` first; a change of the parent page clears the
    database columns and deletes the project's map rows in the same transaction, so no pass writes a row of the old
    destination (the next pass makes a new database)."""

    locked = (
        select(NotionPlanTarget)
        .where(NotionPlanTarget.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    before = (await session.execute(locked)).scalar_one_or_none()
    if before is None:
        await session.execute(delete(NotionPlanItem).where(NotionPlanItem.project_id == project_id))
        session.add(
            NotionPlanTarget(
                project_id=project_id,
                parent_page_id=parent.page_id,
                parent_page_title=parent.title,
                parent_page_url=parent.url,
                enabled=enabled,
                updated_at=now,
            )
        )
        await session.flush()
        return TargetSaved(destination_changed=False, turned_on=enabled, keys_changed=True)
    changed = before.parent_page_id != parent.page_id
    turned_on = enabled and not before.enabled
    if changed:
        await session.execute(delete(NotionPlanItem).where(NotionPlanItem.project_id == project_id))
        before.database_id = before.data_source_id = before.database_url = None
    if changed or before.enabled != enabled:
        before.updated_at = now
    before.parent_page_id, before.parent_page_title, before.parent_page_url = parent.page_id, parent.title, parent.url
    before.enabled = enabled
    await session.flush()
    return TargetSaved(destination_changed=changed, turned_on=turned_on, keys_changed=changed)


async def target_unchanged(session: AsyncSession, project_id: uuid.UUID, *, parent_page_id: str) -> bool:
    """Lock the target row ``FOR SHARE``; true when it exists with this parent page."""

    statement = (
        select(NotionPlanTarget.parent_page_id)
        .where(NotionPlanTarget.project_id == project_id)
        .with_for_update(read=True)
    )
    return (await session.execute(statement)).scalar_one_or_none() == parent_page_id


async def update_target(session: AsyncSession, project_id: uuid.UUID, *, parent_page_id: str, **columns: Any) -> bool:
    """Set the given ``DATABASE_COLUMNS`` while the target still has ``parent_page_id``; false when it changed."""

    if unknown := set(columns) - set(DATABASE_COLUMNS):
        raise TypeError(f"not a column of notion_plan_targets to update: {sorted(unknown)}")
    statement = (
        update(NotionPlanTarget)
        .where(NotionPlanTarget.project_id == project_id, NotionPlanTarget.parent_page_id == parent_page_id)
        .values(columns)
        .returning(NotionPlanTarget.project_id)
    )
    return (await session.execute(statement)).first() is not None


async def record_start(session: AsyncSession, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
    """A pass is queued: status ``running``; the end fields of the previous pass are cleared."""

    await session.execute(
        update(NotionPlanTarget)
        .where(NotionPlanTarget.project_id == project_id)
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
        update(NotionPlanTarget)
        .where(NotionPlanTarget.project_id == project_id)
        .values(last_status=status, last_error_code=error_code, last_counts=dict(counts), last_finished_at=finished_at)
    )


async def running_targets(session: AsyncSession) -> list[uuid.UUID]:
    """Enabled targets whose stored status is ``running``: the passes a restart interrupted."""

    statement = (
        select(NotionPlanTarget.project_id)
        .where(NotionPlanTarget.enabled.is_(True), NotionPlanTarget.last_status == "running")
        .order_by(NotionPlanTarget.project_id)
        .limit(TARGETS_LIMIT)
    )
    return list((await session.execute(statement)).scalars())


async def list_items(session: AsyncSession, project_id: uuid.UUID) -> list[NotionPlanItem]:
    statement = (
        select(NotionPlanItem)
        .where(NotionPlanItem.project_id == project_id)
        .order_by(NotionPlanItem.node_id.collate(BYTE_ORDER))
    )
    return list((await session.execute(statement)).scalars())


async def add_pending(
    session: AsyncSession, project_id: uuid.UUID, *, node_id: str, kind: str, requested_at: datetime
) -> None:
    """Insert the row of a create about to be sent; a row still without a page takes the new ``requested_at`` and loses
    the error of an earlier, refused create."""

    statement = pg_insert(NotionPlanItem).values(
        project_id=project_id, node_id=node_id, kind=kind, requested_at=requested_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("project_id", "node_id"),
            set_={"requested_at": statement.excluded.requested_at, "error_code": None},
            where=NotionPlanItem.page_id.is_(None),
        )
    )


async def update_item(session: AsyncSession, project_id: uuid.UUID, node_id: str, **columns: Any) -> None:
    """Set the given ``ITEM_COLUMNS`` of the row; None clears one, a column left out is untouched."""

    if unknown := set(columns) - set(ITEM_COLUMNS):
        raise TypeError(f"not a column of notion_plan_items to update: {sorted(unknown)}")
    if columns:
        await session.execute(
            update(NotionPlanItem)
            .where(NotionPlanItem.project_id == project_id, NotionPlanItem.node_id == node_id)
            .values(columns)
        )
