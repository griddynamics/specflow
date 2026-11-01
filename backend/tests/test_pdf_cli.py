"""The CLI path (``python -m file_parsers.pdf.parser [--enrich]``): figure PNGs, manifest, enrichment, no PyMuPDF."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import file_parsers.pdf.parser as parser_module
from engine_stub_support import StubEngine
from file_parsers.pdf.parser import PDFParser
from file_parsers.pdf.pdfium_typed import png_size
from file_parsers.pdf.schema import PDFPageContent, PDFTableBox
from llm.content import Attachment
from pdf_builder_support import Line, write_pdf

FIXTURE = Path(__file__).parent / "data" / "specs" / "extraction.pdf"
FORMULA_TABLE = "| name | formula |\n| - | - |\n| area | A = 2<br>r |"


def test_the_module_runs_as_a_script_on_the_fixture_and_writes_markdown_figures_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(sys, "argv", ["parser", str(FIXTURE), "--output-root", str(tmp_path)])
    monkeypatch.delitem(sys.modules, "file_parsers.pdf.parser")  # runpy warns when the module is already imported

    runpy.run_module("file_parsers.pdf.parser", run_name="__main__")

    [out] = tmp_path.iterdir()
    markdown = (out / "document.md").read_text(encoding="utf-8")
    manifest = json.loads((out / "figures.json").read_text(encoding="utf-8"))
    assert markdown.count("<!-- source-page:") == 8
    assert [figure["id"] for figure in manifest] == ["p2.fig0", "p5.fig0", "p6.fig0"]
    for figure in manifest:
        assert f"![{figure['id']}]({figure['asset_ref']})" in markdown
        image = out / figure["asset_ref"]
        assert png_size(image) == (figure["width"], figure["height"])
    printed = capsys.readouterr().out
    assert "figures:  3 (0 described)" in printed


def _formula_page(_range: object) -> list[PDFPageContent]:
    box = PDFTableBox(x0=72, y0=72, x1=272, y1=172)
    return [PDFPageContent(page_number=1, markdown=FORMULA_TABLE, figures=[], tables=[box])]


class _ScriptedClient:
    def __init__(self) -> None:
        self.attachments: list[Attachment] = []

    def message(
        self,
        prompt: str,
        model: str,
        system: str | None = None,
        max_tokens: int = 1024,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        del prompt, model, system, max_tokens
        self.attachments.extend(attachments)
        answer: dict[str, Any] = {
            "markdown": "| name | formula |\n| - | - |\n| area | $A = 2r$ |",
            "confidence": "high",
        }
        return json.dumps(answer)


def test_enrich_runs_the_math_enricher_over_pdfium_renders_and_the_renderer_shows_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _ScriptedClient()
    monkeypatch.setattr(parser_module, "Client", lambda: client)
    pdf = write_pdf(tmp_path / "doc.pdf", [[Line("A page with a formula table", 14)]])
    parser = PDFParser(pdf, tmp_path / "out", StubEngine(_formula_page))

    parser.parse()
    parser.enrich()
    markdown = parser.render()

    assert parser.enrichment_error is None
    assert [a.name for a in client.attachments] == ["page-1-table-0.png"]
    assert "<!-- llm-table " in markdown
    assert "$A = 2r$" in markdown
    assert FORMULA_TABLE not in markdown
