"""The typed pypdfium2 wrapper: text with effective font sizes, PNG rendering, PNG size, page count."""

from __future__ import annotations

from pathlib import Path

import pypdfium2
import pytest

from file_parsers.pdf.pdfium_typed import open_document, page_count, png_size
from pdf_builder_support import Line, write_pdf

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    return write_pdf(
        tmp_path / "sample.pdf",
        [
            [Line("Big Title", 24), Line("Matrix Title", 18, via_matrix=True), Line("body text of the page", 12)],
            [],
        ],
    )


def test_page_count_reads_the_page_tree(sample: Path):
    assert page_count(sample) == 2


def test_the_text_and_the_effective_font_size_of_each_character(sample: Path):
    with open_document(sample) as pdf:
        page = pdf.page_text(0)
    assert page.text.startswith("Big Title")
    assert len(page.sizes) == len(page.text)
    assert page.sizes[page.text.index("Big")] == pytest.approx(24)
    # nominal size 1 x text-matrix scale 18: the nominal size alone would say 1.0
    assert page.sizes[page.text.index("Matrix")] == pytest.approx(18)
    assert page.sizes[page.text.index("body")] == pytest.approx(12)


def test_text_without_sizes_and_a_blank_page(sample: Path):
    with open_document(sample) as pdf:
        assert pdf.page_text(0, with_sizes=False).sizes == []
        blank = pdf.page_text(1)
        assert (pdf.char_count(0) > 0, pdf.char_count(1)) == (True, 0)
    assert (blank.text, blank.sizes) == ("", [])


def test_render_png_of_a_page_and_of_a_clip(sample: Path):
    with open_document(sample) as pdf:
        page = pdf.render_png(0, 72)
        clip = pdf.render_png(0, 144, clip=(0, 0, 100, 50))
        clip_beyond_page = pdf.render_png(0, 72, clip=(500, 700, 900, 900))
    assert page.startswith(PNG_SIGNATURE)
    assert _size(page) == (612, 792)
    assert _size(clip) == (200, 100)  # 100 x 50 points at 2 px per point
    assert _size(clip_beyond_page) == (112, 92)  # cut at the page edge


def test_png_size_reads_the_header(tmp_path: Path, sample: Path):
    with open_document(sample) as pdf:
        target = tmp_path / "page.png"
        target.write_bytes(pdf.render_png(0, 36))
    assert png_size(target) == (306, 396)


@pytest.mark.parametrize("content", [b"", b"not a png at all, but long enough to be read as a header"])
def test_png_size_refuses_a_file_that_is_not_a_png(tmp_path: Path, content: bytes):
    target = tmp_path / "x.png"
    target.write_bytes(content)
    with pytest.raises(ValueError, match="not a PNG"):
        png_size(target)


def test_a_file_that_is_not_a_pdf_raises(tmp_path: Path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(pypdfium2.PdfiumError):
        page_count(bad)


def _size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
