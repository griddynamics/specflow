"""The vocabulary of the importer seam: the source kinds the API imports itself, an import job and the two ways an
importer stops early."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Final

from db.models import CONNECTION_FAILURE_REASONS

# the ``connections.kind`` values the API imports itself
IMPORT_KINDS: Final = ("confluence", "jira_epic", "linear_project", "box", "dropbox", "notion")


class InvalidFailureReasonError(Exception):
    """A failure reason that is not in the closed set of ``connections.failure_reason``."""


class NotImportedKindError(Exception):
    """A source kind the API does not import itself (``gdrive`` and ``upload`` have their own paths)."""


class ImportRunnerClosedError(Exception):
    """The runner was closed: no import starts any more."""


class ImportFailedError(Exception):
    """An importer's failure with a closed reason (a ``connections.failure_reason`` value)."""

    def __init__(self, reason: str) -> None:
        if reason not in CONNECTION_FAILURE_REASONS:
            raise InvalidFailureReasonError
        super().__init__(reason)
        self.reason = reason


class OriginalTooLargeError(Exception):
    """A file streamed past the byte cap its importer gave (``ImportWriter.add_file``): nothing was stored."""


class SourceGoneError(Exception):
    """The source was removed or is no longer importing: the importer stops."""


@dataclass(frozen=True)
class ImportJob:
    """One import: the source row, its project, the importing member and the source kind."""

    project_id: uuid.UUID
    source_id: uuid.UUID
    user_id: uuid.UUID
    kind: str


class ImportRun[Reader, Writer, Found]:
    """The files of one import: at most ``slots`` at a time, and what came of them (``added``, ``duplicates``,
    ``failed``). A subclass sets ``slots`` and imports one file in ``_one``; the first ``ImportFailedError`` or
    ``SourceGoneError`` (a connection that cannot be used, a removed source) ends the whole import and is raised
    after the files in flight stop. Any other error cancels the files in flight."""

    slots: int

    def __init__(self, reader: Reader, writer: Writer) -> None:
        self._reader = reader
        self._writer = writer
        self.added = self.duplicates = self.failed = 0

    async def _one(self, found: Found) -> None:
        raise NotImplementedError

    async def import_all(self, files: list[Found]) -> None:
        pending = iter(files)
        stopped: list[ImportFailedError | SourceGoneError] = []

        async def worker() -> None:
            for found in pending:
                if stopped:
                    return
                try:
                    await self._one(found)
                except (ImportFailedError, SourceGoneError) as error:
                    stopped.append(error)
                    return

        async with asyncio.TaskGroup() as group:
            for _ in range(self.slots):
                _task = group.create_task(worker())
        if stopped:
            raise stopped[0]
