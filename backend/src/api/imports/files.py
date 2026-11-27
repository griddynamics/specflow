"""The lane table of the importers that store binary originals (Box, Dropbox; decision DB18 in
plans/DROPBOX/DROPBOX-PLAN.md): which files go to the file parsers, with which media type and which size cap.

A file is taken by its extension: a parser format of ``file_parsers.formats`` (PDF, DOCX, PPTX, XLSX, HTML, MD, CSV),
or ``.txt``, stored as ``text/markdown`` as an upload of it is. The caps are the Local files caps
(docs/file-parsers/formats.md): PDF 100 MiB, DOCX/PPTX/XLSX 50 MiB, HTML, MD, CSV, TXT 10 MiB. A file over its cap by
its listed size is not taken; the importer holds the cap again while the bytes stream (``ImportWriter.add_file``).

Pure: no I/O, no log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from file_parsers.formats import FORMATS, DocumentFormat

MAX_PDF_BYTES: Final = 100 * 1024 * 1024  # a larger PDF (by its listed size) is skipped; the cap holds while streaming
MAX_OOXML_BYTES: Final = 50 * 1024 * 1024  # DOCX, PPTX, XLSX
MAX_TEXT_BYTES: Final = 10 * 1024 * 1024  # HTML, MD, CSV, TXT
TEXT_EXTENSION: Final = "txt"
TEXT_MEDIA_TYPE: Final = "text/markdown"  # a ``.txt`` is stored as Markdown, as an upload of it is

_FORMAT_CAPS: Final = {
    DocumentFormat.PDF: MAX_PDF_BYTES,
    DocumentFormat.DOCX: MAX_OOXML_BYTES,
    DocumentFormat.PPTX: MAX_OOXML_BYTES,
    DocumentFormat.XLSX: MAX_OOXML_BYTES,
}
_BY_EXTENSION: Final = {spec.extension.lstrip("."): spec for spec in FORMATS}
# The ``category`` of a listed file in a check (``BoxCheckOut.files``, ``DropboxCheckOut.files``): its parser format,
# ``.txt`` is ``text``. Each importer adds its own native kind (a Box Note, a Paper doc).
FileCategory = Literal["pdf", "word", "powerpoint", "excel", "html", "markdown", "csv", "text"]
CATEGORIES: Final[dict[DocumentFormat, FileCategory]] = {
    DocumentFormat.PDF: "pdf",
    DocumentFormat.DOCX: "word",
    DocumentFormat.PPTX: "powerpoint",
    DocumentFormat.XLSX: "excel",
    DocumentFormat.HTML: "html",
    DocumentFormat.MARKDOWN: "markdown",
    DocumentFormat.CSV: "csv",
}


@dataclass(frozen=True)
class FileLane:
    """How one file is imported as an original: its ``media_type``, its size ``cap`` and its parser ``format``
    (``None`` for ``.txt``, which is parsed as Markdown); ``category`` is what a check lists it as."""

    media_type: str
    cap: int
    format: DocumentFormat | None

    @property
    def category(self) -> FileCategory:
        return "text" if self.format is None else CATEGORIES.get(self.format, "text")


def extension_of(name: str) -> str:
    """The lower-case extension of a file name without the dot; ``""`` when it has none."""

    stem, dot, extension = name.rpartition(".")
    return extension.lower() if dot and stem else ""


def lane_of(extension: str) -> FileLane | None:
    """The lane of a file type by its extension (lower case, no dot); ``None`` when it is not imported as an original.
    The cap is read at call time (tests patch ``MAX_TEXT_BYTES``)."""

    if extension == TEXT_EXTENSION:
        return FileLane(TEXT_MEDIA_TYPE, MAX_TEXT_BYTES, None)
    spec = _BY_EXTENSION.get(extension)
    if spec is None:
        return None
    return FileLane(spec.media_type, _FORMAT_CAPS.get(spec.format, MAX_TEXT_BYTES), spec.format)


def classify(extension: str, size: int) -> FileLane | None:
    """The lane of one file by its extension and listed size: ``None`` (skipped) for another type or a file over its
    cap."""

    lane = lane_of(extension)
    if lane is None or size > lane.cap:
        return None
    return lane
