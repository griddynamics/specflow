"""Deterministic assembly of extracted pages into one Markdown document.

Assembly is kept in code rather than delegated to a model so the output is
reproducible and diffable, and so a single page can be re-extracted without
disturbing the rest of the document.

Model-written content is preceded by a provenance comment carrying its figure
id, model and prompt version. Keeping Markdown outside custom HTML elements
ensures Mermaid, LaTeX, lists, and tables remain renderable.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from file_parsers.pdf.schema import (
    PDFDocument,
    PDFFigure,
    PDFPageContent,
    PDFTableReplacement,
)

_IMAGE_REF = re.compile(r"^!\[(?P<id>[^\]]*)\]\([^)]+\)$")
_CAPTION = re.compile(r"^\*{0,2}(Figure|Table)\b")


def _provenance_comment(name: str, **attributes: str | None) -> str:
    rendered = [f'{key}="{escape(value, quote=True)}"' for key, value in attributes.items() if value is not None]
    return f"<!-- {name} {' '.join(rendered)} -->"


def _description_block(figure: PDFFigure) -> str:
    lines = [
        _provenance_comment(
            "llm-description",
            figure=figure.id,
            kind=figure.kind,
            model=figure.model,
            prompt=figure.prompt_version,
        ),
        "",
        figure.summary or "",
    ]
    if figure.details:
        lines.append("")
        lines.extend(f"- {detail}" for detail in figure.details)
    return "\n".join(lines)


def _equation_block(figure: PDFFigure) -> str:
    attributes = {
        "figure": figure.id,
        "source": figure.asset_ref,
        "model": figure.latex_model,
        "prompt": figure.latex_prompt_version,
        "confidence": figure.latex_confidence,
    }
    if figure.latex_uncertain_tokens:
        attributes["uncertain-tokens"] = ", ".join(figure.latex_uncertain_tokens)
    return "\n".join(
        [
            _provenance_comment("llm-equation", **attributes),
            "",
            "$$",
            figure.latex or "",
            "$$",
        ]
    )


def _mermaid_block(figure: PDFFigure) -> str:
    attributes = {
        "figure": figure.id,
        "source": figure.asset_ref,
        "type": figure.mermaid_type,
        "model": figure.mermaid_model,
        "prompt": figure.mermaid_prompt_version,
    }
    if figure.mermaid_uncertain_labels:
        attributes["literal-ambiguous-labels"] = ", ".join(figure.mermaid_uncertain_labels)
    return "\n".join(
        [
            _provenance_comment("llm-mermaid", **attributes),
            "",
            "```mermaid",
            figure.mermaid or "",
            "```",
        ]
    )


def _table_block(table: PDFTableReplacement) -> str:
    attributes = {
        "table": table.id,
        "model": table.model,
        "prompt": table.prompt_version,
        "confidence": table.confidence,
    }
    if table.uncertain_cells:
        attributes["uncertain-cells"] = ", ".join(table.uncertain_cells)
    return "\n".join(
        [
            _provenance_comment("llm-table", **attributes),
            "",
            table.markdown,
        ]
    )


def _math_page_block(page: PDFPageContent) -> str:
    attributes = {
        "page": str(page.page_number),
        "model": page.math_model,
        "prompt": page.math_prompt_version,
        "confidence": page.math_confidence,
    }
    if page.math_uncertain_regions:
        attributes["uncertain-regions"] = ", ".join(page.math_uncertain_regions)
    content = page.math_markdown or ""
    missing_captions = [
        block for block in page.markdown.split("\n\n") if _CAPTION.match(block.strip()) and block not in content
    ]
    if missing_captions:
        content = "\n\n".join([content, *missing_captions])
    return "\n".join(
        [
            _provenance_comment("llm-math-page", **attributes),
            "",
            content,
        ]
    )


class PDFRenderer:
    def __init__(self, document: PDFDocument) -> None:
        self.document = document

    def render(self) -> str:
        blocks = [
            f"<!-- source-page: {page.page_number} -->\n\n{self._render_page(page)}" for page in self.document.pages
        ]
        return "\n\n".join(block for block in blocks if block) + "\n"

    def manifest(self) -> list[dict[str, Any]]:
        return [figure.model_dump(mode="json", exclude={"image_path"}) for figure in self.document.figures]

    def _render_page(self, page: PDFPageContent) -> str:
        if page.math_markdown:
            return _math_page_block(page)

        described = {
            figure.id: figure
            for figure in page.figures
            if (figure.summary and figure.kind != "equation" and not figure.is_decorative)
        }
        markdown = page.markdown
        for table in page.table_replacements:
            markdown = markdown.replace(table.source_markdown, _table_block(table), 1)

        blocks = markdown.split("\n\n")
        rendered: list[str] = []
        index = 0
        while index < len(blocks):
            block = blocks[index]
            index += 1

            match = _IMAGE_REF.match(block.strip())
            figure_id = match.group("id") if match else None
            source_figure = next((item for item in page.figures if item.id == figure_id), None)
            if source_figure and source_figure.latex:
                rendered.append(_equation_block(source_figure))
                continue

            rendered.append(block)
            if source_figure is None:
                continue

            # Keep the document's own caption attached to its image; the
            # generated description follows it.
            if index < len(blocks) and _CAPTION.match(blocks[index].strip()):
                rendered.append(blocks[index])
                index += 1
            if figure_id is not None and (figure := described.get(figure_id)):
                rendered.append(_description_block(figure))
            if source_figure.mermaid:
                rendered.append(_mermaid_block(source_figure))

        return "\n\n".join(rendered)
