"""Plan schema setup.

The core ``ensure_ddl`` applies the plan's static DDL, which ``ensure_schema`` also discovers and
applies with the full graph schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graph.core.schema import ensure_ddl
from graph.plan.queries import PLAN_DDL

__all__ = ["ensure_plan_schema"]


async def ensure_plan_schema(
    driver: Any,
    database: str | None = None,
    *,
    ddl: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Create the missing plan constraints and indexes; return their names.

    ``ddl`` defaults to ``PLAN_DDL``; a sequence replaces it (tests).
    """

    return await ensure_ddl(driver, PLAN_DDL if ddl is None else ddl, database)
