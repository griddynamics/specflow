"""The shared lane table (``api.imports.files``) and pacing (``integrations.pacing``) of the binary importers (DB18):
Box and Dropbox use the same caps and backoff, and the caps are the upload ones."""

from __future__ import annotations

import httpx
import pytest

from api.imports import files
from api.uploads import MAX_BYTES
from file_parsers.formats import DocumentFormat
from integrations.pacing import BACKOFF_SECONDS, WaitBudget, backoff, retry_after


@pytest.mark.parametrize(
    ("extension", "media_type", "cap"),
    [
        ("pdf", "application/pdf", files.MAX_PDF_BYTES),
        ("docx", None, files.MAX_OOXML_BYTES),
        ("pptx", None, files.MAX_OOXML_BYTES),
        ("xlsx", None, files.MAX_OOXML_BYTES),
        ("html", None, files.MAX_TEXT_BYTES),
        ("md", None, files.MAX_TEXT_BYTES),
        ("csv", None, files.MAX_TEXT_BYTES),
        ("txt", "text/markdown", files.MAX_TEXT_BYTES),
    ],
)
def test_each_parser_format_and_txt_have_a_lane_with_the_upload_cap(extension, media_type, cap) -> None:
    lane = files.lane_of(extension)

    assert lane is not None and lane.cap == cap
    assert media_type is None or lane.media_type == media_type
    assert files.classify(extension, cap) == lane and files.classify(extension, cap + 1) is None


@pytest.mark.parametrize("extension", ["png", "doc", "ppt", "xls", "gdoc", "paper", "zip", ""])
def test_other_types_have_no_lane(extension: str) -> None:
    assert files.lane_of(extension) is None and files.classify(extension, 1) is None


def test_the_caps_are_the_upload_caps_and_the_extension_is_lower_case() -> None:
    assert MAX_BYTES[DocumentFormat.PDF] == files.MAX_PDF_BYTES
    assert MAX_BYTES[DocumentFormat.XLSX] == files.MAX_OOXML_BYTES
    assert MAX_BYTES[DocumentFormat.CSV] == files.MAX_TEXT_BYTES
    assert [files.extension_of(n) for n in ("A.PDF", "x.tar.gz", "README", ".env", "a.")] == ["pdf", "gz", "", "", ""]


def test_the_wait_budget_spends_only_what_fits_and_the_backoff_reads_retry_after() -> None:
    budget = WaitBudget(5)
    with_header = httpx.Response(429, headers={"Retry-After": "2"})
    without = httpx.Response(429)
    odd = httpx.Response(429, headers={"Retry-After": "soon"})

    assert budget.take(3) == 3 and budget.take(3) is None and budget.left == 2
    assert retry_after(with_header) == 2.0 and retry_after(without) is None and retry_after(odd) is None
    assert [backoff(without, attempt) for attempt in range(4)] == [*BACKOFF_SECONDS, BACKOFF_SECONDS[-1]]
    assert backoff(with_header, 0) == 2.0
