"""Adopt documents that no source holds yet (DATA-SRC-0002).

An old API pod, or an old worker, can still write documents without
``documents.source_id`` (rolling deploy). Every write path of the new code calls
``adopt_unheld`` first, so those documents get a holder before anything else reads
or removes them.

The statements are steps B1-B5 with an optional project scope; the migration has no
backfill (a development database is created from scratch).
``tests/test_db_sources_batches_offline.py`` keeps the scoped and the global text equal.
Same rules as ``db.sources``: runs inside the caller's session and transaction,
never commits, never logs.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.query_files import read_query

# The statements live in ``src/db/queries/repair/`` and hold the markers ``/*d_scope*/`` /
# ``/*i_scope*/``: they become ``AND d.project_id = :pid`` / ``AND i.project_id = :pid`` for one
# project and nothing for the whole database.
B1_DAY_BATCHES: Final = read_query("repair/b1_day_batches.sql")
B2_ITEMS: Final = read_query("repair/b2_items.sql")
B3_UPLOAD_HOLDERS: Final = read_query("repair/b3_upload_holders.sql")
B4_DRIVE_HOLDERS: Final = read_query("repair/b4_drive_holders.sql")
B5_LINKERS: Final = read_query("repair/b5_linkers.sql")

LOCK_PROJECT: Final = read_query("repair/lock_project.sql")
STEPS: Final = (
    B1_DAY_BATCHES,
    B2_ITEMS,
    B3_UPLOAD_HOLDERS,
    B4_DRIVE_HOLDERS,
    B5_LINKERS,
)


def _apply_scope(step: str, scope: dict[str, str]) -> str:
    for name, clause in scope.items():
        step = step.replace(f"/*{name}*/", clause)
    return step


def repair_statements(*, project_scoped: bool) -> list[str]:
    """B1-B5 as SQL text; ``:pid`` is the only parameter of the scoped form."""

    scope = {
        "d_scope": "AND d.project_id = :pid" if project_scoped else "",
        "i_scope": "AND i.project_id = :pid" if project_scoped else "",
    }
    return [_apply_scope(step, scope) for step in STEPS]


async def adopt_unheld(session: AsyncSession, project_id: uuid.UUID | None) -> int:
    """Give every unheld document of the project (``None``: all) a holder.

    Old uploads move into their UTC day batch (``legacy-YYYYMMDD``), a Drive
    document goes to the earliest source that lists it, and an item that links a
    document held by another source becomes ``duplicate``. Idempotent. Returns the
    number of documents that got a holder; the partial index ``ix_documents_unheld``
    makes the call close to free when nothing is unheld.
    """

    params = {"pid": project_id} if project_id is not None else {}
    if project_id is not None:
        # The lock start_batch takes for name_suffix, so a repair and a batch start in
        # the same minute cannot give two batches one suffix. The global run (None) is
        # for operators and tests only; a display-only suffix clash is acceptable there.
        await session.execute(text(LOCK_PROJECT), params)
    adopted = 0
    for number, sql in enumerate(repair_statements(project_scoped=project_id is not None), start=1):
        result = cast("CursorResult[Any]", await session.execute(text(sql), params))
        if number in (3, 4):  # B3 and B4 set documents.source_id
            adopted += result.rowcount or 0
    return adopted
