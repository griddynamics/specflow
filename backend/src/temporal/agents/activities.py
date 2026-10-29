"""Agents-queue activities (SPECS section 11). All ``async def``.

- The tool activity ``read_document_chunk`` reads Postgres ONLY as the
  row-level-security agent role through ``agent_scope(project_id)``. Its SQL
  (``src/db/queries/agents/``) is fixed text with bound parameters; nothing the
  model writes is ever SQL.
- ``save_summarization`` and ``mark_summarization_failed`` are the trusted
  writes (app role), called by the workflow, never by a tool.

Error messages reach workflow history (``ActivityTaskFailed``), so every
failure is re-raised as an ``ApplicationError`` whose message is a fixed code,
without a cause: never a DB, driver or exception text, never content (AC-8.4).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from db import repositories
from db.agent_scope import agent_scope
from db.query_files import read_query
from logger.correlation import bound
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from temporal.agents.models import (
    CHUNK_LIMIT,
    DocumentChunk,
    MarkFailedInput,
    ReadChunkInput,
    SaveSummarizationInput,
)
from temporal.agents.tools import (
    MARK_FAILED_ACTIVITY,
    READ_CHUNK_ACTIVITY,
    READ_DOCUMENT,
    SAVE_SUMMARIZATION_ACTIVITY,
)
from temporal.containment import Contain

logger = get_logger("temporal.agents.activities")

# Fixed statements with bound parameters only (RLS also filters by project).
READ_CHUNK_SQL: Final = text(read_query("agents/read_document_chunk.sql"))

MAX_SQL_START: Final = 2**31 - 2
DOCUMENT_NOT_FOUND: Final = "document_not_found"
MARKDOWN_MISSING: Final = "markdown_missing"
TOOL_UNAVAILABLE: Final = "tool_unavailable"
SAVE_FAILED: Final = "save_failed"
INVALID_ERROR_CODE: Final = "invalid_error_code"


def fixed_error(code: str, error_type: str, *, retryable: bool = False) -> ApplicationError:
    """An error whose message is only the fixed ``code`` (raise it ``from None``)."""

    return ApplicationError(code, type=error_type, non_retryable=not retryable)


def chunk_of(text_part: str | None, total: int, offset: int) -> DocumentChunk:
    """The chunk at ``offset``; past the end -> empty text and no ``next_offset``."""

    part = (text_part or "")[:CHUNK_LIMIT]
    end = offset + len(part)
    return DocumentChunk(
        text=part,
        offset=offset,
        next_offset=end if part and end < total else None,
        total_chars=total,
    )


class SummarizeActivities:
    """Activities bound to the app and agent session factories."""

    def __init__(
        self,
        app_sessions: async_sessionmaker[AsyncSession],
        agent_sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._app_sessions = app_sessions
        self._agent_sessions = agent_sessions

    # ------------------------------------------------------------------ tools
    @activity.defn(name=READ_CHUNK_ACTIVITY)
    async def read_document_chunk(self, data: ReadChunkInput) -> DocumentChunk:
        """Up to 16 000 characters of the markdown from ``offset`` (agent role, RLS)."""

        async with self._tool_call(READ_DOCUMENT, data.project_id):
            params = {
                # int32 in the SQL: a larger start is past the end of any document.
                "start": min(data.offset + 1, MAX_SQL_START),
                "limit": min(data.limit, CHUNK_LIMIT),
                "document_id": data.document_id,
                "project_id": data.project_id,
            }
            async with agent_scope(self._agent_sessions, data.project_id) as session:
                row = (await session.execute(READ_CHUNK_SQL, params)).one_or_none()
            if row is None:
                raise fixed_error(DOCUMENT_NOT_FOUND, "DocumentNotFound")
            if row.total is None:
                raise fixed_error(MARKDOWN_MISSING, "MarkdownMissing")
            return chunk_of(row.chunk, row.total, data.offset)

    # --------------------------------------------------------- trusted writes
    @activity.defn(name=SAVE_SUMMARIZATION_ACTIVITY)
    async def save_summarization(self, data: SaveSummarizationInput) -> None:
        """Overwrite the summary and set ``succeeded`` (app role)."""

        with Contain() as contained:
            async with self._app_sessions() as session, session.begin():
                found = await repositories.save_summarization(
                    session, data.project_id, data.document_id, data.summary, data.model
                )
        if contained.error is not None:
            self._log_failure("agent.summary.save_error", data.document_id, contained.error)
            raise fixed_error(SAVE_FAILED, "SaveSummarizationError", retryable=True) from None
        if not found:
            raise fixed_error(DOCUMENT_NOT_FOUND, "DocumentNotFound")
        with bound(run_id=_run_id()):
            log_event(
                logger,
                INFO,
                "agent.summary.saved",
                project_id=str(data.project_id),
                workflow_id=data.workflow_id,
                document_id=str(data.document_id),
                requested_by=str(data.requested_by),
                model=data.model,
                summary_chars=len(data.summary),
            )

    @activity.defn(name=MARK_FAILED_ACTIVITY)
    async def mark_summarization_failed(self, data: MarkFailedInput) -> None:
        """Set ``failed`` with a fixed code; the previous summary stays (app role)."""

        try:
            with Contain(ValueError) as contained:
                async with self._app_sessions() as session, session.begin():
                    await repositories.mark_summarization_failed(
                        session, data.project_id, data.document_id, data.error_code
                    )
        except ValueError:
            raise fixed_error(INVALID_ERROR_CODE, "InvalidErrorCode") from None
        if contained.error is not None:
            self._log_failure("agent.summary.mark_failed_error", data.document_id, contained.error)
            raise fixed_error(SAVE_FAILED, "MarkFailedError", retryable=True) from None
        log_event(
            logger,
            ERROR,
            "agent.summary.failed",
            project_id=str(data.project_id),
            document_id=str(data.document_id),
            error_code=data.error_code,
        )

    # ---------------------------------------------------------------- helpers
    @asynccontextmanager
    async def _tool_call(self, tool: str, project_id: Any) -> AsyncIterator[None]:
        """Log ``agent.tool.called`` (no content) and sanitize every failure."""

        started = time.monotonic()
        outcome = "ok"
        try:
            with Contain(ApplicationError) as contained:
                try:
                    yield
                except ApplicationError:
                    outcome = "failed"
                    raise
            if contained.error is not None:
                outcome = "failed"
                self._log_failure("agent.tool.error", None, contained.error, tool=tool)
                raise fixed_error(TOOL_UNAVAILABLE, "ToolActivityError", retryable=True) from None
        finally:
            with bound(run_id=_run_id()):
                log_event(
                    logger,
                    INFO,
                    "agent.tool.called",
                    project_id=str(project_id),
                    tool=tool,
                    outcome=outcome,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )

    @staticmethod
    def _log_failure(event: str, document_id: Any, error: Exception, **fields: Any) -> None:
        log_event(
            logger,
            WARNING,
            event,
            document_id=str(document_id) if document_id is not None else None,
            error_type=type(error).__name__,
            **fields,
        )


def _run_id() -> str | None:
    """The Temporal run id of the calling workflow execution (correlation ``run_id``)."""

    try:
        return activity.info().workflow_run_id
    except RuntimeError:  # outside an activity (a method called directly)
        return None
