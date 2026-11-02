"""The conversion step of ``parse_document``: limits first, then ``file_to_markdown``.

Kept apart from ``activities`` so the activity module stays small and tests can swap the whole step
(``ParseActivities(parse=...)``). It runs in the parse executor thread and is blocking. Importing it
imports Docling (``file_parsers.convert``): only the worker-parse process does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from file_parsers.convert import ParsedMarkdown, PdfParseError, file_to_markdown
from file_parsers.engines.port import ProgressCallback
from file_parsers.formats import DocumentFormat, format_for_media_type
from file_parsers.limits import DocumentTooLargeError, check_pdf_file

ParseFunction = Callable[[Path, str, ProgressCallback | None], ParsedMarkdown]
"""``(path, media_type, on_progress)``: the blocking conversion the activity runs in its executor."""

STEM: Final = "original"


def convert(path: Path, media_type: str, on_progress: ProgressCallback | None = None) -> ParsedMarkdown:
    """Check the document's limits, then convert it with the engine for ``media_type``.

    A PDF over ``MAX_PDF_PAGES`` raises ``DocumentTooLargeError`` from its page tree, before any
    conversion; a PDF pdfium cannot open raises ``PdfParseError``. Every other type goes straight to
    ``file_to_markdown`` (it raises ``UnsupportedFormatError`` until the Office/web formats exist).
    """

    spec = format_for_media_type(media_type)
    if spec is not None and spec.format is DocumentFormat.PDF:
        try:
            check_pdf_file(path)
        except DocumentTooLargeError:
            raise
        except Exception as exc:
            raise PdfParseError(f"cannot parse PDF: {type(exc).__name__}") from exc
    return file_to_markdown(path, media_type, on_progress=on_progress)


def local_name(media_type: str) -> str:
    """The temporary file name for a download: ``original`` plus the format's extension (engines may sniff it)."""

    spec = format_for_media_type(media_type)
    return STEM + (spec.extension if spec is not None else "")


class Progress:
    """Pages done / total of the running conversion: written by the parse thread, read by the heartbeat."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: int | None = None
        self._total: int | None = None
        self._mark = time.monotonic()

    def begin(self) -> None:
        """The conversion starts now (the wait for the parse slot is over)."""

        with self._lock:
            self._mark = time.monotonic()

    def update(self, done: int, total: int) -> float:
        """Record a finished range and return the seconds it took (since the previous one)."""

        now = time.monotonic()
        with self._lock:
            took, self._mark = now - self._mark, now
            self._done, self._total = done, total
        return took

    def snapshot(self) -> tuple[int | None, int | None]:
        with self._lock:
            return self._done, self._total
