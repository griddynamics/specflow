"""Stream a raw PDF request body into the object store (AN 3.3 "Upload stream").

No size limit, no temp file, no multipart: memory is bounded by one 4 MiB
buffer. SHA-256 and the size are computed over the same bytes that are written.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from api.errors import ApiError
from storage.base import CHUNK_SIZE, ObjectStore, ObjectWriter, StoredObject

PDF_MEDIA_TYPE: Final = "application/pdf"
PDF_MAGIC: Final = b"%PDF-"


@dataclass(frozen=True, slots=True)
class Uploaded:
    key: str
    size: int
    content_hash: str


def is_pdf_content_type(header: str | None) -> bool:
    return (header or "").split(";", 1)[0].strip().lower() == PDF_MEDIA_TYPE


def _unsupported() -> ApiError:
    return ApiError(415, "unsupported_format", "The body must be a PDF sent as application/pdf.")


class _PdfStream:
    """The state of one upload: hash, size, the first bytes until the magic is checked, one buffer."""

    def __init__(self, store: ObjectStore, key: str) -> None:
        self._store = store
        self._key = key
        self._digest = hashlib.sha256()
        self._head = bytearray()
        self._buffer = bytearray()
        self._writer: ObjectWriter | None = None
        self.size = 0

    @property
    def content_hash(self) -> str:
        return self._digest.hexdigest()

    async def feed(self, piece: bytes) -> None:
        """Take one piece of the body; the writer opens once the ``%PDF-`` magic is confirmed."""

        if not piece:
            return
        self._digest.update(piece)
        self.size += len(piece)
        if self._writer is None:
            self._head += piece
            if len(self._head) < len(PDF_MAGIC):
                return
            if not self._head.startswith(PDF_MAGIC):
                raise _unsupported()
            self._writer = await self._store.open_writer(self._key, content_type=PDF_MEDIA_TYPE)
            self._buffer += self._head
            self._head.clear()
        else:
            self._buffer += piece
        if len(self._buffer) >= CHUNK_SIZE:
            await self._writer.write(bytes(self._buffer))
            self._buffer.clear()

    async def finish(self) -> StoredObject:
        """Flush the buffer and close the object; an empty or non-PDF body is refused."""

        if self._writer is None:
            if self.size == 0:
                raise ApiError(400, "empty_file", "The uploaded file is empty.")
            raise _unsupported()
        if self._buffer:
            await self._writer.write(bytes(self._buffer))
            self._buffer.clear()
        return await self._writer.close()

    async def abort(self) -> None:
        if self._writer is not None:
            await asyncio.shield(self._writer.abort())


async def stream_pdf(request: Request, store: ObjectStore, key: str) -> Uploaded:
    """Write the body to ``key``; on any failure (also a disconnect) the writer is aborted.

    Raises ``ApiError`` 415 when the body lacks the ``%PDF-`` magic and 400 when
    it is empty. The writer is opened only after the magic is confirmed.
    """

    upload = _PdfStream(store, key)
    try:
        async for piece in request.stream():
            await upload.feed(piece)
        stored = await upload.finish()
    except BaseException:
        await upload.abort()
        raise
    return Uploaded(stored.key, stored.size, upload.content_hash)
