"""In-memory stand-in for ``db.integrations`` (no tests in here); ``test_integrations_db_fakes`` keeps its signatures
equal. ``pointed`` records every acting-account change of the Jira target and the repositories."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db import integrations as integrations_module
from db.integrations import IntegrationView, WorkspaceMismatchError

PUBLIC_FUNCTIONS = (
    "get_integration",
    "list_integrations",
    "bind_integration",
    "add_missing_integration",
    "delete_integration",
    "point_jira_target",
    "point_repositories",
)


class FakeIntegrationsDb:
    """The ``db.integrations`` functions over a dict; ``names`` gives each member's display name."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], IntegrationView] = {}
        self.names: dict[uuid.UUID, str] = {}
        self.pointed: list[tuple[str, uuid.UUID, uuid.UUID | None]] = []
        self.sessions: list[AsyncSession] = []  # the session each call got

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PUBLIC_FUNCTIONS:
            monkeypatch.setattr(integrations_module, name, getattr(self, name))

    def put(
        self,
        project_id: uuid.UUID,
        app: str,
        user_id: uuid.UUID | None,
        *,
        workspace_id: str = "w1",
        workspace_name: str = "W1",
        connected_at: datetime | None = None,
    ) -> None:
        """Write a row the way a connect did (a test's starting point)."""

        when = connected_at or datetime(2026, 1, 1, tzinfo=UTC)
        name = None if user_id is None else self.names.get(user_id)
        self.rows[project_id, app] = IntegrationView(project_id, app, user_id, name, workspace_id, workspace_name, when)

    def release(self, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """A member left: the rows they held lose the member (``db.members`` does it in the same transaction)."""

        for key, row in list(self.rows.items()):
            if key[0] == project_id and row.user_id == user_id:
                self.rows[key] = replace(row, user_id=None, user_name=None)

    async def get_integration(self, session: AsyncSession, project_id: uuid.UUID, app: str) -> IntegrationView | None:
        self.sessions.append(session)
        return self.rows.get((project_id, app))

    async def list_integrations(self, session: AsyncSession, project_id: uuid.UUID) -> list[IntegrationView]:
        self.sessions.append(session)
        return sorted((row for (pid, _), row in self.rows.items() if pid == project_id), key=lambda row: row.app)

    async def bind_integration(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        app: str,
        *,
        user_id: uuid.UUID,
        workspace_id: str,
        workspace_name: str,
        now: datetime,
    ) -> None:
        self.sessions.append(session)
        before = self.rows.get((project_id, app))
        if before is not None and before.workspace_id != workspace_id and before.user_id is not None:
            raise WorkspaceMismatchError
        self.rows[project_id, app] = IntegrationView(
            project_id, app, user_id, self.names.get(user_id), workspace_id, workspace_name, now
        )

    async def add_missing_integration(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        app: str,
        *,
        user_id: uuid.UUID,
        workspace_id: str,
        workspace_name: str,
        now: datetime,
    ) -> bool:
        self.sessions.append(session)
        if (project_id, app) in self.rows:
            return False
        await self.bind_integration(
            session,
            project_id,
            app,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            now=now,
        )
        return True

    async def delete_integration(
        self, session: AsyncSession, project_id: uuid.UUID, app: str, *, only_user: uuid.UUID | None = None
    ) -> bool:
        self.sessions.append(session)
        row = self.rows.get((project_id, app))
        if row is None or (only_user is not None and row.user_id != only_user):
            return False
        del self.rows[project_id, app]
        return True

    async def point_jira_target(self, session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
        self.sessions.append(session)
        self.pointed.append(("jira", project_id, user_id))

    async def point_repositories(self, session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
        self.sessions.append(session)
        self.pointed.append(("repositories", project_id, user_id))
