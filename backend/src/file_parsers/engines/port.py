"""The ``DocumentEngine`` port: what a document-to-content engine offers the parser.

Implementations (Docling) return content and do not rank headings or render the stored Markdown; the facade
(``file_parsers.convert``), ``PDFParser`` and ``OfficeStructure`` do that. A PDF comes back as per-page content
(``extract``); DOCX, PPTX, XLSX, HTML, Markdown and CSV have no pages to render and come back as
``OfficeContent`` (``extract_office``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from file_parsers.office.model import OfficeContent
from file_parsers.pdf.schema import PDFDocument

PageRange = tuple[int, int]
"""First and last page, 1-based and inclusive: ``(1, 10)`` is pages 1 to 10."""

ProgressCallback = Callable[[int, int], None]
"""``(pages_done, pages_total)``. ``pages_total`` is the size of the requested range.

Contract: called from the thread that runs ``extract``, never concurrently, with ``pages_done``
non-decreasing and ``pages_done <= pages_total``; the last call has ``pages_done == pages_total``.
An engine that cannot report in between calls it once, at the end. The callback must be quick and
must not raise; the parse activity turns it into Temporal heartbeats.
"""


class UnsupportedFormatError(Exception):
    """No engine or no path exists for this media type."""

    def __init__(self, media_type: str) -> None:
        super().__init__(f"unsupported media type: {media_type!r}")
        self.media_type = media_type


@runtime_checkable
class DocumentEngine(Protocol):
    """Turns a file of one of ``formats`` into per-page content."""

    @property
    def engine_id(self) -> str:
        """Stored in ``documents.parser``; changes whenever the output may change."""
        ...

    @property
    def formats(self) -> frozenset[str]:
        """The canonical media types (``file_parsers.formats``) this engine accepts."""
        ...

    def extract(
        self,
        path: Path,
        media_type: str,
        page_range: PageRange | None = None,
        *,
        images: bool = False,
        assets_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PDFDocument:
        """Extract ``page_range`` (``None`` = the whole document) of the file at ``path``.

        Blocking and CPU-bound: call it from a worker thread. ``images=True`` writes figure PNGs
        into ``assets_dir`` (required then) and puts ``![pN.figK](<dir>/<file>)`` placeholders in
        the page Markdown; ``images=False`` writes none. Raises ``UnsupportedFormatError`` when
        ``media_type`` is not in ``formats``, ``ValueError`` for an empty or out-of-bounds range
        or ``images`` without ``assets_dir``. Any other failure propagates unchanged.
        """
        ...

    def extract_office(
        self,
        path: Path,
        media_type: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> OfficeContent:
        """Extract the file at ``path`` of an Office or web ``media_type`` (everything in ``formats`` but PDF).

        One section for DOCX, HTML, Markdown and CSV; one per slide (PPTX) or sheet (XLSX, with its name).
        Nothing is fetched and no image is written. ``on_progress`` is called once, at the end, with
        ``(sections, sections)``. Raises ``UnsupportedFormatError`` when ``media_type`` is PDF or not in
        ``formats``; the caller inspects the file first (``file_parsers.office.guard``), so a failure here
        propagates unchanged.
        """
        ...
