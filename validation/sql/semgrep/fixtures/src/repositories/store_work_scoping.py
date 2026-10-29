"""Real-scan fixture for store_work.yml's `paths:` scoping: a repository shaping its own
statement's rows is the repository rules' finding, never the caller rules'."""

from __future__ import annotations


async def active_items(db: object, statement: object) -> list[object]:
    rows = (await db.execute(statement)).scalars().all()
    # ruleid: kit.store_work.repository-result-shaped-in-python
    return [row for row in rows if row.status == "ACTIVE"]


async def first_rows(db: object, statement: object, limit: int) -> list[object]:
    rows = (await db.execute(statement)).scalars().all()
    # ruleid: kit.store_work.repository-result-sliced
    return rows[:limit]


def commit_open_session(session: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    session.commit()
