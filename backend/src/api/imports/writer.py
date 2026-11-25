"""``ImportWriter``: how an importer stores what it read (docs/mission-control/integrations-api.md "Importer seam").

For each page, issue or note the importer gives the original bytes and the Markdown it converted them to. The writer
stores the original in object storage, then in one transaction inserts the ``documents`` row (``origin=connection``,
``status=parsed``, ``parser=<kind>-convert``, held by the source) and finishes the source's ``connection_items`` row,
the progress the Sources status reads. The graph build reads such a document like any other (``db.sources``
``list_graph_corpus``); no parse workflow runs. The source row is locked ``FOR SHARE`` during the write, so a removal
waits for it and a later write finds the source gone (``SourceGoneError``). No transaction is open across the storage
write.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from api.contained import Contained
from api.imports.model import ImportJob, SourceGoneError
from db import imports as imports_db
from db import sources as sources_db
from storage.base import ObjectAlreadyExistsError, ObjectStore
from storage.keys import document_original, document_original_json

JSON_MEDIA_TYPE = "application/json"
MARKDOWN_MEDIA_TYPE = "text/markdown"


class ImportWriter:
    """The writes of one import (one source)."""

    def __init__(
        self, job: ImportJob, *, transaction: Callable[[], AbstractAsyncContextManager[Any]], store: ObjectStore
    ) -> None:
        self._job = job
        self._transaction = transaction
        self._store = store
        self._parser = f"{job.kind}-convert"

    async def declare(self, external_id: str, name: str) -> None:
        """List an item the importer is going to read (``pending``), so progress shows its total early."""

        async with self._transaction() as session:
            if await sources_db.lock_importing_connection(session, self._job.project_id, self._job.source_id) is None:
                raise SourceGoneError
            await imports_db.declare_item(
                session,
                self._job.project_id,
                self._job.source_id,
                external_id=external_id,
                name=name,
                mime_type=MARKDOWN_MEDIA_TYPE,
            )

    async def add(
        self,
        external_id: str,
        *,
        name: str,
        original: bytes,
        markdown: str,
        original_media_type: str = JSON_MEDIA_TYPE,
    ) -> Literal["added", "duplicate"]:
        """Store one item: its original, its Markdown as a ``parsed`` document, its item row ``processed``. The same
        content already in the project (any source) is ``duplicate``: its document is linked and nothing is stored.
        ``SourceGoneError`` when the source is gone."""

        project_id = self._job.project_id
        document_id = uuid.uuid4()
        key = (
            document_original_json(str(project_id), str(document_id))
            if original_media_type == JSON_MEDIA_TYPE
            else document_original(str(project_id), str(document_id), original_media_type)
        )
        await self._put(key, original, original_media_type)
        try:
            created = await self._insert(
                item=(external_id, name),
                stored=(key, document_id, original_media_type),
                original=original,
                markdown=markdown,
            )
        except SourceGoneError:
            await self._drop(key)  # the source is gone: the object is an orphan
            raise
        if created:
            return "added"
        await self._drop(key)  # the content was in the project already: the object is an orphan
        return "duplicate"

    async def _insert(
        self,
        *,
        item: tuple[str, str],
        stored: tuple[str, uuid.UUID, str],
        original: bytes,
        markdown: str,
    ) -> bool:
        """The document and its item in one transaction; whether the document is new."""

        project_id, source_id = self._job.project_id, self._job.source_id
        (external_id, name), (key, document_id, media_type) = item, stored
        async with self._transaction() as session:
            if await sources_db.lock_importing_connection(session, project_id, source_id) is None:
                raise SourceGoneError
            item_id = await imports_db.declare_item(
                session, project_id, source_id, external_id=external_id, name=name, mime_type=media_type
            )
            document, created = await sources_db.insert_connection_document(
                session,
                project_id=project_id,
                file=sources_db.DocumentFile(
                    document_id=document_id,
                    content_hash=hashlib.sha256(original).hexdigest(),
                    filename=name,
                    size_bytes=len(original),
                    original_key=key,
                ),
                media_type=media_type,
                markdown=markdown,
                parser=self._parser,
                status="parsed",
                source_id=source_id,
            )
            await sources_db.finish_item(
                session, item_id, "processed" if created else "duplicate", document_id=document.id
            )
        return created

    async def _drop(self, key: str) -> None:
        with Contained():  # a leftover object is harmless
            await self._store.delete(key)

    async def fail(self, external_id: str, name: str, *, reason: str, step: str) -> None:
        """End one item as ``error`` with a closed reason and step (the import goes on with the next item)."""

        async with self._transaction() as session:
            if await sources_db.lock_importing_connection(session, self._job.project_id, self._job.source_id) is None:
                raise SourceGoneError
            item_id = await imports_db.declare_item(
                session,
                self._job.project_id,
                self._job.source_id,
                external_id=external_id,
                name=name,
                mime_type=MARKDOWN_MEDIA_TYPE,
            )
            await sources_db.finish_item(session, item_id, "error", error_reason=reason, error_step=step)

    async def _put(self, key: str, data: bytes, content_type: str) -> None:
        writer = await self._store.open_writer(key, content_type=content_type)
        try:
            await writer.write(data)
            await writer.close()
        except ObjectAlreadyExistsError:
            return
        except BaseException:
            await writer.abort()
            raise
