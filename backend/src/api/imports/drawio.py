"""The diagrams.net importer (docs/sources/importers.md): one document per diagram page.

The request has already decoded the diagram (``file_parsers.diagrams``), so the importer reads nothing from outside. It
declares ``page:<page id>`` titled ``<diagram name> · <page name>`` for every page, then stores each page: the Markdown
of ``to_markdown``, parser ``drawio-convert``, and ``original.drawio`` (``application/vnd.jgraph.mxfile``) holding that
page's own ``page_original``. A failure can only end as ``unknown_error``. Events carry ids and counts only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from api.imports.model import ImportFailedError, ImportJob
from api.imports.writer import ImportWriter
from file_parsers.diagrams import Diagram, DiagramDecodeError, page_original, page_title, to_markdown
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.imports.drawio")
MXFILE_MEDIA_TYPE = "application/vnd.jgraph.mxfile"
DEFAULT_NAME = "diagrams.net diagram"


def named(diagram: Diagram, link_name: str = "") -> Diagram:
    """``diagram`` with its name settled: the name the link gives, else the one it has, else the name of its only page,
    else ``DEFAULT_NAME``. The
    source name, the check answer and the item names all read it, so they agree."""

    single = diagram.pages[0].name.strip()[:200] if len(diagram.pages) == 1 else ""
    return replace(diagram, name=link_name.strip()[:200] or diagram.name.strip()[:200] or single or DEFAULT_NAME)


@dataclass(frozen=True)
class DrawioImport:
    """What to import: the decoded diagram."""

    diagram: Diagram


class DrawioImporter:
    """An ``Importer`` (``api.imports.runner``) of one decoded diagram."""

    def __init__(self, source: DrawioImport) -> None:
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        diagram = named(self._source.diagram)
        label = diagram.name
        ids = _page_ids(diagram)
        try:  # off the event loop; a page that renders over its cap ends the import before anything is stored
            markdowns = await asyncio.to_thread(lambda: [to_markdown(page, diagram) for page in diagram.pages])
        except DiagramDecodeError:
            raise ImportFailedError("unknown_error") from None
        for page, item in zip(diagram.pages, ids, strict=True):
            await writer.declare(item, page_title(diagram, page) or label)
        for page, item, markdown in zip(diagram.pages, ids, markdowns, strict=True):
            await writer.add(
                item,
                name=page_title(diagram, page) or label,
                original=page_original(page),
                markdown=markdown,
                original_media_type=MXFILE_MEDIA_TYPE,
            )
        log_event(
            logger,
            INFO,
            "api.imports.drawio_done",
            project_id=str(job.project_id),
            source_id=str(job.source_id),
            pages=len(diagram.pages),
            capped=diagram.capped,
        )


def _page_ids(diagram: Diagram) -> list[str]:
    """``page:<page id>`` per page; a repeated id gets its position so no two items collide."""

    seen: set[str] = set()
    ids: list[str] = []
    for index, page in enumerate(diagram.pages):
        item = f"page:{page.id}"
        if item in seen:
            item = f"page:{page.id}#{index}"
        seen.add(item)
        ids.append(item)
    return ids
