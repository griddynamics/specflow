"""Adding an imported source (docs/sources/importers.md): create the source for the caller, start its import as the
member the project's Atlassian is bound to, and answer the card the Sources status shows for it."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from api.errors import source_not_found
from api.imports import Importer, ImportJob
from api.runtime import Runtime
from api.schemas import BatchSourceOut
from api.source_views import summary_out
from db import source_files as files


@dataclass(frozen=True)
class StartedImport:
    """The import to start: the source ``kind``, its importer and the member it reads as (the bound member)."""

    kind: str
    importer: Importer
    reader: uuid.UUID


async def start_import(
    runtime: Runtime, project_id: uuid.UUID, user_id: uuid.UUID, *, started: StartedImport, title: str, link: str
) -> BatchSourceOut:
    """Create the source (``connected_by`` = ``user_id``, the caller), start its import and read the card back."""

    source_id = await runtime.imports.add_source(project_id, user_id, kind=started.kind, name=title, link=link.strip())
    runtime.imports.start(ImportJob(project_id, source_id, started.reader, started.kind), started.importer)
    async with runtime.transaction() as session:
        summary = await files.source_summary(session, project_id, source_id)
    if summary is None:  # removed between the create and the read
        raise source_not_found()
    return BatchSourceOut(source=summary_out(summary))
