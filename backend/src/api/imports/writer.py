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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from api.contained import Contained
from api.imports.model import ImportJob, OriginalTooLargeError, SourceGoneError
from db import imports as imports_db
from db import sources as sources_db
from storage.base import ObjectAlreadyExistsError, ObjectStore, ObjectWriter
from storage.keys import document_original, document_original_json

JSON_MEDIA_TYPE = "application/json"
MARKDOWN_MEDIA_TYPE = "text/markdown"

# Starts the parse of a document the writer stored unparsed (``add_file``): ``(project_id, document_id)``; ``True`` when
# the workflow started. ``ImportRunner.use_parse_starter`` sets it (the app's lifespan hands in
# ``api.batch_files.start_parse_or_mark_failed``); a start that fails marks the document ``failed``, as for an upload.
ParseStarter = Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]]


async def _stream(chunks: AsyncIterator[bytes], writer: ObjectWriter, max_bytes: int) -> tuple[str, int]:
    """Write ``chunks`` to ``writer``; the SHA-256 (hex) and the size. ``OriginalTooLargeError`` past ``max_bytes``."""

    digest, size = hashlib.sha256(), 0
    async for chunk in chunks:
        size += len(chunk)
        if size > max_bytes:
            raise OriginalTooLargeError
        digest.update(chunk)
        await writer.write(chunk)
    return digest.hexdigest(), size


class ImportWriter:
    """The writes of one import (one source)."""

    def __init__(
        self,
        job: ImportJob,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        store: ObjectStore,
        start_parse: ParseStarter | None = None,
    ) -> None:
        self._job = job
        self._transaction = transaction
        self._store = store
        self._start_parse = start_parse
        self._parser = f"{job.kind}-convert"

    async def declare(self, external_id: str, name: str, *, media_type: str = MARKDOWN_MEDIA_TYPE) -> None:
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
                mime_type=media_type,
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

    async def add_file(
        self,
        external_id: str,
        *,
        name: str,
        chunks: AsyncIterator[bytes],
        media_type: str,
        max_bytes: int,
    ) -> Literal["added", "duplicate"]:
        """Store one binary original that the parser reads (decision B12): stream ``chunks`` to the originals
        (``storage.keys.document_original``) while hashing them (SHA-256), insert the document ``processing`` with no
        parser and no Markdown, finish the item ``processed``, then start the parse (``ParseStarter``; ``worker-parse``
        gets the bytes, never a token). The same bytes already in the project (any source) are ``duplicate``: the
        object is dropped, the item links the other document and no parse starts. ``OriginalTooLargeError`` when
        ``chunks`` pass ``max_bytes`` (the object is dropped, nothing is stored); ``SourceGoneError`` when the source
        is gone, or any other failure of the insert (the object is dropped)."""

        project_id, document_id = self._job.project_id, uuid.uuid4()
        key = document_original(str(project_id), str(document_id), media_type)
        writer = await self._store.open_writer(key, content_type=media_type)
        try:
            digest, size = await _stream(chunks, writer, max_bytes)
            await writer.close()
        except BaseException:
            await writer.abort()  # the partial object goes; nothing was inserted
            raise
        try:
            created = await self._insert_document(
                item=(external_id, name),
                stored=(key, document_id, media_type),
                content=(digest, size),
                parsed=None,
            )
        except BaseException:
            await self._drop(key)  # nothing was inserted: the stored object is an orphan, whatever the failure
            raise
        if not created:
            await self._drop(key)  # the bytes were in the project already: the object is an orphan
            return "duplicate"
        if self._start_parse is not None:
            await self._start_parse(project_id, document_id)
        return "added"

    async def skip(self, external_id: str, name: str) -> None:
        """End one item as ``skipped`` (a type or size the importer does not take); the import goes on."""

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
            await sources_db.finish_item(session, item_id, "skipped")

    async def _insert(
        self,
        *,
        item: tuple[str, str],
        stored: tuple[str, uuid.UUID, str],
        original: bytes,
        markdown: str,
    ) -> bool:
        """The document and its item in one transaction; whether the document is new."""

        return await self._insert_document(
            item=item,
            stored=stored,
            content=(hashlib.sha256(original).hexdigest(), len(original)),
            parsed=markdown,
        )

    async def _insert_document(
        self,
        *,
        item: tuple[str, str],
        stored: tuple[str, uuid.UUID, str],
        content: tuple[str, int],
        parsed: str | None,
    ) -> bool:
        """The document and its item in one transaction; whether the document is new. ``parsed`` is the Markdown of an
        item converted here (document ``parsed``, parser ``<kind>-convert``); ``None`` is an original the parser has
        yet to read (document ``processing``, no parser)."""

        project_id, source_id = self._job.project_id, self._job.source_id
        (external_id, name), (key, document_id, media_type) = item, stored
        content_hash, size = content
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
                    content_hash=content_hash,
                    filename=name,
                    size_bytes=size,
                    original_key=key,
                ),
                media_type=media_type,
                markdown=parsed,
                parser=None if parsed is None else self._parser,
                status="processing" if parsed is None else "parsed",
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
