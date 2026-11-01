"""Size limits of the parser and the two error types the parse activity maps to fixed codes.

``DocumentTooLargeError`` becomes ``document_too_large`` and ``DocumentParseFailedError`` becomes
``document_parse_failed`` (DESIGN N3); both are final: a retry would fail the same way. A PDF that
cannot be read at all keeps ``PdfParseError`` (``pdf_parse_failed``). The Office and web limits
(inflated bytes, cells, output size) are added next to the PDF cap by the Office/web session.
"""

from __future__ import annotations

from pathlib import Path

from file_parsers.pdf.pdfium_typed import page_count

MAX_PDF_PAGES = 500
"""Longest PDF the parser accepts (DESIGN D3): 500 pages x 2.86 s is about 24 minutes."""


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
