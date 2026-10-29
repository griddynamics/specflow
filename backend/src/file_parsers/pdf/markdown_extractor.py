"""PDF to per-page Markdown, deterministically.

pymupdf4llm (backed by the pymupdf-layout model) does the hard parts: heading
levels, table reconstruction, and rendering vector figures that
``page.get_images()`` cannot see. This module normalises its output — dropping
only truly tiny images, rewriting asset references to be relative to the
Markdown file, and removing the redundant bold that wraps every heading.

No LLM calls happen here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pymupdf4llm

from file_parsers.pdf.pymupdf_typed import open_pixmap
from file_parsers.pdf.schema import PDFDocument, PDFFigure, PDFPageContent

DEFAULT_DPI = 170
MIN_FIGURE_PX = 64

_IMAGE_REF = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)")
_BOLD_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+\*\*(?P<title>.+?)\*\*\s*$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def _png_size(path: Path) -> tuple[int, int]:
    pixmap = open_pixmap(path)
    return pixmap.width, pixmap.height


class PdfMarkdownExtractor:
    def __init__(
        self,
        pdf_path: Path,
        assets_dir: Path,
        dpi: int = DEFAULT_DPI,
        min_figure_px: int = MIN_FIGURE_PX,
    ) -> None:
        self.pdf_path = pdf_path
        self.assets_dir = assets_dir
        self.dpi = dpi
        self.min_figure_px = min_figure_px

    def extract(self) -> PDFDocument:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        chunks = cast(
            list[dict[str, Any]],
            pymupdf4llm.to_markdown(
                str(self.pdf_path),
                page_chunks=True,
                write_images=True,
                image_path=str(self.assets_dir),
                image_format="png",
                dpi=self.dpi,
                table_output="markdown",
            ),
        )

        pages: list[PDFPageContent] = []
        for chunk in chunks:
            page_number = int(chunk["metadata"]["page_number"])
            markdown, figures = self._normalise(chunk["text"], page_number)
            pages.append(PDFPageContent(page_number=page_number, markdown=markdown, figures=figures))
        return PDFDocument(source_path=self.pdf_path, pages=pages)

    def _normalise(self, text: str, page_number: int) -> tuple[str, list[PDFFigure]]:
        figures: list[PDFFigure] = []
        discarded: list[Path] = []

        def rewrite(match: re.Match[str]) -> str:
            path = Path(match.group("path"))
            if not path.exists():
                return match.group(0)

            width, height = _png_size(path)
            # Display equations are often long but only a few pixels high.
            # Keeping any asset with one useful dimension preserves them for
            # the optional LaTeX enrichment stage while still dropping glyphs.
            if max(width, height) < self.min_figure_px:
                discarded.append(path)
                return ""

            figure = PDFFigure(
                id=f"p{page_number}.fig{len(figures)}",
                page_number=page_number,
                asset_ref=f"{self.assets_dir.name}/{path.name}",
                image_path=path,
                width=width,
                height=height,
            )
            figures.append(figure)
            return f"![{figure.id}]({figure.asset_ref})"

        text = _IMAGE_REF.sub(rewrite, text)
        text = _BOLD_HEADING.sub(lambda m: f"{m.group('hashes')} {m.group('title').strip()}", text)
        text = "\n".join(line.rstrip() for line in text.splitlines())
        text = _BLANK_RUN.sub("\n\n", text)

        self._remove_discarded(discarded)
        return text.strip(), figures

    def _remove_discarded(self, discarded: list[Path]) -> None:
        # Only ever touches PNGs this extractor just wrote into its own assets
        # directory; re-running the extraction recreates them.
        assets = self.assets_dir.resolve()
        for path in discarded:
            if path.resolve().parent == assets:
                path.unlink(missing_ok=True)
