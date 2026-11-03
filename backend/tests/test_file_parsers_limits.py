"""The page cap and the two error types of the parse activity (S04 maps them by name)."""

from __future__ import annotations

from pathlib import Path

import pytest

from file_parsers import limits
from file_parsers.limits import (
    MAX_PDF_PAGES,
    DocumentParseFailedError,
    DocumentTooLargeError,
    check_pdf_file,
    check_pdf_pages,
)
from pdf_builder_support import write_pdf


def test_the_cap_is_500_pages():
    assert MAX_PDF_PAGES == 500


def test_a_document_at_the_cap_passes_and_one_page_more_is_refused():
    check_pdf_pages(500)
    with pytest.raises(DocumentTooLargeError) as error:
        check_pdf_pages(501)
    assert (error.value.what, error.value.actual, error.value.limit) == ("pages", 501, 500)
    assert str(error.value) == "pages: 501 is over the limit of 500"


def test_check_pdf_file_counts_the_pages_and_returns_them(tmp_path: Path):
    assert check_pdf_file(write_pdf(tmp_path / "three.pdf", [[], [], []])) == 3


def test_check_pdf_file_refuses_a_501_page_pdf_from_its_page_tree(tmp_path: Path):
    big = write_pdf(tmp_path / "big.pdf", [[] for _ in range(501)])
    with pytest.raises(DocumentTooLargeError) as error:
        check_pdf_file(big)
    assert error.value.actual == 501


def test_the_error_types_are_distinct_exceptions_with_the_names_the_activity_maps():
    assert issubclass(DocumentTooLargeError, Exception)
    assert issubclass(DocumentParseFailedError, Exception)
    assert not issubclass(DocumentTooLargeError, DocumentParseFailedError)
    assert {DocumentTooLargeError.__name__, DocumentParseFailedError.__name__} == {
        "DocumentTooLargeError",
        "DocumentParseFailedError",
    }
    assert limits.DocumentTooLargeError is DocumentTooLargeError
