"""The sweep of a removed source's leftover objects (FR-SRC-0017, NFR-REL-0003).

An object is deleted only when no ``documents`` row has its id: a document that was
handed over, or never removed, keeps its objects. ``ObjectStore.delete`` is
idempotent, so retries and the second sweep are safe. Logs carry ids, counts and
fixed codes only (NFR-MAIN-0002).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from db import source_files
from logger.logger import ERROR, INFO, get_logger, log_event
from storage import ObjectStore
from storage.keys import (
    document_original,
    document_original_bin,
    document_original_json,
)
from temporal.cleanup import workflows as wf
from temporal.cleanup.models import SweepInput, SweepResult
from temporal.containment import Contain

logger = get_logger("temporal.cleanup.activities")

#: Object deletes in flight at once (a 5,000-id chunk is up to 15,000 calls).
DELETE_CONCURRENCY: Final = 32


class SourceCleanupActivities:
    """Activities bound to one session factory and object store."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], store: ObjectStore) -> None:
        self._sessions = session_factory
        self._store = store

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [self.sweep_source_objects]

    @activity.defn(name=wf.SWEEP_ACTIVITY)
    async def sweep_source_objects(self, data: SweepInput) -> SweepResult:
        with Contain() as contained:
            result = await self._sweep(data)
        if contained.error is not None:  # a fixed, retryable code: no statement or key text
            log_event(
                logger,
                ERROR,
                "source.cleanup.failed",
                project_id=str(data.project_id),
                source_id=str(data.source_id),
                sweep=data.sweep,
                error_type=contained.error_type,
            )
            raise ApplicationError("internal_error", type="SourceCleanupError") from None
        log_event(
            logger,
            INFO,
            "source.cleanup.swept",
            project_id=str(data.project_id),
            source_id=str(data.source_id),
            sweep=data.sweep,
            deleted=result.deleted,
            kept=result.kept,
            source_present=result.source_present,
        )
        return result

    async def _sweep(self, data: SweepInput) -> SweepResult:
        async with self._sessions() as session:
            present, missing = await source_files.sweep_candidates(
                session, data.project_id, data.source_id, data.document_ids
            )
        if present:  # the removal rolled back or is not committed yet: leave everything
            return SweepResult(source_present=True, deleted=0, kept=len(data.document_ids))
        slots = asyncio.Semaphore(DELETE_CONCURRENCY)
        project = str(data.project_id)

        async def drop(document_id: uuid.UUID) -> None:
            async with slots:
                for key in (
                    document_original(project, str(document_id)),
                    document_original_json(project, str(document_id)),
                    document_original_bin(project, str(document_id)),
                ):
                    await self._store.delete(key)

        await asyncio.gather(*(drop(document_id) for document_id in missing))
        return SweepResult(
            source_present=False,
            deleted=len(missing),
            kept=len(data.document_ids) - len(missing),
        )
