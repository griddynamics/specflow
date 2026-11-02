"""What a ``DocumentEngine`` returns for the Office and web formats.

PDF has ``PDFDocument`` (pages the renderer and the enrichers work on). These formats have no pages to render:
DOCX, HTML, Markdown and CSV come back as one unnumbered section; a PPTX has one section per slide and an
XLSX one per sheet.
"""

from __future__ import annotations

from dataclasses import dataclass

from file_parsers.formats import DocumentFormat


@dataclass(frozen=True, slots=True)
class OfficeSection:
    """One part of the document: a slide, a sheet, or the whole document.

    ``number`` is 1-based (the slide or sheet position). ``name`` is the sheet name (XLSX only).
    """

    number: int
    markdown: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class OfficeContent:
    """The sections of one converted document, in order."""

    format: DocumentFormat
    sections: tuple[OfficeSection, ...]
