"""``file_to_markdown``: the one entry point from a stored original to Markdown.

Routes by media type (``file_parsers.formats``) to an engine behind the ``DocumentEngine`` port.
Only PDF has a path until the Office/web formats arrive; any other type raises
``UnsupportedFormatError``. The default engine is Docling (``file_parsers.engines.default``); the import is
eager. Only worker-parse code (``temporal.documents.activities``) imports this module: the api and
worker-agents processes never do (lazy queue registration).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from file_parsers.engines.default import default_engine
from file_parsers.engines.port import DocumentEngine, ProgressCallback, UnsupportedFormatError
from file_parsers.formats import DocumentFormat, format_for_media_type
from file_parsers.limits import DocumentTooLargeError
from file_parsers.pdf.parser import PDFParser


class PdfParseError(Exception):
    """The file is not a parseable PDF."""


@dataclass(frozen=True, slots=True)
class ParsedMarkdown:
    """The converted document and the id of the engine that produced it (``documents.parser``)."""

    markdown: str
    parser: str


def file_to_markdown(
    path: Path,
    media_type: str,
    *,
    engine: DocumentEngine | None = None,
    on_progress: ProgressCallback | None = None,
) -> ParsedMarkdown:
    """Convert the file at ``path`` of ``media_type`` to Markdown.

    ``media_type`` is the stored type (a canonical type or a general alias). Raises
    ``UnsupportedFormatError`` for a type that is not accepted or has no path yet,
    ``DocumentTooLargeError`` for a PDF over ``limits.MAX_PDF_PAGES`` pages and ``PdfParseError``
    when a PDF cannot be parsed. ``on_progress(pages_done, pages_total)`` is called after every
    10-page range (see ``engines.port.ProgressCallback``). Blocking and CPU-bound: call it from a
    worker thread. Importing this module imports the shared PDF parser, which pulls in the
    ``anthropic``/LLM client packages; nothing from them is called here.
    """

    spec = format_for_media_type(media_type)
    if spec is None or spec.format is not DocumentFormat.PDF:
        raise UnsupportedFormatError(media_type)
    return _pdf_to_markdown(Path(path), engine if engine is not None else default_engine(), on_progress)


def _pdf_to_markdown(path: Path, engine: DocumentEngine, on_progress: ProgressCallback | None) -> ParsedMarkdown:
    # No figure images are written: the stored Markdown keeps placeholders only. The temporary
    # directory is the parser's output folder and is always removed.
    with tempfile.TemporaryDirectory(prefix="specflow-parse-") as tmp:
        parser = PDFParser(path, Path(tmp), engine, images=False)
        try:
            parser.parse(on_progress)
            markdown = parser.render()
        except DocumentTooLargeError:
            raise
        except Exception as exc:
            raise PdfParseError(f"cannot parse PDF: {type(exc).__name__}") from exc
        finally:
            parser.stages.finish()
    return ParsedMarkdown(markdown=markdown, parser=engine.engine_id)
