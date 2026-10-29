"""Fixture: clean under orm_lint's scan rule -- every read is bounded, keyed or one row."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.catalog import Category, Item
from app.models.enums import ItemStatus
from app.models.pricing import PricingPlan
from app.models.stock import Warehouse


def select_words(words: Sequence[str]) -> list[str]:
    return list(words)


def _narrowed(statement: Select[tuple[Item]], category_ids: list[str]) -> Select[tuple[Item]]:
    return statement.where(Item.category_ids.contains(category_ids))


class Repo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def item(self, item_id: str) -> Item | None:
        return await self._db.scalar(select(Item).where(Item.id == item_id))

    async def items(self, ids: Sequence[str]) -> list[Item]:
        wanted: list[str] = []
        for value in ids:
            wanted.append(value)
        statement = select(Item).where(Item.status == ItemStatus.ACTIVE)
        statement = statement.where(Item.id.in_(wanted))
        return list((await self._db.scalars(statement)).all())

    async def by_kind(self, kind: str) -> list[Category]:
        return list((await self._db.scalars(select(Category).filter_by(kind=kind))).all())

    async def first_active(self) -> Item | None:
        return await self._db.scalar(
            select(Item).where(Item.status == ItemStatus.ACTIVE).order_by(Item.id).limit(1)
        )

    async def count(self) -> int:
        return int(await self._db.scalar(select(func.count()).select_from(Item)) or 0)

    async def any_active(self) -> bool:
        return bool(
            await self._db.scalar(select(exists().where(Item.status == ItemStatus.ACTIVE)))
        )

    async def main_warehouse(self) -> Warehouse | None:
        return await self._db.scalar(select(Warehouse).where(Warehouse.code == "MAIN"))

    async def active_plan(self) -> PricingPlan | None:
        # `active` is unique among true rows: true seeks the one active row.
        return await self._db.scalar(select(PricingPlan).where(PricingPlan.active.is_(True)))

    async def categories_citing(self, category_ids: list[str]) -> list[str]:
        cited = select(Item.id).where(Item.status == ItemStatus.ACTIVE).subquery()
        rows = await self._db.scalars(
            select(Category.id).where(Category.id.in_(category_ids), Category.id.in_(cited))
        )
        return list(rows.all())

    async def in_categories(self, category_ids: list[str]) -> list[Item]:
        statement = _narrowed(select(Item), category_ids)
        return list((await self._db.scalars(statement)).all())

    def base(self) -> Select[tuple[Item]]:
        return select(Item)
