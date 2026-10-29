"""Fixture: clean under orm_lint -- every relationship() has an allowed lazy=."""

from __future__ import annotations

from sqlalchemy.orm import Mapped, relationship


class Order:
    items: Mapped[list[object]] = relationship(lazy="selectin")
    payments: Mapped[list[object]] = relationship(back_populates="order", lazy="raise")
