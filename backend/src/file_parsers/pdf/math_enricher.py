"""Recover LaTeX from equation figures and mathematics-heavy PDF pages."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from file_parsers.pdf.enrichment_utils import (
    load_prompt,
    render_prompt,
    request_json,
)
from file_parsers.pdf.pymupdf_typed import PageView, TableView, make_rect, open_document
from file_parsers.pdf.schema import (
    PDFDocument,
    PDFFigure,
    PDFPageContent,
    PDFTableReplacement,
)
from llm.client import Client
from llm.content import Attachment

EQUATION_PROMPT_ID = "equation_latex.v1"
MATH_PAGE_PROMPT_ID = "math_page_latex.v2"
TABLE_PROMPT_ID = "formula_table_latex.v1"

PAGE_DPI = 220
MAX_CONTEXT_CHARS = 6000
MIN_EQUATIONS_FOR_MATH_PAGE = 2
_MIN_TABLE_LINES = 2
_TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_FENCED_MARKDOWN = re.compile(r"^\s*```(?:markdown|md)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


class EquationExtraction(BaseModel):
    latex: str
    confidence: Literal["high", "medium", "low"]
    uncertain_tokens: list[str] = Field(default_factory=list)


class MathPageExtraction(BaseModel):
    markdown: str
    confidence: Literal["high", "medium", "low"]
    uncertain_regions: list[str] = Field(default_factory=list)

    @field_validator("markdown")
    @classmethod
    def _normalise_markdown(cls, value: str) -> str:
        return _normalise_alignment_markers(_strip_markdown_fence(value))


class FormulaTableExtraction(BaseModel):
    markdown: str
    confidence: Literal["high", "medium", "low"]
    uncertain_cells: list[str] = Field(default_factory=list)

    @field_validator("markdown")
    @classmethod
    def _require_gfm_table(cls, value: str) -> str:
        value = _strip_markdown_fence(value)
        if not _is_gfm_table(value):
            raise ValueError("Response did not contain one GitHub-flavored table.")
        return value


class PdfMathEnricher:
    """Use a vision model only where deterministic Markdown loses math."""

    def __init__(
        self,
        client: Client,
        model: str,
        *,
        equation_max_tokens: int = 1800,
        page_max_tokens: int = 8000,
        table_max_tokens: int = 3500,
        attempts: int = 2,
    ) -> None:
        self.client = client
        self.model = model
        self.equation_max_tokens = equation_max_tokens
        self.page_max_tokens = page_max_tokens
        self.table_max_tokens = table_max_tokens
        self.attempts = attempts
        self.equation_template = load_prompt(EQUATION_PROMPT_ID)
        self.page_template = load_prompt(MATH_PAGE_PROMPT_ID)
        self.table_template = load_prompt(TABLE_PROMPT_ID)

    def enrich(self, document: PDFDocument) -> PDFDocument:
        with open_document(document.source_path) as source:
            for page in document.pages:
                source_page = source[page.page_number - 1]
                if self._replace_math_page(page, source_page):
                    continue
                self._replace_equations(page)
                self._replace_formula_tables(page, source_page)
        return document

    def _replace_math_page(self, page: PDFPageContent, source_page: PageView) -> bool:
        if sum(figure.kind == "equation" for figure in page.figures) < MIN_EQUATIONS_FOR_MATH_PAGE:
            return False

        prompt = render_prompt(
            self.page_template,
            source_markdown=page.markdown[:MAX_CONTEXT_CHARS],
        )
        result = request_json(
            self.client,
            prompt=prompt,
            model=self.model,
            max_tokens=self.page_max_tokens,
            attachment=_page_attachment(source_page, page.page_number),
            schema=MathPageExtraction,
            attempts=self.attempts,
        )
        if result is None or result.confidence == "low":
            return False

        page.math_markdown = result.markdown
        page.math_confidence = result.confidence
        page.math_model = self.model
        page.math_prompt_version = MATH_PAGE_PROMPT_ID
        page.math_uncertain_regions = result.uncertain_regions
        return True

    def _replace_equations(self, page: PDFPageContent) -> None:
        for figure in page.figures:
            if figure.kind != "equation":
                continue
            result = self._transcribe_equation(figure, page)
            if result is None or result.confidence == "low":
                continue
            figure.latex = result.latex.strip()
            figure.latex_confidence = result.confidence
            figure.latex_model = self.model
            figure.latex_prompt_version = EQUATION_PROMPT_ID
            figure.latex_uncertain_tokens = result.uncertain_tokens

    def _transcribe_equation(self, figure: PDFFigure, page: PDFPageContent) -> EquationExtraction | None:
        prompt = render_prompt(
            self.equation_template,
            page_number=str(page.page_number),
            page_context=page.markdown[:MAX_CONTEXT_CHARS],
        )
        return request_json(
            self.client,
            prompt=prompt,
            model=self.model,
            max_tokens=self.equation_max_tokens,
            attachment=Attachment.from_path(figure.image_path),
            schema=EquationExtraction,
            attempts=self.attempts,
        )

    def _replace_formula_tables(self, page: PDFPageContent, source_page: PageView) -> None:
        markdown_tables = _table_blocks(page.markdown)
        if not markdown_tables:
            return

        source_tables = list(source_page.find_tables())
        for index, source_markdown in enumerate(markdown_tables):
            if not _looks_like_formula_table(source_markdown):
                continue
            if index >= len(source_tables):
                continue

            prompt = render_prompt(
                self.table_template,
                source_table=source_markdown[:MAX_CONTEXT_CHARS],
            )
            result = request_json(
                self.client,
                prompt=prompt,
                model=self.model,
                max_tokens=self.table_max_tokens,
                attachment=_table_attachment(source_page, source_tables[index], page.page_number, index),
                schema=FormulaTableExtraction,
                attempts=self.attempts,
            )
            if result is None or result.confidence == "low":
                continue
            page.table_replacements.append(
                PDFTableReplacement(
                    id=f"p{page.page_number}.table{index}",
                    source_markdown=source_markdown,
                    markdown=result.markdown,
                    confidence=result.confidence,
                    uncertain_cells=result.uncertain_cells,
                    model=self.model,
                    prompt_version=TABLE_PROMPT_ID,
                )
            )


def _page_attachment(page: PageView, page_number: int) -> Attachment:
    pixmap = page.get_pixmap(dpi=PAGE_DPI, alpha=False)
    return Attachment(
        data=pixmap.tobytes("png"),
        media_type="image/png",
        name=f"page-{page_number}.png",
    )


def _table_attachment(page: PageView, table: TableView, page_number: int, table_index: int) -> Attachment:
    bbox = make_rect(table.bbox)
    pixmap = page.get_pixmap(dpi=PAGE_DPI, clip=bbox, alpha=False)
    return Attachment(
        data=pixmap.tobytes("png"),
        media_type="image/png",
        name=f"page-{page_number}-table-{table_index}.png",
    )


def _table_blocks(markdown: str) -> list[str]:
    return [block for block in markdown.split("\n\n") if _is_gfm_table(block)]


def _is_gfm_table(markdown: str) -> bool:
    lines = [line for line in markdown.strip().splitlines() if line.strip()]
    return (
        len(lines) >= _MIN_TABLE_LINES and lines[0].lstrip().startswith("|") and bool(_TABLE_SEPARATOR.match(lines[1]))
    )


def _looks_like_formula_table(markdown: str) -> bool:
    math_markers = ("=", "π", "θ", "_", "^", "√")
    return "<br>" in markdown and any(marker in markdown for marker in math_markers)


def _strip_markdown_fence(markdown: str) -> str:
    match = _FENCED_MARKDOWN.match(markdown)
    return (match.group("body") if match else markdown).strip()


def _normalise_alignment_markers(markdown: str) -> str:
    """Remove prose-colons accidentally placed before aligned equality rows."""
    return re.sub(r"(?m)^:\s*&=", "&=", markdown)
