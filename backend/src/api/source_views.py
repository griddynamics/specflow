"""Read-model rows -> response models of the sources API (INT-SRC-0001, INT-SRC-0002).

Pure mapping, no I/O except ``removed_files``. File names live in these bodies only.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas import (
    AddedByOut,
    CountsOut,
    FileOut,
    FileStateName,
    HolderOut,
    ProgressOut,
    SourceKindName,
    SourcesStatusV2Out,
    SourceSummaryOut,
)
from db.batches import AddOutcome
from db.models import Connection, Project
from db.source_files import FILE_STATES, FileRow, HolderRef, SourceSummary, file_state

_SOURCE_KIND: TypeAdapter[SourceKindName] = TypeAdapter(SourceKindName)
_FILE_STATE: TypeAdapter[FileStateName] = TypeAdapter(FileStateName)


def summary_out(summary: SourceSummary) -> SourceSummaryOut:
    return SourceSummaryOut(
        id=summary.id,
        kind=_SOURCE_KIND.validate_python(summary.kind),
        name=summary.name,
        name_suffix=summary.name_suffix,
        added_by=AddedByOut(id=summary.added_by_id, display_name=summary.added_by_name),
        created_at=summary.created_at,
        listing_finished=summary.listing_finished,
        open=summary.open,
        total=summary.total,
        counts=CountsOut(**{state: summary.counts.get(state, 0) for state in FILE_STATES}),
        failure_reason=summary.failure_reason,
        folder_id=summary.folder_id,
        link=summary.link,
    )


def holder_out(holder: HolderRef | Connection | None) -> HolderOut | None:
    if holder is None:
        return None
    return HolderOut(
        id=holder.id,
        kind=_SOURCE_KIND.validate_python(holder.kind),
        name=holder.name,
        name_suffix=holder.name_suffix,
        created_at=holder.created_at,
    )


def file_out(row: FileRow) -> FileOut:
    return FileOut(
        id=row.id,
        name=row.name,
        category=row.category,
        state=_FILE_STATE.validate_python(row.state),
        reason=row.reason,
        holder=holder_out(row.holder),
    )


def status_v2(summaries: list[SourceSummary], processed: int, removed: int) -> SourcesStatusV2Out:
    """``progress`` sums every source: finished = total - waiting - processing (FR-GDUI-0021)."""

    total = sum(s.total for s in summaries)
    unfinished = sum(s.counts.get("waiting", 0) + s.counts.get("processing", 0) for s in summaries)
    return SourcesStatusV2Out(
        progress=ProgressOut(
            listing_finished=all(s.listing_finished for s in summaries),
            total=total,
            finished=total - unfinished,
        ),
        processed_documents=processed,
        removed_files=removed,
        sources=[summary_out(s) for s in summaries],
    )


def added_file(outcome: AddOutcome) -> FileOut | None:
    """The row of an accepted file as the file list would show it right after the add.

    Same rules as the SQL read model, through the pure mirror: a new or moved file is
    held by the batch and its document is ``processing`` (the add marked it), a linked
    one is a ``duplicate`` item. ``None`` for ``duplicate`` (nothing was added).
    """

    item = outcome.item
    if outcome.outcome == "duplicate" or item is None:
        return None
    held = outcome.outcome in ("added", "read_again")
    state = file_state(
        source_failed=False,
        item_state="pending" if held else "duplicate",
        item_error_reason=None,
        holds=held,
        doc_status="processing" if held else None,
        doc_error=None,
        doc_age=timedelta(0) if held else None,
    )
    return FileOut(
        id=item.id,
        name=item.name,
        category=item.category,
        state=_FILE_STATE.validate_python(state.state),
        reason=state.reason,
        holder=holder_out(outcome.holder),
    )


async def removed_files(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Files of sources removed since the last build (FR-SRC-0018)."""

    return int(await session.scalar(select(Project.removed_files).where(Project.id == project_id)) or 0)
