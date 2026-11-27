"""What the offline ``db.box`` and ``db.dropbox`` tests share (no tests in here): a session that records each statement
compiled for Postgres, and the checks of the ``expire``/``delete`` statements and of ``connection_store``."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects import postgresql

USER = uuid.UUID(int=1)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 3, 1, tzinfo=UTC)


class Result:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row

    def first(self) -> object | None:
        return self._row


class Session:
    """Records each statement compiled for Postgres (with its literal parameters) and answers with ``row``."""

    def __init__(self, row: object | None = None) -> None:
        self.row = row
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def execute(self, statement: Any) -> Result:
        compiled = statement.compile(dialect=postgresql.asyncpg.dialect())
        self.sql.append(str(compiled))
        self.params.append(dict(compiled.params))
        return Result(self.row)


async def check_expire_and_delete(rows: Callable[[Session], Any], session: Session, table: str) -> None:
    """After one UPDATE on ``session``: ``expire`` and ``delete`` follow, every statement filters on the user."""

    await rows(session).expire(USER, now=LATER)
    assert await rows(session).delete(USER) is True

    assert [text.split(" ", 1)[0] for text in session.sql] == ["UPDATE", "UPDATE", "DELETE"]
    assert all(f"{table}.user_id = $" in text for text in session.sql)
    assert await rows(Session()).delete(USER) is False


async def check_connection_store(connection_store: Callable[..., Any], rows_type: type) -> None:
    session = Session()
    opened: list[str] = []

    @asynccontextmanager
    async def transaction() -> Any:
        opened.append("begin")
        yield session
        opened.append("commit")

    async with connection_store(transaction)() as store:
        assert isinstance(store, rows_type) and opened == ["begin"]

    assert opened == ["begin", "commit"]
