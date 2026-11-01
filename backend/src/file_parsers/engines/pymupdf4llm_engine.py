"""The current PDF engine: pymupdf4llm behind the ``DocumentEngine`` port (replaced in S03)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from file_parsers.engines.port import PageRange, ProgressCallback, UnsupportedFormatError
from file_parsers.formats import PDF_MEDIA_TYPE
from file_parsers.pdf.markdown_extractor import PdfMarkdownExtractor
from file_parsers.pdf.pymupdf_typed import page_count
from file_parsers.pdf.schema import PDFDocument

ENGINE_ID = "pymupdf4llm"


class PyMuPDF4LLMEngine:
    engine_id = ENGINE_ID
    formats = frozenset({PDF_MEDIA_TYPE})

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
        if media_type not in self.formats:
            raise UnsupportedFormatError(media_type)
        if images and assets_dir is None:
            raise ValueError("images=True needs assets_dir")
        pages = None
        total: int | None = None
        if page_range is not None:
            first, last = page_range
            if first < 1 or last < first:
                raise ValueError(f"invalid page range {page_range}")
            pages_in_file = page_count(path)
            if last > pages_in_file:
                raise ValueError(f"page range {page_range} is beyond the {pages_in_file} pages of the document")
            pages = list(range(first - 1, last))
            total = len(pages)
        with tempfile.TemporaryDirectory(prefix="specflow-pdf-assets-") as scratch:
            target = assets_dir if assets_dir is not None else Path(scratch) / "images"
            document = PdfMarkdownExtractor(path, target).extract(pages, images=images)
        if on_progress is not None:
            done = total if total is not None else len(document.pages)
            on_progress(done, done)
        return document
