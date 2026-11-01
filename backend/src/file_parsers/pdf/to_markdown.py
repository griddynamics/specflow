"""Compatibility alias for ``file_parsers.convert`` (S04 switches the callers, S09 removes this)."""

from __future__ import annotations

from pathlib import Path

from file_parsers.convert import ParsedMarkdown, PdfParseError, file_to_markdown
from file_parsers.formats import PDF_MEDIA_TYPE

__all__ = ["ParsedMarkdown", "PdfParseError", "pdf_file_to_markdown"]


def pdf_file_to_markdown(path: Path) -> ParsedMarkdown:
    """``file_to_markdown(path, "application/pdf")``."""

    return file_to_markdown(path, PDF_MEDIA_TYPE)
