"""Wire shapes of the orders capability."""

from __future__ import annotations

from pydantic import BaseModel, Field

from shop.enums import OrderStatus


class OrderIn(BaseModel):
    item: str
    quantity: int = Field(ge=1)


class OrderOut(BaseModel):
    id: str
    item: str
    quantity: int
    status: OrderStatus
