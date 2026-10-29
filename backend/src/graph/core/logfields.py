"""Log field helpers of the graph library."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def count_fields(counts: Mapping[str, int]) -> dict[str, Any]:
    """``counts`` as free-form log fields.

    Expanding a ``dict[str, int]`` into ``log_event(..., **counts)`` type-checks the ints against the
    function's keyword parameters too (``exc_info``); a ``dict[str, Any]`` does not.
    """

    return dict(counts)
