"""``OfficeStructure``: the stored Markdown of an Office or web document (DESIGN 'Post-processing').

- DOCX: Docling writes Word "Heading 1" as ``##`` and keeps ``#`` for a Title style. When the document has no
  ``#`` heading, every heading moves up one level (measured: 0/358 exact levels become 358/358). HTML and
  Markdown keep the levels they were authored with (Docling reproduces them exactly), so they are not shifted.
- PPTX: ``<!-- source-page: N -->`` before each slide (the slide title is already a ``#`` heading).
- XLSX: the same marker before each sheet, followed by the sheet name as a ``#`` heading.
- DOCX, HTML, Markdown and CSV have no pages and get no marker.

Output is ``"\\n\\n"``-joined sections with one trailing newline, the shape ``PDFRenderer`` writes.
"""

from __future__ import annotations

import re

from file_parsers.formats import DocumentFormat
from file_parsers.office.model import OfficeContent, OfficeSection

_HEADING = re.compile(r"^(#{1,6})(?=[ \t])")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_NAME_BREAKS = re.compile(r"\s+")


class OfficeStructure:
    """Renders ``OfficeContent`` as the Markdown that is stored in ``documents.markdown``."""

    def render(self, content: OfficeContent) -> str:
        blocks = [self._block(content.format, section) for section in content.sections]
        text = "\n\n".join(block for block in blocks if block)
        if content.format is DocumentFormat.DOCX:
            text = shift_headings(text)
        return text + "\n" if text else ""

    @staticmethod
    def _block(document_format: DocumentFormat, section: OfficeSection) -> str:
        if document_format is DocumentFormat.PPTX:
            return f"{_marker(section.number)}\n\n{section.markdown}".strip()
        if document_format is DocumentFormat.XLSX:
            name = _NAME_BREAKS.sub(" ", section.name or f"Sheet {section.number}").strip()
            return f"{_marker(section.number)}\n\n# {name}\n\n{section.markdown}".strip()
        return section.markdown


def _marker(number: int) -> str:
    return f"<!-- source-page: {number} -->"


def shift_headings(markdown: str) -> str:
    """Move every heading up one level when the text has no ``#`` heading; otherwise return it unchanged.

    Lines inside fenced code blocks are not headings.
    """

    lines = markdown.split("\n")
    headings = _heading_lines(lines)
    if not headings or any(len(match.group(1)) == 1 for _, match in headings):
        return markdown
    for index, _ in headings:
        lines[index] = lines[index][1:]  # one '#' fewer
    return "\n".join(lines)


def _heading_lines(lines: list[str]) -> list[tuple[int, re.Match[str]]]:
    found: list[tuple[int, re.Match[str]]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and (match := _HEADING.match(line)):
            found.append((index, match))
    return found
