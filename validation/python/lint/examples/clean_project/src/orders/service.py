"""Order arithmetic."""

from __future__ import annotations

from src.orders.repositories import stored_totals


def grand_total() -> int:
    return sum(stored_totals())
