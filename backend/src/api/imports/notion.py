"""The Notion importer (docs/notion/source.md): a page and its sub-pages, or a database and its rows, as documents.
Stream D1 fills ``inspect`` and ``NotionImporter.__call__``; the types and the caps are final.

Runs in the API process as the member the project's Notion is connected by (``connections.gateway(job.user_id)``). A
page is read as Markdown (N3: ``NotionGateway.page_markdown``, converted by ``notion.convert``); the ``<page>`` lines
it holds are followed, and so are the rows of the ``<database>`` lines (``query_data_source``, a row's document is its
properties table and its body). A visited set and ``MAX_PAGES`` (pages and rows together, N4) bound the walk; a cap
that is hit is logged (counts only). A page the member cannot see is an item ``no_access`` (step ``read``); a link
that cannot be read at all fails the source ``no_access``; Notion down or throttled is ``unknown_error``; a page too
deep to convert is an item ``convert_failed`` (step ``convert``) (N6). The original stored per page is JSON ``{page,
markdown}`` with e-mails removed (N5). Titles and text live in documents only: events carry ids, kinds and counts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from api.imports.model import ImportJob
from api.imports.writer import ImportWriter
from notion.gateway import NotionGateway
from notion.links import NotionLink

MAX_PAGES: Final = 100  # pages and database rows of one import, together
NO_ACCESS: Final = "no_access"
CONVERT_FAILED: Final = "convert_failed"
READ_STEP: Final = "read"
CONVERT_STEP: Final = "convert"


class Connections(Protocol):
    """The part of ``notion.connections.NotionConnections`` the importer uses."""

    async def gateway(self, user_id: uuid.UUID) -> NotionGateway: ...


@dataclass(frozen=True)
class NotionImport:
    """What to import: the object a pasted link names."""

    link: NotionLink


@dataclass(frozen=True)
class Inspection:
    """What a link would import, for the check: a page with its sub-pages or a database with its rows, its title, how
    many pages (at most ``MAX_PAGES``; ``capped`` when there are more)."""

    kind: Literal["page", "database"]
    title: str
    pages: int
    capped: bool


async def inspect(gateway: NotionGateway, source: NotionImport) -> Inspection:
    """Read the target and report what an import would take. ``NotionError`` when a read is refused
    (``notion_not_shared`` for a page or database the member cannot see)."""

    raise NotImplementedError("D1 fills inspect")


class NotionImporter:
    """An ``Importer`` (``api.imports.runner``) of one Notion link."""

    def __init__(self, connections: Connections, source: NotionImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        raise NotImplementedError("D1 fills NotionImporter.__call__")
