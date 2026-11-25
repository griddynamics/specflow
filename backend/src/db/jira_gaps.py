"""Application-role data access for the Jira gap channel (migration ``0009_jira_gaps``, docs/jira-sync/gaps.md).

Same rules as ``db.jira``: every function runs inside the caller's session and transaction, flushes and never commits,
and every statement filters on the project it is given, except ``enabled_targets``, the one cross-project read. Rows
hold no credential and no Jira text; nothing here logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AtlassianConnection, JiraGapIssue, JiraGapTarget, ProjectMember

TARGETS_LIMIT = 10_000  # the targets one process polls


class DestinationChangedError(Exception):
    """The Jira project of the target changed (or the target went) since a pass read it: stop, write nothing more."""


@dataclass(frozen=True)
class GapTargetView:
    """The target row of a project's Jira gap channel."""

    project_id: uuid.UUID
    jira_project: str
    grouping: str
    story_key: str | None
    enabled: bool
    updated_at: datetime
    poll_cursor: datetime | None
    last_polled_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True)
class GapTargetSaved:
    """What ``save_target`` changed: ``turned_on`` (new or disabled, now enabled) and ``project_changed`` (the Jira
    project differs: the map, the Story and the cursor were dropped, so the open questions are posted again)."""

    turned_on: bool
    project_changed: bool


@dataclass(frozen=True)
class GapIssueView:
    """A gap posted to Jira: its sub-task, the group of the sub-task and its number in it."""

    project_id: uuid.UUID
    gap_id: str
    issue_key: str
    area: str
    number: int
    posted_at: datetime
    answered_at: datetime | None


def _target(row: JiraGapTarget) -> GapTargetView:
    return GapTargetView(
        row.project_id,
        row.jira_project,
        row.grouping,
        row.story_key,
        row.enabled,
        row.updated_at,
        row.poll_cursor,
        row.last_polled_at,
        row.last_error_code,
    )


def _issue(row: JiraGapIssue) -> GapIssueView:
    return GapIssueView(row.project_id, row.gap_id, row.issue_key, row.area, row.number, row.posted_at, row.answered_at)


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> GapTargetView | None:
    row = await session.get(JiraGapTarget, project_id)
    return None if row is None else _target(row)


async def enabled_targets(session: AsyncSession) -> list[GapTargetView]:
    """Every enabled target (the one cross-project read: the poller's loop)."""

    statement = select(JiraGapTarget).where(JiraGapTarget.enabled.is_(True)).limit(TARGETS_LIMIT)
    return [_target(row) for row in (await session.execute(statement)).scalars()]


async def save_target(
    session: AsyncSession, project_id: uuid.UUID, *, jira_project: str, grouping: str, enabled: bool, now: datetime
) -> GapTargetSaved:
    """Create or change the target. A ``jira_project`` change deletes the project's map rows and forgets the Story and
    the cursor in the same transaction, so no row names an issue of the old Jira project."""

    locked = select(JiraGapTarget).where(JiraGapTarget.project_id == project_id).with_for_update()
    before = (await session.execute(locked.execution_options(populate_existing=True))).scalar_one_or_none()
    if before is None:
        session.add(
            JiraGapTarget(
                project_id=project_id, jira_project=jira_project, grouping=grouping, enabled=enabled, updated_at=now
            )
        )
        await session.flush()
        return GapTargetSaved(turned_on=enabled, project_changed=False)
    project_changed = before.jira_project != jira_project
    turned_on = enabled and not before.enabled
    if project_changed:
        await session.execute(delete(JiraGapIssue).where(JiraGapIssue.project_id == project_id))
        before.story_key, before.poll_cursor, before.last_error_code = None, None, None
    if project_changed or before.grouping != grouping or before.enabled != enabled:
        before.updated_at = now
    before.jira_project, before.grouping, before.enabled = jira_project, grouping, enabled
    await session.flush()
    return GapTargetSaved(turned_on=turned_on, project_changed=project_changed)


async def require_destination(session: AsyncSession, project_id: uuid.UUID, jira_project: str) -> None:
    """Lock the target row ``FOR SHARE`` and require that it still names ``jira_project``: the check a pass makes in
    the transaction of each write, so it cannot put data of the old Jira project on a changed target.
    ``DestinationChangedError`` otherwise."""

    statement = (
        select(JiraGapTarget.jira_project).where(JiraGapTarget.project_id == project_id).with_for_update(read=True)
    )
    if (await session.execute(statement)).scalar_one_or_none() != jira_project:
        raise DestinationChangedError


async def set_story(session: AsyncSession, project_id: uuid.UUID, story_key: str) -> None:
    await session.execute(
        update(JiraGapTarget).where(JiraGapTarget.project_id == project_id).values(story_key=story_key)
    )


async def record_poll(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    cursor: datetime | None,
    now: datetime,
    error_code: str | None,
) -> None:
    """The result of one poll: a ``cursor`` that is not ``None`` replaces the stored one; ``error_code`` is the closed
    code of a refused poll (``None`` after a good one)."""

    values: dict[str, object] = {"last_polled_at": now, "last_error_code": error_code}
    if cursor is not None:
        values["poll_cursor"] = cursor
    await session.execute(update(JiraGapTarget).where(JiraGapTarget.project_id == project_id).values(**values))


async def record_error(session: AsyncSession, project_id: uuid.UUID, error_code: str | None) -> None:
    """The closed code of the last refused post (``None``: the last post went through)."""

    await session.execute(
        update(JiraGapTarget).where(JiraGapTarget.project_id == project_id).values(last_error_code=error_code)
    )


async def list_issues(session: AsyncSession, project_id: uuid.UUID) -> list[GapIssueView]:
    statement = select(JiraGapIssue).where(JiraGapIssue.project_id == project_id).order_by(JiraGapIssue.posted_at)
    return [_issue(row) for row in (await session.execute(statement)).scalars()]


async def add_issue(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    gap_id: str,
    issue_key: str,
    area: str,
    number: int,
    now: datetime,
) -> bool:
    """Record that ``gap_id`` was posted; ``False`` when it already was (the first row stays)."""

    statement = (
        pg_insert(JiraGapIssue)
        .values(project_id=project_id, gap_id=gap_id, issue_key=issue_key, area=area, number=number, posted_at=now)
        .on_conflict_do_nothing()
        .returning(JiraGapIssue.gap_id)
    )
    return (await session.execute(statement)).first() is not None


async def mark_answered(session: AsyncSession, project_id: uuid.UUID, gap_id: str, *, now: datetime) -> bool:
    """Set ``answered_at`` once; ``False`` when it was set already or the gap was never posted."""

    statement = (
        update(JiraGapIssue)
        .where(
            JiraGapIssue.project_id == project_id,
            JiraGapIssue.gap_id == gap_id,
            JiraGapIssue.answered_at.is_(None),
        )
        .values(answered_at=now)
        .returning(JiraGapIssue.gap_id)
    )
    return (await session.execute(statement)).first() is not None


async def member_accounts(session: AsyncSession, project_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """The Atlassian account id of each member of the project that has a connection, to the member."""

    statement = (
        select(AtlassianConnection.account_id, AtlassianConnection.user_id)
        .join(ProjectMember, ProjectMember.user_id == AtlassianConnection.user_id)
        .where(ProjectMember.project_id == project_id)
    )
    return {row[0]: row[1] for row in (await session.execute(statement)).all()}


async def account_of(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """The Atlassian account id of the user's connection, or ``None`` without one."""

    row = await session.get(AtlassianConnection, user_id)
    return None if row is None else row.account_id
