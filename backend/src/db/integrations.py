"""Application-role data access for the project integrations (migration ``0008_project_integrations``).

A row binds a member's per-user connection (Atlassian, GitHub) or Slack channel binding to a project: the member is the
account every use of the app in the project acts as, and the row names the project's one workspace (Atlassian site,
GitHub account, Slack workspace). The acting account the runners read stays where it was (``jira_targets``
``connected_user_id``, ``project_repositories`` ``added_by``): ``point_*`` copies the bound member there.

Same rules as ``db.atlassian``: every function runs inside the caller's session and transaction, flushes and never
commits; every statement filters on the project it is given. Nothing here holds or logs a token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AtlassianConnection, GitHubConnection, JiraTarget, ProjectIntegration, ProjectRepository, User

ATLASSIAN, GITHUB, SLACK = "atlassian", "github", "slack"


class WorkspaceMismatchError(Exception):
    """The project is bound to another workspace of this app (one organisation per app per project)."""


class BindingHeldError(Exception):
    """Another member's active connection holds the project's binding: it is taken over only once that member left, lost
    the connection, or removed the integration."""


@dataclass(frozen=True)
class IntegrationView:
    """One binding with the display name of its member (``None`` once the member is gone)."""

    project_id: uuid.UUID
    app: str
    user_id: uuid.UUID | None
    user_name: str | None
    workspace_id: str
    workspace_name: str
    connected_at: datetime


def _view(row: ProjectIntegration, name: str | None) -> IntegrationView:
    return IntegrationView(
        row.project_id, row.app, row.user_id, name, row.workspace_id, row.workspace_name, row.connected_at
    )


def _with_member() -> Any:
    return select(ProjectIntegration, User.display_name).outerjoin(User, User.id == ProjectIntegration.user_id)


async def get_integration(session: AsyncSession, project_id: uuid.UUID, app: str) -> IntegrationView | None:
    statement = _with_member().where(ProjectIntegration.project_id == project_id, ProjectIntegration.app == app)
    found = (await session.execute(statement)).first()
    return None if found is None else _view(found[0], found[1])


async def list_integrations(session: AsyncSession, project_id: uuid.UUID) -> list[IntegrationView]:
    statement = _with_member().where(ProjectIntegration.project_id == project_id).order_by(ProjectIntegration.app)
    return [_view(row, name) for row, name in (await session.execute(statement)).all()]


async def _holds(session: AsyncSession, app: str, user_id: uuid.UUID) -> bool:
    """Whether ``user_id`` has an active connection to ``app``. Slack has no per-member connection: whoever bound the
    channel holds it."""

    if app == ATLASSIAN:
        probe = exists().where(AtlassianConnection.user_id == user_id, AtlassianConnection.status == "active")
    elif app == GITHUB:
        probe = exists().where(GitHubConnection.user_id == user_id, GitHubConnection.status == "active")
    else:
        return True
    return bool((await session.execute(select(probe))).scalar())


async def bind_integration(
    session: AsyncSession,
    project_id: uuid.UUID,
    app: str,
    *,
    user_id: uuid.UUID,
    workspace_id: str,
    workspace_name: str,
    now: datetime,
) -> None:
    """Bind the project's ``app`` to ``user_id``: a new row, the same member again, a member taking over the same
    workspace from a member whose connection is no longer active, or any member taking over a binding whose member is
    gone (nothing acts on its workspace any more). The row is locked ``FOR UPDATE`` while it is decided.
    ``WorkspaceMismatchError`` when the project is bound to another workspace of the app and a member acts on it;
    ``BindingHeldError`` when another member's active connection holds it."""

    values = {"user_id": user_id, "workspace_id": workspace_id, "workspace_name": workspace_name, "connected_at": now}
    added = pg_insert(ProjectIntegration).values(project_id=project_id, app=app, **values).on_conflict_do_nothing()
    if (await session.execute(added.returning(ProjectIntegration.app))).first() is not None:
        return
    locked = (
        select(ProjectIntegration)
        .where(ProjectIntegration.project_id == project_id, ProjectIntegration.app == app)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(locked)).scalar_one()
    if row.user_id is not None:
        if row.workspace_id != workspace_id:
            raise WorkspaceMismatchError
        if row.user_id != user_id and await _holds(session, app, row.user_id):
            raise BindingHeldError
    row.user_id, row.workspace_id, row.workspace_name, row.connected_at = user_id, workspace_id, workspace_name, now
    await session.flush()


async def add_missing_integration(
    session: AsyncSession,
    project_id: uuid.UUID,
    app: str,
    *,
    user_id: uuid.UUID,
    workspace_id: str,
    workspace_name: str,
    now: datetime,
) -> bool:
    """Bind the project's ``app`` only when it has no binding yet (a backfill); true when a row was added."""

    statement = (
        pg_insert(ProjectIntegration)
        .values(
            project_id=project_id,
            app=app,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            connected_at=now,
        )
        .on_conflict_do_nothing(index_elements=("project_id", "app"))
        .returning(ProjectIntegration.app)
    )
    return (await session.execute(statement)).first() is not None


async def delete_integration(
    session: AsyncSession, project_id: uuid.UUID, app: str, *, only_user: uuid.UUID | None = None
) -> bool:
    """Delete the binding; with ``only_user`` only when that member holds it (one statement, so a take-over between a
    check and the delete cannot be undone by the earlier holder). True when a row went."""

    statement = delete(ProjectIntegration).where(
        ProjectIntegration.project_id == project_id, ProjectIntegration.app == app
    )
    if only_user is not None:
        statement = statement.where(ProjectIntegration.user_id == only_user)
    return (await session.execute(statement.returning(ProjectIntegration.app))).first() is not None


async def point_jira_target(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """The Jira target of the project (if any) acts as ``user_id``; ``None`` leaves it with no acting account."""

    await session.execute(
        update(JiraTarget).where(JiraTarget.project_id == project_id).values(connected_user_id=user_id)
    )


async def point_repositories(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """Every repository of the project is checked and cloned with the connection of ``user_id`` (``added_by``)."""

    await session.execute(
        update(ProjectRepository).where(ProjectRepository.project_id == project_id).values(added_by=user_id)
    )
