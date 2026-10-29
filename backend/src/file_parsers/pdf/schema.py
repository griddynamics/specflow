from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class PDFFigure(BaseModel):
    id: str = Field(description="Stable id of the figure, e.g. 'p2.fig0'")
    page_number: int = Field(description="1-based page the figure was rendered from")
    asset_ref: str = Field(description="Path used inside the markdown, relative to it")
    image_path: Path = Field(description="Path to the rendered PNG on disk")
    width: int = Field(description="Pixel width of the rendered PNG")
    height: int = Field(description="Pixel height of the rendered PNG")
    kind: str | None = Field(
        default=None,
        description="chart | diagram | equation | photo | screenshot | other",
    )
    summary: str | None = Field(default=None, description="One-sentence summary of the figure")
    details: list[str] = Field(default_factory=list, description="Bullet points describing the figure")
    is_decorative: bool | None = Field(default=None, description="True if the figure carries no real content")
    latex: str | None = Field(default=None, description="LaTeX for a displayed equation")
    latex_confidence: str | None = Field(default=None, description="high | medium | low")
    latex_model: str | None = Field(default=None, description="Model that produced the LaTeX")
    latex_prompt_version: str | None = Field(default=None, description="Prompt id and version that produced the LaTeX")
    latex_uncertain_tokens: list[str] = Field(
        default_factory=list,
        description="Mathematical tokens that require human review",
    )
    mermaid: str | None = Field(default=None, description="Mermaid source when the figure is a convertible diagram")
    mermaid_type: str | None = Field(
        default=None,
        description="flowchart | sequenceDiagram | stateDiagram-v2 | classDiagram | erDiagram",
    )
    mermaid_model: str | None = Field(default=None, description="Model that produced the Mermaid source")
    mermaid_prompt_version: str | None = Field(
        default=None,
        description="Prompt id and version that produced the Mermaid source",
    )
    mermaid_uncertain_labels: list[str] = Field(
        default_factory=list,
        description="Source labels that were preserved literally but are ambiguous",
    )
    model: str | None = Field(default=None, description="Model that produced the description")
    prompt_version: str | None = Field(default=None, description="Prompt id and version that produced the description")


class PDFTableReplacement(BaseModel):
    """LLM-derived replacement for one source Markdown table."""

    id: str = Field(description="Stable id of the table, e.g. 'p8.table0'")
    source_markdown: str = Field(description="Deterministic table before enrichment")
    markdown: str = Field(description="Replacement GFM table with inline LaTeX")
    confidence: str = Field(description="high | medium | low")
    uncertain_cells: list[str] = Field(default_factory=list, description="Cells that require human review")
    model: str = Field(description="Model that produced the table")
    prompt_version: str = Field(description="Prompt id and version")


class PDFPageContent(BaseModel):
    page_number: int = Field(description="The page number of the document")
    markdown: str = Field(description="Cleaned markdown for this page")
    figures: list[PDFFigure] = Field(description="Figures rendered from this page")
    table_replacements: list[PDFTableReplacement] = Field(default_factory=list)
    math_markdown: str | None = Field(
        default=None,
        description="LLM-derived full-page Markdown with mathematical notation in LaTeX",
    )
    math_confidence: str | None = Field(default=None, description="high | medium | low")
    math_model: str | None = Field(default=None, description="Model that produced math_markdown")
    math_prompt_version: str | None = Field(
        default=None, description="Prompt id and version that produced math_markdown"
    )
    math_uncertain_regions: list[str] = Field(
        default_factory=list,
        description="Page regions that require human review",
    )


class PDFDocument(BaseModel):
    source_path: Path = Field(description="Path to the source PDF")
    pages: list[PDFPageContent] = Field(description="Pages in document order")

    @property
    def figures(self) -> list[PDFFigure]:
        return [figure for page in self.pages for figure in page.figures]
