"""Whether a file's bytes are the format it is stored as (DESIGN N2: the limits of the stored format hold).

The guard checks the limits of the STORED format. A converter that sniffed the bytes and picked another backend
would run it without that format's limits (a PDF stored as Markdown has no page cap; XLSX bytes stored as DOCX have
no cell count). So the guard refuses a file whose bytes name another format, and the engine
(``file_parsers.engines.docling``) only allows the stored format for each conversion. The two rules here are the
guard's half:

- HTML, Markdown, CSV: the first bytes, after an optional UTF-8 BOM, may not be a PDF (``%PDF``) or a ZIP
  (``PK``) signature, the binary formats a converter detects from magic bytes. Nothing else is looked at: a
  Markdown file full of HTML, an HTML file without a doctype and a CSV that looks like a Markdown table are all
  what they claim to be, and the converter allowed for them is the stored one, whatever its own sniffing guesses.
- DOCX, PPTX, XLSX: every main-document content type ``[Content_Types].xml`` declares (``Override`` or
  ``Default``) must be the stored format's (``wordprocessingml``/``ms-word``, ``presentationml``/``ms-powerpoint``,
  ``spreadsheetml``/``ms-excel``, ending in ``.main+xml``, or ``.main`` for the binary workbook
  ``application/vnd.ms-excel.sheet.binary.macroEnabled.main``). The binary workbook is in the XLSX family, so it
  passes as a stored XLSX here and openpyxl refuses it later (a parse failure); stored as DOCX or PPTX it is
  refused here. The readers find their main part through exactly these
  types (python-docx and python-pptx check the main part's type, openpyxl searches the types), so XLSX bytes cannot
  load as anything but a workbook, nor DOCX or PPTX bytes as anything else. Other types (charts, embedded packages
  kept as binary parts) do not count.
"""

from __future__ import annotations

from file_parsers.formats import DocumentFormat
from file_parsers.limits import DocumentParseFailedError

HEAD_BYTES = 8
"""How much of a text file ``check_text_head`` needs: a UTF-8 BOM (3 bytes) and the longest signature (4)."""

_FOREIGN_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", "PDF"),
    (b"PK\x03\x04", "ZIP"),
    (b"PK\x05\x06", "ZIP"),  # an empty archive
    (b"PK\x07\x08", "ZIP"),  # a spanned archive
)

_MAIN_FAMILIES: dict[DocumentFormat, tuple[str, ...]] = {
    DocumentFormat.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.",
        "application/vnd.ms-word.",
    ),
    DocumentFormat.PPTX: (
        "application/vnd.openxmlformats-officedocument.presentationml.",
        "application/vnd.ms-powerpoint.",
    ),
    DocumentFormat.XLSX: ("application/vnd.openxmlformats-officedocument.spreadsheetml.", "application/vnd.ms-excel."),
}
_MAIN_SUFFIXES = (".main+xml", ".main")  # ".main": the binary workbook (xlsb), XLSX family (see the module doc)
_UTF8_BOM = b"\xef\xbb\xbf"


def check_text_head(head: bytes, document_format: DocumentFormat) -> None:
    """Refuse a text file (HTML, Markdown, CSV) whose first bytes, after an optional UTF-8 BOM, are a PDF or ZIP
    signature."""

    head = head.removeprefix(_UTF8_BOM)
    for magic, name in _FOREIGN_MAGIC:
        if head.startswith(magic):
            raise DocumentParseFailedError(f"the {document_format.value} file is a {name}")


def main_format(content_type: str) -> DocumentFormat | None:
    """The OOXML format whose main document part has ``content_type``, or ``None`` for any other type."""

    kind = content_type.split(";", maxsplit=1)[0].strip().lower()
    if not kind.endswith(_MAIN_SUFFIXES):
        return None
    for document_format, prefixes in _MAIN_FAMILIES.items():
        if kind.startswith(prefixes):
            return document_format
    return None


def check_main_parts(content_types: list[dict[str, str]], document_format: DocumentFormat) -> None:
    """Refuse an OOXML package that declares the main document of another format.

    ``content_types`` are the attributes of the ``Default`` and ``Override`` elements of ``[Content_Types].xml``.
    """

    for entry in content_types:
        found = main_format(entry.get("ContentType", ""))
        if found is not None and found is not document_format:
            raise DocumentParseFailedError(f"the {document_format.value} file declares a {found.value} main document")
