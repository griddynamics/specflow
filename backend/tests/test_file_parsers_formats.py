"""The shared media-type table: every row, alias and refused legacy type."""

from __future__ import annotations

import subprocess
import sys

import pytest

from file_parsers import formats
from file_parsers.formats import (
    FORMATS,
    LEGACY_FORMATS,
    DocumentFormat,
    canonical_media_types,
    format_for_media_type,
    format_for_upload,
    is_legacy,
    normalize_media_type,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ROWS = [
    (DocumentFormat.PDF, ".pdf", "application/pdf"),
    (DocumentFormat.DOCX, ".docx", DOCX),
    (DocumentFormat.PPTX, ".pptx", PPTX),
    (DocumentFormat.XLSX, ".xlsx", XLSX),
    (DocumentFormat.HTML, ".html", "text/html"),
    (DocumentFormat.MARKDOWN, ".md", "text/markdown"),
    (DocumentFormat.CSV, ".csv", "text/csv"),
]


def test_the_table_has_exactly_the_seven_formats():
    assert [(s.format, s.extension, s.media_type) for s in FORMATS] == ROWS
    assert canonical_media_types() == tuple(row[2] for row in ROWS)
    assert len({s.media_type for s in FORMATS}) == 7


@pytest.mark.parametrize(("fmt", "extension", "media_type"), ROWS)
def test_each_row_resolves_by_type_and_by_upload(fmt, extension, media_type):
    assert format_for_media_type(media_type).format is fmt
    assert format_for_upload(media_type, f"file{extension}").format is fmt
    assert format_for_upload(media_type.upper(), f"FILE{extension.upper()}").format is fmt


def test_type_parameters_and_case_are_ignored():
    assert normalize_media_type(" Text/HTML; charset=UTF-8 ") == "text/html"
    assert normalize_media_type(None) == ""
    assert format_for_media_type("text/csv; charset=utf-8").format is DocumentFormat.CSV


@pytest.mark.parametrize(
    ("header", "filename", "expected"),
    [
        ("", "notes.md", DocumentFormat.MARKDOWN),
        ("text/plain", "notes.md", DocumentFormat.MARKDOWN),
        ("text/plain", "data.csv", DocumentFormat.CSV),
        ("application/vnd.ms-excel", "data.csv", DocumentFormat.CSV),
        (None, "notes.md", DocumentFormat.MARKDOWN),
    ],
)
def test_browser_quirks_are_accepted_for_uploads_with_the_right_extension(header, filename, expected):
    assert format_for_upload(header, filename).format is expected


@pytest.mark.parametrize("header", ["", "text/plain", "application/vnd.ms-excel"])
def test_upload_aliases_never_resolve_as_trusted_types(header):
    # Drive classifies by MIME type only: a text/plain file stays unclassified (S07).
    assert format_for_media_type(header) is None


@pytest.mark.parametrize(
    ("header", "filename"),
    [
        ("text/plain", "report.pdf"),  # quirk of another format
        ("application/pdf", "report.docx"),  # header disagrees with the extension
        ("", "report.html"),  # only .md accepts an empty type
        ("text/plain", "page.html"),
        ("application/vnd.ms-excel", "sheet.xlsx"),
        ("image/png", "scan.pdf"),
    ],
)
def test_a_header_that_disagrees_with_the_extension_is_refused(header, filename):
    assert format_for_upload(header, filename) is None


def test_without_a_known_extension_only_a_trusted_type_counts():
    assert format_for_upload("application/pdf", "download").format is DocumentFormat.PDF
    assert format_for_upload("application/pdf", None).format is DocumentFormat.PDF
    assert format_for_upload("text/plain", "download") is None
    assert format_for_upload("", "") is None
    assert format_for_upload("application/octet-stream", "archive.zip") is None


@pytest.mark.parametrize(
    ("header", "filename", "expected"),
    [
        ("text/markdown", "notes.txt", DocumentFormat.MARKDOWN),  # unknown extension: trusted header decides
        ("text/html", "page.htm", DocumentFormat.HTML),
        ("text/csv", "data.dat", DocumentFormat.CSV),
        ("text/plain", "notes.txt", None),  # quirk headers need their own extension
        ("", "notes.txt", None),
        ("application/vnd.ms-excel", "data.txt", None),
        ("text/markdown", "notes.pdf", None),  # known extension: it decides, the header must agree
    ],
)
def test_an_extension_outside_the_table_falls_back_to_a_trusted_header_only(header, filename, expected):
    spec = format_for_upload(header, filename)
    assert (spec.format if spec else None) is expected


def test_paths_in_filenames_do_not_confuse_the_extension():
    assert format_for_upload("text/markdown", r"C:\docs.v2\readme.md").format is DocumentFormat.MARKDOWN
    assert format_for_upload("text/plain", "dir.md/readme") is None
    assert format_for_upload("text/plain", ".md") is None  # a dotfile has no extension


@pytest.mark.parametrize(
    ("extension", "media_type"),
    [(".doc", "application/msword"), (".ppt", "application/vnd.ms-powerpoint"), (".xls", "application/vnd.ms-excel")],
)
def test_legacy_office_formats_are_listed_and_refused(extension, media_type):
    assert (extension, media_type) in [(x.extension, x.media_type) for x in LEGACY_FORMATS]
    assert is_legacy(filename=f"old{extension}")
    assert is_legacy(filename=f"OLD{extension.upper()}", media_type="application/octet-stream")
    assert format_for_upload(media_type, f"old{extension}") is None
    assert format_for_media_type(media_type) is None


def test_legacy_by_type_only_and_non_legacy_files():
    assert is_legacy(media_type="application/msword")
    assert is_legacy(media_type="application/vnd.ms-powerpoint")
    assert not is_legacy(filename="data.csv", media_type="application/vnd.ms-excel")
    assert not is_legacy(filename="report.docx", media_type=DOCX)
    assert not is_legacy()


def test_a_legacy_extension_is_refused_even_with_a_trusted_header():
    assert format_for_upload("application/pdf", "report.doc") is None


def test_the_module_imports_nothing_heavy():
    # The api image imports formats.py: a fresh interpreter must not load a parser stack.
    code = (
        "import sys, file_parsers.formats;"
        "heavy = {'docling', 'torch', 'pymupdf', 'pymupdf4llm', 'pypdfium2', 'anthropic', 'pydantic'};"
        "loaded = sorted(heavy & {m.split('.')[0] for m in sys.modules});"
        "assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
    assert formats.__doc__
