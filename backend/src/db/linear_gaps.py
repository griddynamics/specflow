"""Application-role data access for the Linear gap channel (migration ``0011_linear``, docs/linear/gaps.md).

Same rules as ``db.jira_gaps``: every function runs inside the caller's session and transaction, flushes and never
commits, and every statement filters on the project it is given, except the cross-project reads the webhook and the
start need (``enabled_targets``, ``issue_rows``, ``member_of``, ``revoke_connections``). Rows hold no credential and
no Linear text; nothing here logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LinearConnection, LinearGapIssue, LinearGapTarget, Project, ProjectIntegration, ProjectMember

TARGETS_LIMIT = 10_000  # the targets one process reads at start
ROWS_LIMIT = 1_000  # the gap rows one Linear issue can hold


class DestinationChangedError(Exception):
    """The team or Linear project of the target changed (or the target went) since a pass read it: stop, write nothing
    more."""


@dataclass(frozen=True)
class GapTargetView:
    """The target row of a project's Linear gap channel."""

    project_id: uuid.UUID
    team_id: str
    team_key: str
    linear_project_id: str | None
    grouping: str
    parent_issue_id: uuid.UUID | None
    parent_issue_identifier: str | None
    enabled: bool
    updated_at: datetime
    last_error_code: str | None


@dataclass(frozen=True)
class GapTargetSaved:
    """What ``save_target`` changed: ``turned_on`` (new or disabled, now enabled) and ``destination_changed`` (the team
    or Linear project differs: the rows and the parent issue were dropped, so the open questions are posted again)."""

    turned_on: bool
    destination_changed: bool


@dataclass(frozen=True)
class GapIssueView:
    """A gap posted to Linear: its issue, the group of the issue and its number in it."""

    project_id: uuid.UUID
    gap_id: str
    issue_id: uuid.UUID
    identifier: str
    area: str
    number: int
    posted_at: datetime
    answered_at: datetime | None


def _target(row: LinearGapTarget) -> GapTargetView:
    return GapTargetView(
        row.project_id,
        row.team_id,
        row.team_key,
        row.linear_project_id,
        row.grouping,
        row.parent_issue_id,
        row.parent_issue_identifier,
        row.enabled,
        row.updated_at,
        row.last_error_code,
    )


def _issue(row: LinearGapIssue) -> GapIssueView:
    return GapIssueView(
        row.project_id, row.gap_id, row.issue_id, row.identifier, row.area, row.number, row.posted_at, row.answered_at
    )


async def get_target(session: AsyncSession, project_id: uuid.UUID) -> GapTargetView | None:
    row = await session.get(LinearGapTarget, project_id)
    return None if row is None else _target(row)


async def enabled_targets(session: AsyncSession) -> list[GapTargetView]:
    """Every enabled target (a cross-project read: the catch-up at start)."""

    statement = select(LinearGapTarget).where(LinearGapTarget.enabled.is_(True)).limit(TARGETS_LIMIT)
    return [_target(row) for row in (await session.execute(statement)).scalars()]


async def save_target(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    team_id: str,
    team_key: str,
    linear_project_id: str | None,
    grouping: str,
    enabled: bool,
    now: datetime,
) -> GapTargetSaved:
    """Create or change the target. A team or Linear project change deletes the project's map rows and forgets the
    parent issue in the same transaction, so no row names an issue of the old destination."""

    locked = select(LinearGapTarget).where(LinearGapTarget.project_id == project_id).with_for_update()
    before = (await session.execute(locked.execution_options(populate_existing=True))).scalar_one_or_none()
    if before is None:
        session.add(
            LinearGapTarget(
                project_id=project_id,
                team_id=team_id,
                team_key=team_key,
                linear_project_id=linear_project_id,
                grouping=grouping,
                enabled=enabled,
                updated_at=now,
            )
        )
        await session.flush()
        return GapTargetSaved(turned_on=enabled, destination_changed=False)
    changed = before.team_id != team_id or before.linear_project_id != linear_project_id
    turned_on = enabled and not before.enabled
    if changed:
        await session.execute(delete(LinearGapIssue).where(LinearGapIssue.project_id == project_id))
        before.parent_issue_id, before.parent_issue_identifier, before.last_error_code = None, None, None
    if changed or before.grouping != grouping or before.enabled != enabled:
        before.updated_at = now
    before.team_id, before.team_key, before.linear_project_id = team_id, team_key, linear_project_id
    before.grouping, before.enabled = grouping, enabled
    await session.flush()
    return GapTargetSaved(turned_on=turned_on, destination_changed=changed)


async def require_destination(
    session: AsyncSession, project_id: uuid.UUID, *, team_id: str, linear_project_id: str | None
) -> None:
    """Lock the target row ``FOR NO KEY UPDATE`` and require that it still names this team and Linear project: the check
    a pass makes in the transaction of each write, so it cannot put data of the old destination on a changed target. It
    is the lock level of the target updates that follow (``set_parent``), so the lock is never upgraded.
    ``DestinationChangedError`` otherwise."""

    statement = (
        select(LinearGapTarget.team_id, LinearGapTarget.linear_project_id)
        .where(LinearGapTarget.project_id == project_id)
        .with_for_update(key_share=True)
    )
    found = (await session.execute(statement)).first()
    if found is None or (found[0], found[1]) != (team_id, linear_project_id):
        raise DestinationChangedError


async def set_parent(session: AsyncSession, project_id: uuid.UUID, issue_id: uuid.UUID, identifier: str) -> None:
    await session.execute(
        update(LinearGapTarget)
        .where(LinearGapTarget.project_id == project_id)
        .values(parent_issue_id=issue_id, parent_issue_identifier=identifier)
    )


async def record_error(session: AsyncSession, project_id: uuid.UUID, error_code: str | None) -> None:
    """The closed code of the last refused post or read (``None``: the last one went through)."""

    await session.execute(
        update(LinearGapTarget).where(LinearGapTarget.project_id == project_id).values(last_error_code=error_code)
    )


async def list_issues(session: AsyncSession, project_id: uuid.UUID) -> list[GapIssueView]:
    statement = select(LinearGapIssue).where(LinearGapIssue.project_id == project_id).order_by(LinearGapIssue.posted_at)
    return [_issue(row) for row in (await session.execute(statement)).scalars()]


async def issue_rows(session: AsyncSession, issue_id: uuid.UUID) -> list[GapIssueView]:
    """The gaps posted in one Linear issue, of any project (the webhook knows only the issue): empty for an issue the
    channel did not post."""

    statement = (
        select(LinearGapIssue)
        .where(LinearGapIssue.issue_id == issue_id)
        .order_by(LinearGapIssue.number)
        .limit(ROWS_LIMIT)
    )
    return [_issue(row) for row in (await session.execute(statement)).scalars()]


async def add_issue(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    gap_id: str,
    issue_id: uuid.UUID,
    identifier: str,
    area: str,
    number: int,
    now: datetime,
) -> bool:
    """Record that ``gap_id`` was posted; ``False`` when it already was (the first row stays)."""

    statement = (
        pg_insert(LinearGapIssue)
        .values(
            project_id=project_id,
            gap_id=gap_id,
            issue_id=issue_id,
            identifier=identifier,
            area=area,
            number=number,
            posted_at=now,
        )
        .on_conflict_do_nothing()
        .returning(LinearGapIssue.gap_id)
    )
    return (await session.execute(statement)).first() is not None


async def mark_answered(session: AsyncSession, project_id: uuid.UUID, gap_id: str, *, now: datetime) -> bool:
    """Set ``answered_at`` once; ``False`` when it was set already or the gap was never posted."""

    statement = (
        update(LinearGapIssue)
        .where(
            LinearGapIssue.project_id == project_id,
            LinearGapIssue.gap_id == gap_id,
            LinearGapIssue.answered_at.is_(None),
        )
        .values(answered_at=now)
        .returning(LinearGapIssue.gap_id)
    )
    return (await session.execute(statement)).first() is not None


async def member_of(
    session: AsyncSession, project_id: uuid.UUID, *, organization_id: str, linear_user_id: str
) -> uuid.UUID | None:
    """The member of the project whose (not revoked) Linear connection is this Linear user of this workspace; ``None``
    for anyone else."""

    statement = (
        select(LinearConnection.user_id)
        .join(ProjectMember, ProjectMember.user_id == LinearConnection.user_id)
        .where(
            ProjectMember.project_id == project_id,
            LinearConnection.organization_id == organization_id,
            LinearConnection.linear_user_id == linear_user_id,
            LinearConnection.status != "revoked",
        )
        .order_by(LinearConnection.user_id)
        .limit(1)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def revoke_connections(session: AsyncSession, *, organization_id: str, linear_user_id: str | None) -> int:
    """Mark the connections of a workspace (or of one user in it) ``revoked``: the app's authorization was withdrawn in
    Linear. How many rows changed."""

    statement = update(LinearConnection).where(
        LinearConnection.organization_id == organization_id, LinearConnection.status != "revoked"
    )
    if linear_user_id is not None:
        statement = statement.where(LinearConnection.linear_user_id == linear_user_id)
    done = await session.execute(statement.values(status="revoked").returning(LinearConnection.user_id))
    return len(done.all())


async def url_key(session: AsyncSession, project_id: uuid.UUID) -> str | None:
    """The workspace URL key (``graphico-app`` in ``linear.app/graphico-app``) of the member the project's Linear is
    connected by: the first part of an issue link. ``None`` without a binding or a connection."""

    statement = (
        select(LinearConnection.organization_url_key)
        .join(ProjectIntegration, ProjectIntegration.user_id == LinearConnection.user_id)
        .where(ProjectIntegration.project_id == project_id, ProjectIntegration.app == "linear")
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def project_name(session: AsyncSession, project_id: uuid.UUID) -> str | None:
    """The Graphico project's name, for the title of the parent issue; ``None`` when the project is gone."""

    project = await session.get(Project, project_id)
    return None if project is None else project.name
