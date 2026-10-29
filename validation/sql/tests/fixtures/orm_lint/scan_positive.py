"""Fixture: violates orm_lint's scan rule -- each method reads every row of a table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Item
from app.models.enums import ItemStatus
from app.models.pricing import PricingPlan


class Repo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def every_item(self) -> list[Item]:
        return list((await self._db.execute(select(Item))).scalars().all())

    async def every_active_item(self) -> list[Item]:
        statement = select(Item).where(Item.status == ItemStatus.ACTIVE)
        return list((await self._db.scalars(statement.order_by(Item.id))).all())

    async def categories_with_items(self, kind: str) -> list[object]:
        del kind
        rows = await self._db.execute(
            select(Category.id, Item.id).join(Item, Item.id == Category.id)
        )
        return list(rows.all())

    async def inactive_plans(self) -> list[PricingPlan]:
        # `active` is unique only among true rows: false reads every inactive row.
        statement = select(PricingPlan).where(PricingPlan.active.is_(False))
        return list((await self._db.scalars(statement)).all())


class NightlyReconciliationJob:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def run(self) -> list[str]:
        return list((await self._db.scalars(select(Item.id))).all())
