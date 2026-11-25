"""Application-role data access for the GitHub Projects destination (docs/github/projects.md "Data").

Same rules as ``db.jira``: every function runs inside the caller's session and transaction, flushes and never commits.
Every statement filters on the project id it is given, except ``running_targets``, the one cross-project read. Rows
hold no credential and nothing here logs: issue content and numbers stay out of the logs.
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

from db.models import GitHubProjectItem, GitHubProjectTarget, ProjectRepository

# The columns ``update_item`` sets; one left out is not touched (None is a value there).
ITEM_COLUMNS: Final = (
    "issue_number",
    "issue_id",
    "issue_node_id",
    "item_id",
    "content",
    "content_hash",
    "position",
    "removed_at",
    "error_code",
    "synced_at",
)
BYTE_ORDER: Final = "C"
TARGETS_LIMIT: Final = 10_000  # the passes one process start recovers


@dataclass(frozen=True)
class ProjectTargetView:
    """The target row with the full name of its repository (``owner/name``, the issues' home)."""

    project_id: uuid.UUID
    repository_id: uuid.UUID
    repository_name: str
    project_node_id: str
    project_title: str
    project_url: str
    enabled: bool
    updated_at: datetime
    last_status: str | None
    last_trigger: str | None
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_error_code: str | None
    last_counts: Any
    connected_user_id: uuid.UUID | None


@dataclass(frozen=True)
class ProjectChoice:
    """The organization project of a target: its GraphQL node id, title and web address."""

    node_id: str
    title: str
    url: str


@dataclass(frozen=True)
class TargetSaved:
    """What a ``save_target`` changed: ``destination_changed`` (the map was deleted), ``turned_on`` (disabled or new,
    now enabled) and ``keys_changed`` (new, or the repository or the project differ)."""

    destination_changed: bool
    turned_on: bool
    keys_changed: bool


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> ProjectTargetView | None:
    statement = (
        select(GitHubProjectTarget, ProjectRepository.full_name)
        .join(ProjectRepository, ProjectRepository.id == GitHubProjectTarget.repository_id)
        .where(GitHubProjectTarget.project_id == project_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    target, name = row
    columns = {column.name: getattr(target, column.name) for column in GitHubProjectTarget.__table__.columns}
    return ProjectTargetView(repository_name=name, **columns)


async def save_target(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    repository_id: uuid.UUID,
    project: ProjectChoice,
    enabled: bool,
    now: datetime,
    connected_user_id: uuid.UUID | None,
) -> TargetSaved:
    """Create or change the target. The row is locked ``FOR UPDATE`` first; a change of the repository or the project
    deletes the project's map rows in the same transaction, so no pass writes a row of the old destination. A map left
    behind by a target the removal of its repository deleted is deleted too. ``connected_user_id`` is the member the
    project's GitHub is connected by: the account the export acts as."""

    locked = (
        select(GitHubProjectTarget)
        .where(GitHubProjectTarget.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    before = (await session.execute(locked)).scalar_one_or_none()
    if before is None:
        await session.execute(delete(GitHubProjectItem).where(GitHubProjectItem.project_id == project_id))
        session.add(
            GitHubProjectTarget(
                project_id=project_id,
                repository_id=repository_id,
                project_node_id=project.node_id,
                project_title=project.title,
                project_url=project.url,
                enabled=enabled,
                updated_at=now,
                connected_user_id=connected_user_id,
            )
        )
        await session.flush()
        return TargetSaved(destination_changed=False, turned_on=enabled, keys_changed=True)
    changed = before.repository_id != repository_id or before.project_node_id != project.node_id
    turned_on = enabled and not before.enabled
    if changed:
        await session.execute(delete(GitHubProjectItem).where(GitHubProjectItem.project_id == project_id))
    if changed or before.enabled != enabled:
        before.updated_at = now
    before.repository_id, before.project_node_id = repository_id, project.node_id
    before.project_title, before.project_url = project.title, project.url
    before.enabled, before.connected_user_id = enabled, connected_user_id
    await session.flush()
    return TargetSaved(destination_changed=changed, turned_on=turned_on, keys_changed=changed)


async def target_unchanged(
    session: AsyncSession, project_id: uuid.UUID, *, repository_id: uuid.UUID, project_node_id: str
) -> bool:
    """Lock the target row ``FOR SHARE``; true when it exists with this destination."""

    statement = (
        select(GitHubProjectTarget.repository_id, GitHubProjectTarget.project_node_id)
        .where(GitHubProjectTarget.project_id == project_id)
        .with_for_update(read=True)
    )
    row = (await session.execute(statement)).first()
    return row is not None and row.repository_id == repository_id and row.project_node_id == project_node_id


async def record_start(session: AsyncSession, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
    """A pass is queued: status ``running``; the end fields of the previous pass are cleared."""

    await session.execute(
        update(GitHubProjectTarget)
        .where(GitHubProjectTarget.project_id == project_id)
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
        update(GitHubProjectTarget)
        .where(GitHubProjectTarget.project_id == project_id)
        .values(last_status=status, last_error_code=error_code, last_counts=dict(counts), last_finished_at=finished_at)
    )


async def running_targets(session: AsyncSession) -> list[uuid.UUID]:
    """Enabled targets whose stored status is ``running``: the passes a restart interrupted."""

    statement = (
        select(GitHubProjectTarget.project_id)
        .where(GitHubProjectTarget.enabled.is_(True), GitHubProjectTarget.last_status == "running")
        .order_by(GitHubProjectTarget.project_id)
        .limit(TARGETS_LIMIT)
    )
    return list((await session.execute(statement)).scalars())


async def list_items(session: AsyncSession, project_id: uuid.UUID) -> list[GitHubProjectItem]:
    statement = (
        select(GitHubProjectItem)
        .where(GitHubProjectItem.project_id == project_id)
        .order_by(GitHubProjectItem.node_id.collate(BYTE_ORDER))
    )
    return list((await session.execute(statement)).scalars())


async def add_pending(
    session: AsyncSession, project_id: uuid.UUID, *, node_id: str, kind: str, requested_at: datetime
) -> None:
    """Insert the row of a create about to be sent; a row still without an issue takes the new ``requested_at`` and
    loses the error of an earlier, refused create."""

    statement = pg_insert(GitHubProjectItem).values(
        project_id=project_id, node_id=node_id, kind=kind, requested_at=requested_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("project_id", "node_id"),
            set_={"requested_at": statement.excluded.requested_at, "error_code": None},
            where=GitHubProjectItem.issue_number.is_(None),
        )
    )


async def update_item(session: AsyncSession, project_id: uuid.UUID, node_id: str, **columns: Any) -> None:
    """Set the given ``ITEM_COLUMNS`` of the row; None clears one, a column left out is untouched."""

    if unknown := set(columns) - set(ITEM_COLUMNS):
        raise TypeError(f"not a column of github_project_items to update: {sorted(unknown)}")
    if columns:
        await session.execute(
            update(GitHubProjectItem)
            .where(GitHubProjectItem.project_id == project_id, GitHubProjectItem.node_id == node_id)
            .values(columns)
        )
