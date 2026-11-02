"""PDFParser and the facade over a pre-configured engine: 10-page ranges, progress, the page cap, structure.

Scenario (25-page PDF, engine = a recording ``RangeEngine``):

    caller        PDFParser          RangeEngine        PdfStructure
      | parse(cb)     |                   |                  |
      |-------------->| check_pdf_file    |                  |
      |               | extract (1,10)--->|                  |
      |<-- cb(10,25) -|<-- pages 1-10 ----|                  |
      |               | extract (11,20)-->|                  |
      |<-- cb(20,25) -|<-- pages 11-20 ---|                  |
      |               | extract (21,25)-->|                  |
      |<-- cb(25,25) -|<-- pages 21-25 ---|                  |
      |               | apply(all 25 pages) ---------------->|
      |<-- document --|<-- levels and glue repaired ---------|
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine_stub_support import StubEngine
from file_parsers.convert import PdfParseError, file_to_markdown
from file_parsers.engines.port import PageRange
from file_parsers.limits import DocumentTooLargeError
from file_parsers.pdf.parser import RANGE_PAGES, PDFParser
from file_parsers.pdf.schema import PDFPageContent
from pdf_builder_support import Line, write_pdf

PDF = "application/pdf"


def _chapters(page_range: PageRange | None) -> list[PDFPageContent]:
    """One ``## Chapter N`` page per requested page."""

    assert page_range is not None
    first, last = page_range
    return [PDFPageContent(page_number=n, markdown=f"## Chapter {n}", figures=[]) for n in range(first, last + 1)]


def _range_engine() -> StubEngine:
    return StubEngine(_chapters, engine_id="range@1")


def _pdf(tmp_path: Path, pages: int) -> Path:
    # Chapter 1 is big, the others are small: ranking must see all ranges to give them different levels.
    chapters = [[Line(f"Chapter {n}", 24 if n == 1 else 14)] for n in range(1, pages + 1)]
    return write_pdf(tmp_path / f"{pages}.pdf", chapters)


def test_a_25_page_document_is_extracted_in_ranges_of_ten(tmp_path: Path):
    engine = _range_engine()
    parser = PDFParser(_pdf(tmp_path, 25), tmp_path / "out", engine, images=False)

    document = parser.parse()

    assert RANGE_PAGES == 10
    assert [call[0] for call in engine.calls] == [(1, 10), (11, 20), (21, 25)]
    assert [page.page_number for page in document.pages] == list(range(1, 26))


def test_the_levels_are_ranked_once_over_all_ranges(tmp_path: Path):
    parser = PDFParser(_pdf(tmp_path, 25), tmp_path / "out", _range_engine(), images=False)
    document = parser.parse()
    assert document.pages[0].markdown == "# Chapter 1"  # the biggest size of the whole document
    assert document.pages[11].markdown == "## Chapter 12"  # extracted in the second range, ranked with the first
    assert document.pages[24].markdown == "## Chapter 25"


def test_progress_is_reported_after_each_range_with_the_document_total(tmp_path: Path):
    progress: list[tuple[int, int]] = []
    PDFParser(_pdf(tmp_path, 25), tmp_path / "out", _range_engine(), images=False).parse(
        lambda done, total: progress.append((done, total))
    )
    assert progress == [(10, 25), (20, 25), (25, 25)]


def test_a_short_document_is_one_range_and_images_follow_the_parser_setting(tmp_path: Path):
    engine = _range_engine()
    PDFParser(_pdf(tmp_path, 3), tmp_path / "out", engine, images=True).parse()
    PDFParser(_pdf(tmp_path, 3), tmp_path / "out2", engine, images=False).parse()
    assert engine.calls == [((1, 3), True, tmp_path / "out" / "images"), ((1, 3), False, None)]


def test_a_501_page_pdf_is_refused_before_any_extraction(tmp_path: Path):
    engine = _range_engine()
    parser = PDFParser(_pdf(tmp_path, 501), tmp_path / "out", engine, images=False)
    with pytest.raises(DocumentTooLargeError) as error:
        parser.parse()
    assert (error.value.actual, error.value.limit) == (501, 500)
    assert engine.calls == []


def test_a_500_page_pdf_is_accepted_in_fifty_ranges(tmp_path: Path):
    engine = _range_engine()
    PDFParser(_pdf(tmp_path, 500), tmp_path / "out", engine, images=False).parse()
    assert len(engine.calls) == 50


def test_the_facade_keeps_the_too_large_error_and_wraps_every_other_failure(tmp_path: Path):
    with pytest.raises(DocumentTooLargeError):
        file_to_markdown(_pdf(tmp_path, 501), PDF, engine=_range_engine())
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(PdfParseError, match="PdfiumError"):
        file_to_markdown(bad, PDF, engine=_range_engine())
    empty = write_pdf(tmp_path / "empty.pdf", [])
    with pytest.raises(PdfParseError):
        file_to_markdown(empty, PDF, engine=_range_engine())


def test_the_facade_reports_progress_and_the_engine_id_and_writes_no_images(tmp_path: Path):
    engine = _range_engine()
    progress: list[tuple[int, int]] = []

    result = file_to_markdown(_pdf(tmp_path, 12), PDF, engine=engine, on_progress=lambda d, t: progress.append((d, t)))

    assert result.parser == "range@1"
    assert progress == [(10, 12), (12, 12)]
    assert [call[1:] for call in engine.calls] == [(False, None), (False, None)]
    assert result.markdown.startswith("<!-- source-page: 1 -->\n\n# Chapter 1\n\n<!-- source-page: 2 -->")
    assert result.markdown.count("<!-- source-page:") == 12
