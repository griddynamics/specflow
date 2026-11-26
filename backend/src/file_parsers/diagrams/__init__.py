"""Diagram files and links (diagrams.net / draw.io) as Markdown. Stdlib only; the API may import this package."""

from __future__ import annotations

from file_parsers.diagrams.decode import decode, decode_link_payload
from file_parsers.diagrams.markdown import file_markdown, page_title, to_markdown
from file_parsers.diagrams.model import (
    MAX_CELLS_PER_PAGE,
    MAX_INPUT_BYTES,
    MAX_LINK_BYTES,
    MAX_MARKDOWN_BYTES,
    MAX_MARKDOWN_FILE_BYTES,
    MAX_PAGES,
    MAX_XML_BYTES,
    Arrow,
    Diagram,
    DiagramDecodeError,
    Page,
    Role,
    Shape,
    page_original,
)

__all__ = [
    "MAX_CELLS_PER_PAGE",
    "MAX_INPUT_BYTES",
    "MAX_LINK_BYTES",
    "MAX_MARKDOWN_BYTES",
    "MAX_MARKDOWN_FILE_BYTES",
    "MAX_PAGES",
    "MAX_XML_BYTES",
    "Arrow",
    "Diagram",
    "DiagramDecodeError",
    "Page",
    "Role",
    "Shape",
    "decode",
    "decode_link_payload",
    "file_markdown",
    "page_original",
    "page_title",
    "to_markdown",
]
