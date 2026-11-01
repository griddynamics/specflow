"""The DocumentEngine port and the file_to_markdown facade (S02), over a pre-configured engine.

The Docling engine is tested in test_docling_engine.py, the quality of its output (G1, G8) in test_pdf_quality.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from file_parsers.convert import ParsedMarkdown, PdfParseError, default_engine, file_to_markdown
from file_parsers.engines.docling import DoclingEngine
from file_parsers.engines.port import DocumentEngine, PageRange, ProgressCallback, UnsupportedFormatError
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
    assert isinstance(DoclingEngine(threads=1), DocumentEngine)
    assert isinstance(FakeEngine(), DocumentEngine)
    assert not isinstance(object(), DocumentEngine)


def test_the_default_engine_is_docling_and_its_id_is_the_stored_parser_value():
    assert isinstance(default_engine(), DoclingEngine)
    assert default_engine().engine_id == "docling@2.145.0+sf1"


def test_the_alias_is_the_facade_for_pdf(monkeypatch):
    seen: list[tuple[Path, str]] = []

    def fake(path: Path, media_type: str) -> ParsedMarkdown:
        seen.append((path, media_type))
        return ParsedMarkdown(markdown="x", parser="pymupdf4llm")

    monkeypatch.setattr("file_parsers.pdf.to_markdown.file_to_markdown", fake)
    assert pdf_file_to_markdown(FIXTURE).markdown == "x"
    assert seen == [(FIXTURE, "application/pdf")]


def test_the_facade_routes_pdf_to_the_given_engine_and_reports_its_id():
    engine = FakeEngine()
    result = file_to_markdown(FIXTURE, "application/pdf", engine=engine)
    assert result.parser == "fake@1"
    assert "# Fake" in result.markdown
    [(path, media_type, page_range, images, assets_dir)] = engine.calls
    # the fixture has 8 pages: one range; the stored Markdown keeps placeholders only, no images are written
    assert (path, media_type, page_range, images, assets_dir) == (FIXTURE, "application/pdf", (1, 8), False, None)


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


def test_pdf_parser_defaults_to_the_docling_engine_and_accepts_another(tmp_path):
    assert isinstance(PDFParser(FIXTURE, tmp_path).engine, DoclingEngine)
    engine = FakeEngine()
    parser = PDFParser(FIXTURE, tmp_path, engine)
    document = parser.parse()
    assert parser.engine is engine
    assert [page.markdown for page in document.pages] == ["# Fake"]
    assert engine.calls[0][4] == tmp_path / "images"  # the CLI path writes figure images
