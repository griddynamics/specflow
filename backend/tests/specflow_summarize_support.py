"""Fakes for the tier-1 summarize activity tests (no tests here).

``FakeAgentDb`` emulates the agent role: a session must first set the
``app.project_id`` scope (as ``agent_scope`` does), and a row is visible only
inside its own project's scope (RLS).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeRow:
    project_id: uuid.UUID
    markdown: str | None


@dataclass
class FakeAgentDb:
    rows: dict[uuid.UUID, FakeRow] = field(default_factory=dict)
    fail: Exception | None = None
    statements: list[str] = field(default_factory=list)
    sessions: int = 0

    def __call__(self) -> FakeSession:
        self.sessions += 1
        return FakeSession(self)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one_or_none(self) -> Any:
        return self._row


class FakeSession:
    def __init__(self, db: FakeAgentDb) -> None:
        self._db = db
        self._scope: uuid.UUID | None = None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self):
        yield self

    def _visible(self, params: dict[str, Any]) -> FakeRow | None:
        assert self._db.statements and "set_config" in self._db.statements[0]
        if self._db.fail is not None:
            raise self._db.fail
        row = self._db.rows.get(params["document_id"])
        if row is None or self._scope is None:
            return None
        if row.project_id != self._scope or row.project_id != params["project_id"]:
            return None
        return row

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result | None:
        sql = str(statement)
        self._db.statements.append(sql)
        if "set_config" in sql:
            self._scope = uuid.UUID(params["pid"])
            return None
        row = self._visible(params)
        if row is None:
            return _Result(None)
        if row.markdown is None:
            return _Result(SimpleNamespace(chunk=None, total=None))
        start = params["start"] - 1
        chunk = row.markdown[start : start + params["limit"]]
        return _Result(SimpleNamespace(chunk=chunk, total=len(row.markdown)))


class FakeAppSessions:
    """App-role session factory; repositories are monkeypatched in the tests."""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> FakeAppSession:
        self.opened += 1
        return FakeAppSession()


class FakeAppSession:
    async def __aenter__(self) -> FakeAppSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self):
        yield self
