"""The rows of the Linear pass behind one small port: each call opens its own transaction (no Linear call ever runs
inside one) and every write first checks that the target still has the destination the pass started with."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from db import linear_plan as plan_db
from db.linear_plan import LinearTargetView
from db.models import LinearIssue


class TargetChangedError(Exception):
    """The destination of the target changed during the pass: stop, write nothing more."""


class PlanStore(Protocol):
    """What a pass reads and writes."""

    async def get_target(self, project_id: uuid.UUID) -> LinearTargetView | None: ...

    async def list_items(self, project_id: uuid.UUID) -> list[LinearIssue]: ...

    async def add_pending(
        self, target: LinearTargetView, *, node_id: str, kind: str, requested_at: datetime
    ) -> None: ...

    async def update_item(self, target: LinearTargetView, node_id: str, **columns: Any) -> None: ...


class DbStore:
    """``PlanStore`` on the application database."""

    def __init__(self, transaction: Callable[[], AbstractAsyncContextManager[Any]]) -> None:
        self._transaction = transaction

    async def get_target(self, project_id: uuid.UUID) -> LinearTargetView | None:
        async with self._transaction() as session:
            return await plan_db.get_target(session, project_id)

    async def list_items(self, project_id: uuid.UUID) -> list[LinearIssue]:
        async with self._transaction() as session:
            return await plan_db.list_items(session, project_id)

    async def _guard(self, session: Any, target: LinearTargetView) -> None:
        unchanged = await plan_db.target_unchanged(
            session, target.project_id, team_id=target.team_id, linear_project_id=target.linear_project_id
        )
        if not unchanged:
            raise TargetChangedError

    async def add_pending(self, target: LinearTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await plan_db.add_pending(session, target.project_id, node_id=node_id, kind=kind, requested_at=requested_at)

    async def update_item(self, target: LinearTargetView, node_id: str, **columns: Any) -> None:
        async with self._transaction() as session:
            await self._guard(session, target)
            await plan_db.update_item(session, target.project_id, node_id, **columns)
