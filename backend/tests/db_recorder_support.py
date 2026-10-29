"""A recording stand-in for an async SQLAlchemy session, shared by the offline db.sources tests.

Each call compiles the statement for asyncpg, keeps the SQL text and answers with the next canned result.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.dialects import postgresql


class Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def one_or_none(self) -> object | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return self._rows

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)


class Recorder:
    """Records the compiled SQL; answers each call with the next canned result."""

    def __init__(self, *answers: list[object]) -> None:
        self.sql: list[str] = []
        self._answers = list(answers)

    def _answer(self, statement: object) -> Rows:
        self.sql.append(str(statement.compile(dialect=postgresql.asyncpg.dialect())))
        return Rows(self._answers.pop(0) if self._answers else [])

    async def scalars(self, statement: object) -> Rows:
        return self._answer(statement)

    async def scalar(self, statement: object) -> object | None:
        return self._answer(statement).one_or_none()

    async def execute(self, statement: object) -> Rows:
        return self._answer(statement)
