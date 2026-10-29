"""Fill figure descriptions on a PDFDocument using a vision model.

Each figure is sent as an image attachment together with the Markdown of the
page it came from. The page context matters: small labels inside a rendered
figure are frequently misread, and the surrounding text already spells the
document's own names correctly.

A figure whose description fails to parse is left un-enriched rather than
failing the run, so a bad response costs one figure and not the document.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from file_parsers.pdf.enrichment_utils import (
    load_prompt,
    render_prompt,
    request_json,
)
from file_parsers.pdf.schema import PDFDocument, PDFFigure
from llm.client import Client
from llm.content import Attachment

PROMPT_ID = "figure_description.v2"
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_CONTEXT_CHARS = 4000

GLOSSARY_MIN_COUNT = 2
GLOSSARY_MIN_TERM_LENGTH = 3
GLOSSARY_MAX_TERMS = 40

# Identifier-shaped tokens: CamelCase (WildGuard), acronyms and hyphenated
# acronyms (AEGIS, HH-RLHF), and versioned names (GPT-5.4).
_IDENTIFIER = re.compile(
    r"\b(?:"
    r"[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+"
    r"|[A-Z]{2,}(?:-[A-Z0-9]+)*"
    r"|[A-Z][A-Za-z]*-[A-Z0-9][A-Za-z0-9.]*"
    r")\b"
)


class FigureDescription(BaseModel):
    kind: Literal["chart", "diagram", "equation", "screenshot", "photo", "other"]
    summary: str
    details: list[str] = Field(default_factory=list)
    is_decorative: bool = False

    @field_validator("details", mode="before")
    @classmethod
    def _accept_single_string(cls, value: object) -> object:
        if isinstance(value, str):
            return [line.lstrip("-* ").strip() for line in value.splitlines() if line.strip()]
        return value


def build_glossary(document: PDFDocument) -> list[str]:
    """Recurring identifiers across the document, as a spelling reference.

    Figure legends are rendered small and get misread; the same names almost
    always appear in body text or tables somewhere else in the document, so a
    document-wide list is a far better anchor than any single page.
    """
    counts = Counter(
        term
        for page in document.pages
        for term in _IDENTIFIER.findall(page.markdown)
        if len(term) >= GLOSSARY_MIN_TERM_LENGTH
    )
    ranked = [term for term, count in counts.most_common() if count >= GLOSSARY_MIN_COUNT]
    return ranked[:GLOSSARY_MAX_TERMS]


class PdfFigureEnricher:
    def __init__(
        self,
        client: Client,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 900,
        attempts: int = 2,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.attempts = attempts
        self.template = load_prompt(PROMPT_ID)

    def enrich(self, document: PDFDocument) -> PDFDocument:
        glossary = ", ".join(build_glossary(document))
        for page in document.pages:
            for figure in page.figures:
                description = self._describe(figure, page.markdown, page.page_number, glossary)
                if description is not None:
                    self._apply(figure, description)
        return document

    def _describe(
        self,
        figure: PDFFigure,
        page_markdown: str,
        page_number: int,
        glossary: str,
    ) -> FigureDescription | None:
        prompt = render_prompt(
            self.template,
            page_number=str(page_number),
            glossary=glossary,
            page_context=page_markdown[:MAX_CONTEXT_CHARS],
        )
        attachment = Attachment.from_path(figure.image_path)
        return request_json(
            self.client,
            prompt=prompt,
            model=self.model,
            max_tokens=self.max_tokens,
            attachment=attachment,
            schema=FigureDescription,
            attempts=self.attempts,
        )

    def _apply(self, figure: PDFFigure, description: FigureDescription) -> None:
        figure.kind = description.kind
        figure.summary = description.summary
        figure.details = description.details
        figure.is_decorative = description.is_decorative
        figure.model = self.model
        figure.prompt_version = PROMPT_ID
