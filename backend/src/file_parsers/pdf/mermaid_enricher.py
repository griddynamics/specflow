"""Conditionally convert technical-diagram figures into Mermaid source."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from file_parsers.pdf.enrichment_utils import (
    load_prompt,
    render_prompt,
    request_json,
)
from file_parsers.pdf.schema import PDFDocument, PDFFigure
from llm.client import Client
from llm.content import Attachment

PROMPT_ID = "diagram_mermaid.v2"
MAX_CONTEXT_CHARS = 4000


class MermaidExtraction(BaseModel):
    convertible: bool
    diagram_type: (
        Literal[
            "flowchart",
            "sequenceDiagram",
            "stateDiagram-v2",
            "classDiagram",
            "erDiagram",
        ]
        | None
    ) = None
    mermaid: str | None = None
    literal_ambiguous_labels: list[str] = Field(default_factory=list)


class PdfMermaidEnricher:
    """Generate Mermaid only for figures first classified as diagrams."""

    def __init__(
        self,
        client: Client,
        model: str,
        max_tokens: int = 2200,
        attempts: int = 2,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.attempts = attempts
        self.template = load_prompt(PROMPT_ID)

    def enrich(self, document: PDFDocument) -> PDFDocument:
        for page in document.pages:
            for figure in page.figures:
                if figure.kind != "diagram" or figure.is_decorative:
                    continue
                result = self._convert(figure, page.markdown, page.page_number)
                if result and result.convertible and result.mermaid:
                    figure.mermaid = _strip_mermaid_fence(result.mermaid)
                    figure.mermaid_type = result.diagram_type
                    figure.mermaid_model = self.model
                    figure.mermaid_prompt_version = PROMPT_ID
                    figure.mermaid_uncertain_labels = result.literal_ambiguous_labels
        return document

    def _convert(self, figure: PDFFigure, page_markdown: str, page_number: int) -> MermaidExtraction | None:
        prompt = render_prompt(
            self.template,
            page_number=str(page_number),
            page_context=page_markdown[:MAX_CONTEXT_CHARS],
        )
        return request_json(
            self.client,
            prompt=prompt,
            model=self.model,
            max_tokens=self.max_tokens,
            attachment=Attachment.from_path(figure.image_path),
            schema=MermaidExtraction,
            attempts=self.attempts,
        )


def _strip_mermaid_fence(source: str) -> str:
    lines = source.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
