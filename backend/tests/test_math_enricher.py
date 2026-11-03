"""PdfMathEnricher over pypdfium2 renders and the Docling table boxes (no PyMuPDF, no network).

The LLM client is the only external party: ``ScriptedClient`` returns prepared JSON answers and keeps
the attachments it was sent, so the tests check the PNGs that pdfium rendered for the model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from file_parsers.pdf.math_enricher import PAGE_DPI, PdfMathEnricher
from file_parsers.pdf.schema import PDFDocument, PDFFigure, PDFPageContent, PDFTableBox
from llm.client import Client
from llm.content import Attachment
from pdf_builder_support import Line, write_pdf

FORMULA_TABLE = "| name | formula |\n| - | - |\n| area | A = 2<br>r |"
SCALE = PAGE_DPI / 72


class ScriptedClient:
    """Answers the n-th request with the n-th prepared JSON object and records the attachments."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
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
        return json.dumps(self.answers.pop(0))


def _png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _enricher(client: ScriptedClient) -> PdfMathEnricher:
    return PdfMathEnricher(cast(Client, client), model="test-model", attempts=1)


def _document(tmp_path: Path, page: PDFPageContent) -> PDFDocument:
    pdf = write_pdf(tmp_path / "doc.pdf", [[Line("A page with a formula table", 14)]])
    return PDFDocument(source_path=pdf, pages=[page])


def test_a_formula_table_is_sent_as_a_clip_of_its_docling_box_and_replaced(tmp_path: Path):
    client = ScriptedClient({"markdown": "| name | formula |\n| - | - |\n| area | $A = 2r$ |", "confidence": "high"})
    page = PDFPageContent(
        page_number=1, markdown=FORMULA_TABLE, figures=[], tables=[PDFTableBox(x0=72, y0=72, x1=272, y1=172)]
    )

    result = _enricher(client).enrich(_document(tmp_path, page))

    [replacement] = result.pages[0].table_replacements
    assert (replacement.id, replacement.source_markdown, replacement.confidence) == ("p1.table0", FORMULA_TABLE, "high")
    assert "$A = 2r$" in replacement.markdown
    [attachment] = client.attachments
    assert attachment.media_type == "image/png"
    assert attachment.name == "page-1-table-0.png"
    assert _png_size(attachment.data) == (
        round(272 * SCALE) - round(72 * SCALE),
        round(172 * SCALE) - round(72 * SCALE),
    )


def test_a_formula_table_without_a_box_is_left_alone(tmp_path: Path):
    client = ScriptedClient()
    page = PDFPageContent(page_number=1, markdown=FORMULA_TABLE, figures=[])
    result = _enricher(client).enrich(_document(tmp_path, page))
    assert result.pages[0].table_replacements == []
    assert client.attachments == []


def test_a_low_confidence_table_answer_is_not_used(tmp_path: Path):
    client = ScriptedClient({"markdown": "| a |\n| - |\n| $x$ |", "confidence": "low"})
    page = PDFPageContent(
        page_number=1, markdown=FORMULA_TABLE, figures=[], tables=[PDFTableBox(x0=0, y0=0, x1=100, y1=100)]
    )
    assert _enricher(client).enrich(_document(tmp_path, page)).pages[0].table_replacements == []


def test_a_plain_table_is_not_sent_to_the_model(tmp_path: Path):
    client = ScriptedClient()
    page = PDFPageContent(
        page_number=1,
        markdown="| a | b |\n| - | - |\n| 1 | 2 |",
        figures=[],
        tables=[PDFTableBox(x0=0, y0=0, x1=100, y1=100)],
    )
    _enricher(client).enrich(_document(tmp_path, page))
    assert client.attachments == []


def _equation(tmp_path: Path, number: int) -> PDFFigure:
    image = tmp_path / f"eq{number}.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    return PDFFigure(
        id=f"p1.fig{number}",
        page_number=1,
        asset_ref=f"images/eq{number}.png",
        image_path=image,
        width=100,
        height=20,
        kind="equation",
    )


def test_a_page_with_two_equations_is_rendered_whole_by_pdfium_and_replaced(tmp_path: Path):
    client = ScriptedClient({"markdown": "Page with $a=b$ and $c=d$", "confidence": "medium"})
    page = PDFPageContent(
        page_number=1, markdown="![p1.fig0](images/eq0.png)\n\n![p1.fig1](images/eq1.png)", figures=[]
    )
    page.figures = [_equation(tmp_path, 0), _equation(tmp_path, 1)]

    result = _enricher(client).enrich(_document(tmp_path, page))

    assert result.pages[0].math_markdown == "Page with $a=b$ and $c=d$"
    assert (result.pages[0].math_confidence, result.pages[0].math_model) == ("medium", "test-model")
    [attachment] = client.attachments
    assert attachment.name == "page-1.png"
    assert _png_size(attachment.data) == (round(612 * SCALE), round(792 * SCALE))


def test_a_single_equation_figure_is_transcribed_from_its_own_image(tmp_path: Path):
    client = ScriptedClient({"latex": " a = b ", "confidence": "high", "uncertain_tokens": ["b"]})
    page = PDFPageContent(page_number=1, markdown="![p1.fig0](images/eq0.png)", figures=[])
    page.figures = [_equation(tmp_path, 0)]

    result = _enricher(client).enrich(_document(tmp_path, page))

    [figure] = result.pages[0].figures
    assert (figure.latex, figure.latex_confidence, figure.latex_uncertain_tokens) == ("a = b", "high", ["b"])
    assert client.attachments[0].name == "eq0.png"
