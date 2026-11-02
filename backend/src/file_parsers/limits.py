"""Size limits of the parser and the two error types the parse activity maps to fixed codes.

``DocumentTooLargeError`` becomes ``document_too_large`` and ``DocumentParseFailedError`` becomes
``document_parse_failed`` (DESIGN N3); both are final: a retry would fail the same way. A PDF that
cannot be read at all keeps ``PdfParseError`` (``pdf_parse_failed``). The Office and web limits (DESIGN N2) sit
next to the PDF cap; ``file_parsers.office.guard`` enforces them while it reads a file, never from declared sizes.
"""

from __future__ import annotations

from pathlib import Path

from file_parsers.pdf.pdfium_typed import page_count

MAX_PDF_PAGES = 500
"""Longest PDF the parser accepts (DESIGN D3): 500 pages x 2.86 s is about 24 minutes."""

MAX_SLIDES = 500
"""Most slides a PPTX may have (DESIGN D3), counted from the archive's entry names."""

MAX_OOXML_INFLATED_BYTES = 200 * 1024 * 1024
"""Most bytes all entries of a DOCX/PPTX/XLSX may inflate to (DESIGN N2), counted while inflating."""

MAX_OOXML_RATIO = 100
"""Largest inflated:compressed ratio of one entry (DESIGN N2), measured on the bytes actually read."""

RATIO_FLOOR_BYTES = 1024 * 1024
"""The ratio applies once an entry has inflated past this size: a tiny repetitive part cannot hurt."""

MAX_OOXML_ENTRIES = 10_000
"""Most entries an OOXML archive may list (a real document has dozens; this bounds the central directory)."""

MAX_OOXML_FILE_BYTES = 50 * 1024 * 1024
"""Largest DOCX/PPTX/XLSX on disk (DESIGN N2; the upload cap). The guard holds it too: the Drive import streams
files without the upload check."""

MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024
"""Largest HTML, Markdown or CSV on disk (DESIGN N2), held by the guard for the same reason."""

MAX_CELLS = 200_000
"""Most cells of an XLSX (all sheets) or a CSV (DESIGN N2), counted while reading, never from the dimension tag."""

MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
"""Longest Markdown an Office or web conversion (DOCX, PPTX, XLSX, HTML, Markdown, CSV) may produce, in UTF-8 bytes
(DESIGN N2; the same cap as drive-convert). A PDF is bounded by ``MAX_PDF_PAGES`` instead and is not checked."""


class DocumentTooLargeError(Exception):
    """The document is over a limit (pages, size): it is refused before or while it is converted."""

    def __init__(self, what: str, actual: int, limit: int) -> None:
        super().__init__(f"{what}: {actual} is over the limit of {limit}")
        self.what = what
        self.actual = actual
        self.limit = limit


class DocumentParseFailedError(Exception):
    """A document that is not a PDF could not be parsed (malformed, unreadable, unsupported content)."""


def check_pdf_pages(pages: int) -> None:
    """Raise ``DocumentTooLargeError`` when ``pages`` is over ``MAX_PDF_PAGES``."""

    if pages > MAX_PDF_PAGES:
        raise DocumentTooLargeError("pages", pages, MAX_PDF_PAGES)


def check_pdf_file(path: Path) -> int:
    """Count the pages of the PDF at ``path`` (page tree only), enforce the cap and return the count.

    Call it before any conversion. A file pdfium cannot open raises its own error (the facade turns
    it into ``PdfParseError``).
    """

    pages = page_count(path)
    check_pdf_pages(pages)
    return pages


def check_slides(slides: int) -> None:
    """Raise ``DocumentTooLargeError`` when ``slides`` is over ``MAX_SLIDES``."""

    if slides > MAX_SLIDES:
        raise DocumentTooLargeError("slides", slides, MAX_SLIDES)


def check_cells(cells: int) -> None:
    """Raise ``DocumentTooLargeError`` when ``cells`` is over ``MAX_CELLS``."""

    if cells > MAX_CELLS:
        raise DocumentTooLargeError("cells", cells, MAX_CELLS)


def check_markdown_size(markdown: str) -> None:
    """Raise ``DocumentTooLargeError`` when the UTF-8 size of ``markdown`` is over ``MAX_MARKDOWN_BYTES``."""

    size = len(markdown.encode("utf-8"))
    if size > MAX_MARKDOWN_BYTES:
        raise DocumentTooLargeError("markdown bytes", size, MAX_MARKDOWN_BYTES)
