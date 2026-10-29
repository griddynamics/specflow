"""PDF to Markdown (AC-5.1, AC-5.2): deterministic, no LLM, no leftovers."""

from __future__ import annotations

from pathlib import Path

import pytest

from file_parsers.pdf import parser as parser_module
from file_parsers.pdf.to_markdown import ParsedMarkdown, PdfParseError, pdf_file_to_markdown

FIXTURE = Path(__file__).parent / "data" / "specs" / "extraction.pdf"


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point the temp dir at tmp_path so leftovers are observable."""

    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    return tmp_path


def test_parses_the_fixture_without_llm_or_leftovers(temp_root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("LLM path used")

    monkeypatch.setattr(parser_module, "Client", forbidden)
    monkeypatch.setattr(parser_module.PDFParser, "enrich", forbidden)
    result = pdf_file_to_markdown(FIXTURE)
    assert isinstance(result, ParsedMarkdown)
    assert result.parser == "pymupdf4llm"
    assert result.markdown.strip()
    assert list(temp_root.iterdir()) == []


def test_uses_parse_then_render_only(monkeypatch, temp_root):
    calls = []
    real_parse, real_render = parser_module.PDFParser.parse, parser_module.PDFParser.render

    def parse(self):
        calls.append("parse")
        return real_parse(self)

    def render(self):
        calls.append("render")
        return real_render(self)

    monkeypatch.setattr(parser_module.PDFParser, "parse", parse)
    monkeypatch.setattr(parser_module.PDFParser, "render", render)
    pdf_file_to_markdown(FIXTURE)
    assert calls == ["parse", "render"]


def test_corrupt_pdf_raises_and_cleans_up(tmp_path, temp_root):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    with pytest.raises(PdfParseError):
        pdf_file_to_markdown(bad)
    assert [p.name for p in temp_root.iterdir()] == ["bad.pdf"]


def test_truncated_pdf_raises(tmp_path):
    bad = tmp_path / "cut.pdf"
    bad.write_bytes(FIXTURE.read_bytes()[:200])
    with pytest.raises(PdfParseError):
        pdf_file_to_markdown(bad)


def test_missing_file_raises_parse_error(tmp_path):
    with pytest.raises(PdfParseError):
        pdf_file_to_markdown(tmp_path / "nope.pdf")


def test_failing_parse_still_finishes_stages(tmp_path, monkeypatch):
    finished = []
    real = parser_module._StageTracker.finish
    monkeypatch.setattr(parser_module._StageTracker, "finish", lambda self: (finished.append(1), real(self))[1])
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    with pytest.raises(PdfParseError):
        pdf_file_to_markdown(bad)
    assert finished == [1]
