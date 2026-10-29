"""PDF to Markdown (SPECS §8): deterministic parse and render, no LLM, no enrichment."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from file_parsers.pdf.parser import PDFParser


class PdfParseError(Exception):
    """The file is not a parseable PDF."""


@dataclass(frozen=True, slots=True)
class ParsedMarkdown:
    markdown: str
    parser: str = "pymupdf4llm"


def pdf_file_to_markdown(path: Path) -> ParsedMarkdown:
    """Parse ``path`` with the shared ``PDFParser`` (``parse`` then ``render``).

    Figures are written to a temporary directory that is always removed. This is
    blocking and CPU-bound: call it from a worker thread. Importing this module
    imports the shared parser, which pulls in the ``anthropic``/LLM client
    packages; nothing from them is called here.
    """

    with tempfile.TemporaryDirectory(prefix="specflow-parse-") as tmp:
        parser = PDFParser(Path(path), Path(tmp))
        try:
            parser.parse()
            markdown = parser.render()
        except Exception as exc:
            raise PdfParseError(f"cannot parse PDF: {type(exc).__name__}") from exc
        finally:
            parser.stages.finish()
    return ParsedMarkdown(markdown=markdown)
