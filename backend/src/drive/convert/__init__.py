"""Pure converters from Google file JSON to Markdown (B3). No I/O, no Google imports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from drive.convert._md import ConversionError
from drive.convert.docs import document_to_markdown
from drive.convert.sheets import spreadsheet_to_markdown
from drive.convert.slides import presentation_to_markdown

# Private on purpose: the parity test compares them with drive.categories (DATA-GDUI-0001).
_MIME_DOCUMENT = "application/vnd.google-apps.document"
_MIME_PRESENTATION = "application/vnd.google-apps.presentation"
_MIME_SPREADSHEET = "application/vnd.google-apps.spreadsheet"

__all__ = [
    "ConversionError",
    "document_to_markdown",
    "presentation_to_markdown",
    "spreadsheet_to_markdown",
    "to_markdown",
]


def to_markdown(mime_type: str, payload: Mapping[str, Any]) -> str:
    """Dispatch on the Google ``mimeType``; anything else is a ConversionError."""
    if mime_type == _MIME_DOCUMENT:
        return document_to_markdown(payload)
    if mime_type == _MIME_PRESENTATION:
        return presentation_to_markdown(payload)
    if mime_type == _MIME_SPREADSHEET:
        return spreadsheet_to_markdown(payload)
    raise ConversionError("unsupported mime type")
