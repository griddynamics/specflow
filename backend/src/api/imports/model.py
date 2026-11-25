"""The vocabulary of the importer seam: the source kinds the API imports itself, an import job and the two ways an
importer stops early."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from db.models import CONNECTION_FAILURE_REASONS

# the ``connections.kind`` values the API imports itself
IMPORT_KINDS: Final = ("confluence", "jira_epic", "linear_project")


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


class SourceGoneError(Exception):
    """The source was removed or is no longer importing: the importer stops."""


@dataclass(frozen=True)
class ImportJob:
    """One import: the source row, its project, the importing member and the source kind."""

    project_id: uuid.UUID
    source_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
