"""The in-memory ``ConnectionStore`` the GitHub and Box tests share (no tests in here). A provider's support module
subclasses ``MemoryConnectionStore`` and adds ``save`` (the one method that builds its own ``StoredConnection``)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any


class MemoryConnectionStore:
    """The store over a dict; ``transaction()`` keeps nothing of a failed block. ``events`` records every write and each
    commit."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, Any] = {}
        self.refresh_expires: dict[uuid.UUID, datetime | None] = {}
        self.expired_at: dict[uuid.UUID, datetime] = {}
        self.events: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryConnectionStore]:
        before = dict(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = before
            self.events.append(("rollback", None))
            raise
        self.events.append(("commit", None))

    async def get(self, user_id: uuid.UUID) -> Any:
        return self.rows.get(user_id)

    async def lock(self, user_id: uuid.UUID) -> Any:
        self.events.append(("lock", user_id))
        return self.rows.get(user_id)

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None:
        self.rows[user_id] = replace(self.rows[user_id], refresh_token=refresh_token, last_used_at=now)
        self.refresh_expires[user_id] = refresh_expires_at
        self.events.append(("rotate", user_id))

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        self.rows[user_id] = replace(self.rows[user_id], status="expired")
        self.expired_at[user_id] = now
        self.events.append(("expire", user_id))

    async def delete(self, user_id: uuid.UUID) -> bool:
        self.events.append(("delete", user_id))
        return self.rows.pop(user_id, None) is not None
