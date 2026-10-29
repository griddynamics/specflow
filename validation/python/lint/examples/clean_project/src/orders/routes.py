"""Order endpoints, framework-free in this example."""

from __future__ import annotations

from src.orders.service import grand_total


def read_grand_total() -> dict[str, int]:
    return {"total": grand_total()}
