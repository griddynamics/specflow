"""The rows of the GitHub Projects pass behind one small port: each call opens its own transaction (no GitHub call
ever runs inside one) and every write first checks that the target still has the destination the pass started with."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from db import github_projects as projects_db
from db.github_projects import ProjectTargetView
from db.models import GitHubProjectItem


class TargetChangedError(Exception):
    """The destination of the target changed during the pass: stop, write nothing more."""


class ProjectsStore(Protocol):
    """What a pass reads and writes."""

    async def get_target(self, project_id: uuid.UUID) -> ProjectTargetView | None: ...

    async def list_items(self, project_id: uuid.UUID) -> list[GitHubProjectItem]: ...

    async def add_pending(
        self, target: ProjectTargetView, *, node_id: str, kind: str, requested_at: datetime
    ) -> None: ...

    async def update_item(self, target: ProjectTargetView, node_id: str, **columns: Any) -> None: ...


class DbStore:
    """``ProjectsStore`` on the application database."""

    def __init__(self, transaction: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._transaction = transaction

    async def get_target(self, project_id: uuid.UUID) -> ProjectTargetView | None:
        async with self._transaction() as session:
            return await projects_db.get_target(session, project_id)

    async def list_items(self, project_id: uuid.UUID) -> list[GitHubProjectItem]:
        async with self._transaction() as session:
            return await projects_db.list_items(session, project_id)

    async def _guard(self, session: Any, target: ProjectTargetView) -> None:
        unchanged = await projects_db.target_unchanged(
            session, target.project_id, repository_id=target.repository_id, project_node_id=target.project_node_id
        )
        if not unchanged:
            raise TargetChangedError

    async def add_pending(self, target: ProjectTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await projects_db.add_pending(
                session, target.project_id, node_id=node_id, kind=kind, requested_at=requested_at
            )

    async def update_item(self, target: ProjectTargetView, node_id: str, **columns: Any) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await projects_db.update_item(session, target.project_id, node_id, **columns)
