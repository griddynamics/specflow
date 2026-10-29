"""The orders capability's entry module: other capabilities import only this."""

from __future__ import annotations

import uuid

from shop.enums import OrderStatus
from shop.errors import ConflictError, NotFoundError
from shop.orders.schemas import OrderIn, OrderOut

_ORDERS: dict[str, OrderOut] = {}


def place_order(data: OrderIn) -> OrderOut:
    order = OrderOut(
        id=uuid.uuid4().hex, item=data.item, quantity=data.quantity, status=OrderStatus.NEW
    )
    _ORDERS[order.id] = order
    return order


def get_order(order_id: str) -> OrderOut:
    order = _ORDERS.get(order_id)
    if order is None:
        raise NotFoundError("order", order_id)
    return order


def cancel_order(order_id: str) -> None:
    order = get_order(order_id)
    if order.status is OrderStatus.SHIPPED:
        msg = "a shipped order cannot be cancelled"
        raise ConflictError(msg)
    _ORDERS[order_id] = order.model_copy(update={"status": OrderStatus.CANCELLED})
