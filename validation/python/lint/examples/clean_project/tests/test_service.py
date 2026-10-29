"""Order arithmetic."""

from __future__ import annotations

import pytest

from src.orders.service import grand_total


@pytest.mark.requirement("REQ-1")
def test_grand_total_sums_the_stored_totals() -> None:
    assert grand_total() == 7
