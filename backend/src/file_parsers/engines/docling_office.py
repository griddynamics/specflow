"""Docling's document model to ``OfficeContent`` (DOCX, PPTX, XLSX, HTML, Markdown, CSV).

PPTX and XLSX come back as one Docling page per slide or sheet, exported page by page; the sheet names are the
``sheet`` groups of the document, in page order. Everything else is exported whole. The export options are the
PDF ones (raw ``_``/``<``/``>``, compact tables). Each picture keeps a placeholder in the shape the PDF path
writes, although no image file is stored: ``![pN.figK](images/pN.figK.png)`` with N the slide or sheet number, and
``![figK](images/figK.png)`` for formats without pages (K counts from 0 per section).
"""

from __future__ import annotations

import itertools
import re

from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.labels import GroupLabel

from file_parsers.formats import DocumentFormat
from file_parsers.office.model import OfficeContent, OfficeSection

PAGED_FORMATS = frozenset({DocumentFormat.PPTX, DocumentFormat.XLSX})
_BLANK_RUN = re.compile(r"\n{3,}")
_IMAGE_MARK = "￼SPECFLOW-IMAGE￼"


def to_office_content(document: DoclingDocument, document_format: DocumentFormat) -> OfficeContent:
    """Build the sections of ``document``: per page for a deck or workbook, else one."""

    if document_format not in PAGED_FORMATS:
        return OfficeContent(document_format, (OfficeSection(number=1, markdown=_export(document, None, None)),))
    names = [group.name for group in document.groups if group.label is GroupLabel.SHEET]
    sections: list[OfficeSection] = []
    for position, page_number in enumerate(sorted(document.pages), start=1):
        name = names[position - 1] if document_format is DocumentFormat.XLSX and position <= len(names) else None
        sections.append(OfficeSection(number=position, markdown=_export(document, page_number, position), name=name))
    return OfficeContent(document_format, tuple(sections))


def _export(document: DoclingDocument, page_number: int | None, section_number: int | None) -> str:
    markdown = document.export_to_markdown(
        page_no=page_number,
        escape_underscores=False,
        escape_html=False,
        compact_tables=True,
        image_placeholder=_IMAGE_MARK,
    )
    counter = itertools.count()

    def place(_: re.Match[str]) -> str:
        index = next(counter)
        figure_id = f"fig{index}" if section_number is None else f"p{section_number}.fig{index}"
        return f"![{figure_id}](images/{figure_id}.png)"

    markdown = re.sub(re.escape(_IMAGE_MARK), place, markdown)
    markdown = "\n".join(line.rstrip() for line in markdown.splitlines())
    return _BLANK_RUN.sub("\n\n", markdown).strip()
