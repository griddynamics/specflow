"""Stream a raw document request body into the object store (AN 3.3 "Upload stream").

Accepted: PDF, DOCX, PPTX, XLSX, HTML, Markdown and CSV (``file_parsers.formats``). The
extension of the file name picks the canonical type; the ``Content-Type`` header must agree
with it (or be one of the browser quirks the table lists).

The stream checks only what the first bytes show (``%PDF-``, ZIP magic ``PK\\x03\\x04`` for
OOXML, UTF-8 and no NUL for text) and counts the bytes against the byte cap of the format
(N2: PDF 100 MB, OOXML 50 MB, HTML/Markdown/CSV 10 MB; 413 when exceeded). Deep checks (ZIP
inflation, cells, pages) belong to the parse activity.

No size is buffered beyond one 4 MiB buffer, no temp file, no multipart: SHA-256 and the size
are computed over the same bytes that are written. A body over the cap is refused as soon as
the counted bytes pass it, and the partial object is aborted.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from api.errors import ApiError
from file_parsers.formats import FORMATS, DocumentFormat, FormatSpec, format_for_upload
from storage.base import CHUNK_SIZE, MiB, ObjectStore, ObjectWriter, StoredObject

PDF_MAGIC: Final = b"%PDF-"
ZIP_MAGIC: Final = b"PK\x03\x04"
TEXT_HEAD_BYTES: Final = 4096  # the part of a text body that is checked for UTF-8

#: N2 byte caps per format. The Office and text caps equal the parser's own (``file_parsers.limits``,
#: ``MAX_OOXML_FILE_BYTES``, ``MAX_TEXT_FILE_BYTES``; a test keeps them equal): not imported here because the
#: api image has no PDFium, which that module needs.
MAX_BYTES: Final[dict[DocumentFormat, int]] = {
    DocumentFormat.PDF: 100 * MiB,
    DocumentFormat.DOCX: 50 * MiB,
    DocumentFormat.PPTX: 50 * MiB,
    DocumentFormat.XLSX: 50 * MiB,
    DocumentFormat.HTML: 10 * MiB,
    DocumentFormat.MARKDOWN: 10 * MiB,
    DocumentFormat.CSV: 10 * MiB,
}

_MAGIC: Final[dict[DocumentFormat, bytes]] = {
    DocumentFormat.PDF: PDF_MAGIC,
    DocumentFormat.DOCX: ZIP_MAGIC,
    DocumentFormat.PPTX: ZIP_MAGIC,
    DocumentFormat.XLSX: ZIP_MAGIC,
}


@dataclass(frozen=True, slots=True)
class Uploaded:
    key: str
    size: int
    content_hash: str


def accepted_types_text() -> str:
    """``application/pdf (.pdf), ...``: the accepted canonical types for the 415 message."""

    return ", ".join(f"{spec.media_type} ({spec.extension})" for spec in FORMATS)


def _unsupported(reason: str = "") -> ApiError:
    message = f"Unsupported file. Accepted types: {accepted_types_text()}."
    return ApiError(415, "unsupported_format", f"{reason} {message}".strip())


def too_large(limit: int) -> ApiError:
    return ApiError(
        413,
        "file_too_large",
        f"The file is larger than the {limit // MiB} MB limit for its type.",
        details={"max_bytes": limit},
    )


def accepted_format(header: str | None, filename: str) -> FormatSpec:
    """The format of an upload from its ``Content-Type`` header and file name, else 415."""

    spec = format_for_upload(header, filename)
    if spec is None:
        raise _unsupported()
    return spec


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length", "")
    return int(raw) if raw.isascii() and raw.isdigit() else None


class _Stream:
    """The state of one upload: hash, size, the first bytes until they are checked, one buffer."""

    def __init__(self, store: ObjectStore, key: str, spec: FormatSpec) -> None:
        self._store = store
        self._key = key
        self._spec = spec
        self._magic = _MAGIC.get(spec.format)
        self._need = len(self._magic) if self._magic else TEXT_HEAD_BYTES
        self._limit = MAX_BYTES[spec.format]
        self._digest = hashlib.sha256()
        self._head = bytearray()
        self._buffer = bytearray()
        self._writer: ObjectWriter | None = None
        self.size = 0

    @property
    def content_hash(self) -> str:
        return self._digest.hexdigest()

    def _check_head(self, *, complete: bool) -> None:
        """Refuse a body whose first bytes do not fit the format (415)."""

        head = bytes(self._head)
        if self._magic is not None:
            if not head.startswith(self._magic):
                raise _unsupported("The file content does not match its type.")
            return
        if head.startswith((PDF_MAGIC, ZIP_MAGIC)) or b"\x00" in head:
            raise _unsupported("The file content is not text.")
        try:  # a multi-byte character cut at the end of the head is fine unless the body ended
            codecs.getincrementaldecoder("utf-8")().decode(head, final=complete)
        except UnicodeDecodeError:
            raise _unsupported("The file is not UTF-8 text.") from None

    async def _open(self, *, complete: bool) -> ObjectWriter:
        self._check_head(complete=complete)
        writer = await self._store.open_writer(self._key, content_type=self._spec.media_type)
        self._writer = writer
        self._buffer += self._head
        self._head.clear()
        return writer

    async def feed(self, piece: bytes) -> None:
        """Take one piece of the body; the writer opens once the first bytes are confirmed."""

        if not piece:
            return
        self.size += len(piece)
        if self.size > self._limit:
            raise too_large(self._limit)
        self._digest.update(piece)
        writer = self._writer
        if writer is None:
            self._head += piece
            if len(self._head) < self._need:
                return
            writer = await self._open(complete=False)
        else:
            self._buffer += piece
        if len(self._buffer) >= CHUNK_SIZE:
            await writer.write(bytes(self._buffer))
            self._buffer.clear()

    async def finish(self) -> StoredObject:
        """Flush the buffer and close the object; an empty or mismatching body is refused."""

        writer = self._writer
        if writer is None:
            if self.size == 0:
                raise ApiError(400, "empty_file", "The uploaded file is empty.")
            writer = await self._open(complete=True)  # a body shorter than the checked head
        if self._buffer:
            await writer.write(bytes(self._buffer))
            self._buffer.clear()
        return await writer.close()

    async def abort(self) -> None:
        if self._writer is not None:
            await asyncio.shield(self._writer.abort())


async def stream_upload(request: Request, store: ObjectStore, key: str, spec: FormatSpec) -> Uploaded:
    """Write the body to ``key``; on any failure (also a disconnect) the writer is aborted.

    Raises ``ApiError`` 413 when the body passes the byte cap of the format (a larger declared
    ``Content-Length`` is refused before the first byte is read), 415 when the first bytes do
    not fit the format and 400 when it is empty. The writer is opened only after the first
    bytes are confirmed.
    """

    declared = _declared_length(request)
    if declared is not None and declared > MAX_BYTES[spec.format]:
        raise too_large(MAX_BYTES[spec.format])
    upload = _Stream(store, key, spec)
    try:
        async for piece in request.stream():
            await upload.feed(piece)
        stored = await upload.finish()
    except BaseException:
        await upload.abort()
        raise
    return Uploaded(stored.key, stored.size, upload.content_hash)
