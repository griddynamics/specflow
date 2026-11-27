"""The rows of the Notion plan pass behind one small port: each call opens its own transaction (no Notion call ever runs
inside one) and every write first checks that the target still has the parent page and the database the pass started
with. Stream E1 owns this file; it is final and complete (the shape of ``api.github_projects.store``)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from db import notion_plan as plan_db
from db.models import NotionPlanItem
from db.notion_plan import PlanTargetView


class TargetChangedError(Exception):
    """The destination of the target changed during the pass: stop, write nothing more."""


class PlanStore(Protocol):
    """What a pass reads and writes."""

    async def get_target(self, project_id: uuid.UUID) -> PlanTargetView | None: ...

    async def list_items(self, project_id: uuid.UUID) -> list[NotionPlanItem]: ...

    async def save_database(
        self, target: PlanTargetView, *, database_id: str, data_source_id: str, database_url: str
    ) -> bool:
        """Store the database; false when the target no longer has the parent page and database the pass started with
        (changed, or another pass stored its database first): nothing is written."""
        ...

    async def reset_database(self, target: PlanTargetView) -> None: ...

    async def add_pending(self, target: PlanTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None: ...

    async def update_item(self, target: PlanTargetView, node_id: str, **columns: Any) -> None: ...


class DbStore:
    """``PlanStore`` on the application database."""

    def __init__(self, transaction: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._transaction = transaction

    async def get_target(self, project_id: uuid.UUID) -> PlanTargetView | None:
        async with self._transaction() as session:
            return await plan_db.get_target(session, project_id)

    async def list_items(self, project_id: uuid.UUID) -> list[NotionPlanItem]:
        async with self._transaction() as session:
            return await plan_db.list_items(session, project_id)

    async def _guard(self, session: Any, target: PlanTargetView) -> None:
        unchanged = await plan_db.target_unchanged(
            session, target.project_id, parent_page_id=target.parent_page_id, database_id=target.database_id
        )
        if not unchanged:
            raise TargetChangedError

    async def save_database(
        self, target: PlanTargetView, *, database_id: str, data_source_id: str, database_url: str
    ) -> bool:
        async with self._transaction() as session:
            return await plan_db.save_database(
                session,
                target.project_id,
                parent_page_id=target.parent_page_id,
                held_database_id=target.database_id,
                database_id=database_id,
                data_source_id=data_source_id,
                database_url=database_url,
            )

    async def reset_database(self, target: PlanTargetView) -> None:
        """The database is gone: forget it and the map."""

        async with self._transaction() as session:
            await self._guard(session, target)
            await plan_db.reset_database(session, target.project_id)

    async def add_pending(self, target: PlanTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await plan_db.add_pending(session, target.project_id, node_id=node_id, kind=kind, requested_at=requested_at)

    async def update_item(self, target: PlanTargetView, node_id: str, **columns: Any) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await plan_db.update_item(session, target.project_id, node_id, **columns)
