"""Fixture: violates orm_lint -- missing lazy=, and a disallowed lazy=."""

from __future__ import annotations

from sqlalchemy.orm import Mapped, relationship


class Order:
    items: Mapped[list[object]] = relationship()
    payments: Mapped[list[object]] = relationship(back_populates="order", lazy="noload")
