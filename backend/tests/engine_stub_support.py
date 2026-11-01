"""A pre-configured ``DocumentEngine`` for tests: it returns the pages a factory builds and records every call."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from file_parsers.engines.port import PageRange, ProgressCallback
from file_parsers.pdf.schema import PDFDocument, PDFPageContent

PDF = "application/pdf"

PageFactory = Callable[[PageRange | None], list[PDFPageContent]]


def one_page(markdown: str) -> PageFactory:
    """Always one page, number 1, whatever range is asked for."""

    return lambda _range: [PDFPageContent(page_number=1, markdown=markdown, figures=[])]


class StubEngine:
    formats = frozenset({PDF})

    def __init__(self, pages: PageFactory, engine_id: str = "stub@1") -> None:
        self.engine_id = engine_id
        self._pages = pages
        self.calls: list[tuple[PageRange | None, bool, Path | None]] = []

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
        del media_type, on_progress
        self.calls.append((page_range, images, assets_dir))
        return PDFDocument(source_path=path, pages=self._pages(page_range))
