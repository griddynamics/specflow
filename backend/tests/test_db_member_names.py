"""``db.repositories.member_names``: one query, names of the project's members, ``None`` for the rest."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy.dialects import postgresql

from db import repositories as repo

PROJECT = uuid.UUID("00000000-0000-4000-8000-000000000001")
ANA, BOB, GONE = (uuid.UUID(int=n) for n in (1, 2, 3))


class StubSession:
    """The SQL boundary: answers every statement with ``rows`` and keeps the statements."""

    def __init__(self, rows: list[tuple[uuid.UUID, str, str]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> list[tuple[uuid.UUID, str, str]]:
        self.statements.append(statement)
        return self.rows


async def test_a_member_with_a_name_is_named_the_email_fallback_and_a_leaver_are_none() -> None:
    session = StubSession([(ANA, "Ana", "ana@example.com"), (BOB, "bob@example.com", "bob@example.com")])

    names = await repo.member_names(cast(Any, session), PROJECT, {ANA, BOB, GONE})

    assert names == {ANA: "Ana", BOB: None, GONE: None}


async def test_a_display_name_that_is_an_address_is_no_name() -> None:
    session = StubSession([(ANA, "Ana@Example.com", "ana@example.com"), (BOB, "bob@corp.example", "bob@example.com")])

    names = await repo.member_names(cast(Any, session), PROJECT, {ANA, BOB})

    assert names == {ANA: None, BOB: None}  # the email in another case; any other address


async def test_it_asks_one_query_scoped_to_the_project_and_none_without_ids() -> None:
    session = StubSession([])

    assert await repo.member_names(cast(Any, session), PROJECT, set()) == {}
    assert session.statements == []
    await repo.member_names(cast(Any, session), PROJECT, [ANA])
    assert len(session.statements) == 1
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "project_members.project_id =" in sql and "users.id IN" in sql
