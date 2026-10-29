"""Routes of the orders capability."""

from __future__ import annotations

from fastapi import APIRouter

from shop.orders import service
from shop.orders.schemas import OrderIn, OrderOut
from shop.platform.auth import CurrentUserDep

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", status_code=201, response_model=OrderOut)
async def create_order(body: OrderIn, user: CurrentUserDep) -> OrderOut:
    del user  # authenticated; every customer may place an order in this example
    return service.place_order(body)


@router.get("/{order_id}", response_model=OrderOut)
async def read_order(order_id: str, user: CurrentUserDep) -> OrderOut:
    del user  # authenticated; every customer may read an order by id in this example
    return service.get_order(order_id)


@router.delete("/{order_id}", status_code=204)
async def delete_order(order_id: str, user: CurrentUserDep) -> None:
    del user
    service.cancel_order(order_id)
