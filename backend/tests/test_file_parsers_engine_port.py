"""The DocumentEngine port, the pymupdf4llm engine and the file_to_markdown facade (S02)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from file_parsers.convert import ParsedMarkdown, PdfParseError, default_engine, file_to_markdown
from file_parsers.engines.port import DocumentEngine, PageRange, ProgressCallback, UnsupportedFormatError
from file_parsers.engines.pymupdf4llm_engine import PyMuPDF4LLMEngine
from file_parsers.formats import FORMATS, PDF_MEDIA_TYPE
from file_parsers.pdf.parser import PDFParser
from file_parsers.pdf.schema import PDFDocument, PDFPageContent
from file_parsers.pdf.to_markdown import pdf_file_to_markdown

FIXTURE = Path(__file__).parent / "data" / "specs" / "extraction.pdf"
NON_PDF_TYPES = [spec.media_type for spec in FORMATS if spec.media_type != PDF_MEDIA_TYPE]


class FakeEngine:
    """A pre-configured engine: records its calls, returns a fixed document."""

    engine_id = "fake@1"
    formats = frozenset({PDF_MEDIA_TYPE})

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, PageRange | None, bool, Path | None]] = []

    def extract(
        self,
        path: Path,
        media_type: str,
        page_range: PageRange | None = None,
        *,
        images: bool = False,
        assets_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PDFDocument:
        del on_progress
        self.calls.append((path, media_type, page_range, images, assets_dir))
        return PDFDocument(source_path=path, pages=[PDFPageContent(page_number=1, markdown="# Fake", figures=[])])


def test_the_engines_satisfy_the_port():
    assert isinstance(PyMuPDF4LLMEngine(), DocumentEngine)
    assert isinstance(FakeEngine(), DocumentEngine)
    assert not isinstance(object(), DocumentEngine)


def test_engine_identity_and_formats_are_stable_contract_values():
    engine = PyMuPDF4LLMEngine()
    assert engine.engine_id == "pymupdf4llm"
    assert engine.formats == frozenset({"application/pdf"})
    assert default_engine().engine_id == "pymupdf4llm"


# Pre-S02 output of pymupdf4llm 2.0 on the fixture without an OCR engine, as in CI and the images
# (S03 replaces the engine and this test with G1).
PRE_S02_SHA256 = "34db74eb200135bb6a9a52f12487e56358884987c5ce4d305b730846265f1078"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_pdf_output_is_byte_identical_to_the_pre_s02_output(tmp_path):
    parser = PDFParser(FIXTURE, tmp_path)  # the production path: parse, then render
    parser.parse()
    produced = parser.render()

    result = file_to_markdown(FIXTURE, "application/pdf")

    assert _sha256(produced) == PRE_S02_SHA256
    assert _sha256(result.markdown) == PRE_S02_SHA256
    assert _sha256(pdf_file_to_markdown(FIXTURE).markdown) == PRE_S02_SHA256
    assert result.parser == "pymupdf4llm"


def test_the_alias_is_the_facade_for_pdf(monkeypatch):
    seen: list[tuple[Path, str]] = []

    def fake(path: Path, media_type: str) -> ParsedMarkdown:
        seen.append((path, media_type))
        return ParsedMarkdown(markdown="x")

    monkeypatch.setattr("file_parsers.pdf.to_markdown.file_to_markdown", fake)
    assert pdf_file_to_markdown(FIXTURE).markdown == "x"
    assert seen == [(FIXTURE, "application/pdf")]


def test_the_facade_routes_pdf_to_the_given_engine_and_reports_its_id(tmp_path):
    engine = FakeEngine()
    result = file_to_markdown(FIXTURE, "application/pdf", engine=engine)
    assert result.parser == "fake@1"
    assert "# Fake" in result.markdown
    [(path, media_type, page_range, images, assets_dir)] = engine.calls
    assert (path, media_type, page_range, images) == (FIXTURE, "application/pdf", None, True)
    assert assets_dir is not None
    assert not assets_dir.exists()  # the temporary directory is gone


@pytest.mark.parametrize("media_type", NON_PDF_TYPES)
def test_every_non_pdf_format_raises_the_typed_error_until_s05(media_type):
    engine = FakeEngine()
    with pytest.raises(UnsupportedFormatError) as error:
        file_to_markdown(FIXTURE, media_type, engine=engine)
    assert error.value.media_type == media_type
    assert engine.calls == []


@pytest.mark.parametrize("media_type", ["", "application/msword", "image/png", "application/vnd.ms-excel"])
def test_unknown_and_legacy_types_raise_the_typed_error(media_type):
    with pytest.raises(UnsupportedFormatError):
        file_to_markdown(FIXTURE, media_type)


def test_the_facade_accepts_a_type_with_parameters():
    result = file_to_markdown(FIXTURE, "Application/PDF; q=1", engine=FakeEngine())
    assert result.parser == "fake@1"


def test_a_broken_pdf_raises_pdf_parse_error_without_the_cause_text(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(PdfParseError, match="cannot parse PDF: ") as error:
        file_to_markdown(bad, "application/pdf")
    assert "not a pdf" not in str(error.value)


def test_pdf_parser_defaults_to_the_current_engine_and_accepts_another(tmp_path):
    assert isinstance(PDFParser(FIXTURE, tmp_path).engine, PyMuPDF4LLMEngine)
    engine = FakeEngine()
    parser = PDFParser(FIXTURE, tmp_path, engine)
    document = parser.parse()
    assert parser.engine is engine
    assert [page.markdown for page in document.pages] == ["# Fake"]
    assert engine.calls[0][4] == tmp_path / "images"


def test_engine_page_range_is_one_based_and_inclusive():
    progress: list[tuple[int, int]] = []
    document = PyMuPDF4LLMEngine().extract(
        FIXTURE, PDF_MEDIA_TYPE, page_range=(2, 3), on_progress=lambda done, total: progress.append((done, total))
    )
    assert [page.page_number for page in document.pages] == [2, 3]
    assert progress == [(2, 2)]


def test_engine_reports_progress_once_at_the_end_for_a_whole_document():
    progress: list[tuple[int, int]] = []
    document = PyMuPDF4LLMEngine().extract(
        FIXTURE, PDF_MEDIA_TYPE, on_progress=lambda done, total: progress.append((done, total))
    )
    assert progress == [(len(document.pages), len(document.pages))]


def test_engine_without_images_writes_no_figures(tmp_path):
    assets = tmp_path / "assets"
    document = PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, images=False, assets_dir=assets)
    assert all(not page.figures for page in document.pages)
    assert "![" not in "".join(page.markdown for page in document.pages)
    assert list(assets.glob("*.png")) == []


def test_engine_with_images_writes_figures_into_assets_dir(tmp_path):
    document = PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, images=True, assets_dir=tmp_path / "images")
    figures = [figure for page in document.pages for figure in page.figures]
    assert all(figure.image_path.parent == tmp_path / "images" for figure in figures)


def test_engine_without_images_leaves_no_temporary_files(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("page_range", [(0, 1), (3, 2), (-1, 2)])
def test_engine_rejects_an_invalid_range(page_range):
    with pytest.raises(ValueError, match="invalid page range"):
        PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, page_range=page_range)


@pytest.mark.parametrize("page_range", [(7, 99), (9, 9), (1, 9)])
def test_engine_rejects_a_range_beyond_the_document_with_its_own_message(page_range):
    with pytest.raises(ValueError, match=r"is beyond the 8 pages of the document"):
        PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, page_range=page_range)


def test_engine_accepts_the_last_page_exactly():
    document = PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, page_range=(8, 8))
    assert [page.page_number for page in document.pages] == [8]


def test_engine_rejects_images_without_a_directory():
    with pytest.raises(ValueError, match="assets_dir"):
        PyMuPDF4LLMEngine().extract(FIXTURE, PDF_MEDIA_TYPE, images=True)


def test_engine_rejects_a_type_it_does_not_accept():
    with pytest.raises(UnsupportedFormatError):
        PyMuPDF4LLMEngine().extract(FIXTURE, "text/csv")
