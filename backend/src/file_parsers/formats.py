"""The one table of document formats SpecFlow accepts.

Shared by the upload API, the Drive import and the parser. It imports only the standard library
(no docling, torch or pypdfium2) because the api image imports it.

Two lookups with different strictness:

- :func:`format_for_media_type` is for a type that is already trusted (a stored
  ``documents.media_type`` or a Drive MIME type): the canonical type and its general aliases.
- :func:`format_for_upload` is for a browser upload: the file extension decides, and the header may
  also be one of the browser quirks (``upload_aliases``). Drive never gets those: it classifies by
  MIME type only.

Legacy ``.doc``, ``.ppt`` and ``.xls`` are listed in :data:`LEGACY_FORMATS` and are never accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DocumentFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    MARKDOWN = "markdown"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """One accepted format.

    ``extension`` has a leading dot and is lower case. ``media_type`` is canonical (stored in
    ``documents.media_type``). ``aliases`` are other types that name the same format in any
    context. ``upload_aliases`` are browser quirks that are valid for uploads only, and only
    together with this format's extension.
    """

    format: DocumentFormat
    extension: str
    media_type: str
    aliases: frozenset[str] = frozenset()
    upload_aliases: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class LegacyFormat:
    """A format that is recognised only to be refused."""

    extension: str
    media_type: str


PDF_MEDIA_TYPE = "application/pdf"

FORMATS: tuple[FormatSpec, ...] = (
    FormatSpec(DocumentFormat.PDF, ".pdf", PDF_MEDIA_TYPE),
    FormatSpec(
        DocumentFormat.DOCX,
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    FormatSpec(
        DocumentFormat.PPTX,
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    FormatSpec(
        DocumentFormat.XLSX,
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    FormatSpec(DocumentFormat.HTML, ".html", "text/html"),
    # Browsers send an empty type for .md, and text/plain for some .md uploads.
    FormatSpec(DocumentFormat.MARKDOWN, ".md", "text/markdown", upload_aliases=frozenset({"", "text/plain"})),
    # Windows browsers send application/vnd.ms-excel for .csv; some send text/plain.
    FormatSpec(
        DocumentFormat.CSV,
        ".csv",
        "text/csv",
        upload_aliases=frozenset({"text/plain", "application/vnd.ms-excel"}),
    ),
)

LEGACY_FORMATS: tuple[LegacyFormat, ...] = (
    LegacyFormat(".doc", "application/msword"),
    LegacyFormat(".ppt", "application/vnd.ms-powerpoint"),
    LegacyFormat(".xls", "application/vnd.ms-excel"),
)

_BY_MEDIA_TYPE = {t: spec for spec in FORMATS for t in (spec.media_type, *spec.aliases)}
_BY_EXTENSION = {spec.extension: spec for spec in FORMATS}


def normalize_media_type(value: str | None) -> str:
    """Lower-case the type and drop parameters: ``"Text/HTML; charset=utf-8"`` -> ``"text/html"``."""

    return (value or "").split(";", 1)[0].strip().lower()


def _extension(filename: str | None) -> str:
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def canonical_media_types() -> tuple[str, ...]:
    """The accepted canonical media types, in table order (for an error message)."""

    return tuple(spec.media_type for spec in FORMATS)


def format_for_media_type(media_type: str | None) -> FormatSpec | None:
    """The format of a trusted media type (canonical or general alias), else ``None``."""

    return _BY_MEDIA_TYPE.get(normalize_media_type(media_type))


def format_for_upload(content_type: str | None, filename: str | None) -> FormatSpec | None:
    """The format of an uploaded file, else ``None`` (refuse with 415).

    With a known extension the extension decides and the header must agree with that format
    (canonical type, alias, upload alias). With an extension that is not in the table (``.txt``,
    ``.htm``, none) the extension says nothing and only a trusted header type (canonical or general
    alias) counts, so ``notes.txt`` as ``text/markdown`` is accepted as Markdown, while the quirk
    headers (``""``, ``text/plain``, ``application/vnd.ms-excel``) are refused there. Legacy
    extensions are never accepted.
    """

    header = normalize_media_type(content_type)
    spec = _BY_EXTENSION.get(_extension(filename))
    if spec is None:
        return None if is_legacy(filename=filename) else format_for_media_type(header)
    if header in (spec.media_type, *spec.aliases, *spec.upload_aliases):
        return spec
    return None


def is_legacy(media_type: str | None = None, filename: str | None = None) -> bool:
    """True when the extension or the type is a refused legacy Office format."""

    extension = _extension(filename)
    header = normalize_media_type(media_type)
    return any(
        extension == legacy.extension or (not extension and header == legacy.media_type) for legacy in LEGACY_FORMATS
    )
