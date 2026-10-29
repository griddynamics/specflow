"""Real-scan fixture for store_work.yml's `paths:` scoping: a service shaping a repository
read is the caller rules' finding, never the repository rules'."""

from __future__ import annotations


async def bundles(repo: object, category_ids: list[str]) -> list[object]:
    items = await repo.list_items(category_ids)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return [item for item in items if item.kind == "BUNDLE"]


async def first_rows(db: object, statement: object, limit: int) -> list[object]:
    rows = (await db.execute(statement)).scalars().all()
    # ruleid: kit.store_work.caller-slices-repository-read
    return rows[:limit]


def dot(first: list[float], second: list[float]) -> float:
    # ruleid: kit.store_work.hand-rolled-vector-maths
    return sum(a * b for a, b in zip(first, second, strict=True))
