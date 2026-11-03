"""``file_to_markdown``: the one entry point from a stored original to Markdown.

Routes by media type (``file_parsers.formats``) to an engine behind the ``DocumentEngine`` port: PDF through
``PDFParser``, DOCX/PPTX/XLSX/HTML/Markdown/CSV through ``office.guard`` (limits), the engine and
``OfficeStructure``. Any other type raises ``UnsupportedFormatError``. The default engine is Docling
(``file_parsers.engines.default``); the import is eager. Only worker-parse code
(``temporal.documents.activities``) imports this module: the api and worker-agents processes never do
(lazy queue registration).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from file_parsers.engines.default import default_engine
from file_parsers.engines.port import DocumentEngine, ProgressCallback, UnsupportedFormatError
from file_parsers.formats import DocumentFormat, format_for_media_type
from file_parsers.limits import DocumentParseFailedError, DocumentTooLargeError, check_markdown_size
from file_parsers.office.guard import inspect_file
from file_parsers.office.structure import OfficeStructure
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
    ``UnsupportedFormatError`` for a type that is not accepted, ``DocumentTooLargeError`` for a PDF
    over ``limits.MAX_PDF_PAGES`` pages or an Office/web file over a limit of ``limits`` (inflated
    bytes, ratio, slides, cells, Markdown size), ``PdfParseError`` when a PDF cannot be parsed and
    ``DocumentParseFailedError`` when another format cannot. ``on_progress(pages_done, pages_total)``
    is called after every 10-page range of a PDF and once, at the end, for the other formats (see
    ``engines.port.ProgressCallback``). Blocking and CPU-bound: call it from a
    worker thread. Importing this module imports the shared PDF parser, which pulls in the
    ``anthropic``/LLM client packages; nothing from them is called here.
    """

    spec = format_for_media_type(media_type)
    if spec is None:
        raise UnsupportedFormatError(media_type)
    engine = engine if engine is not None else default_engine()
    if spec.format is DocumentFormat.PDF:
        return _pdf_to_markdown(Path(path), engine, on_progress)
    return _office_to_markdown(Path(path), spec.format, spec.media_type, engine, on_progress)


def _office_to_markdown(
    path: Path,
    document_format: DocumentFormat,
    media_type: str,
    engine: DocumentEngine,
    on_progress: ProgressCallback | None,
) -> ParsedMarkdown:
    # The guard reads the file in chunks and refuses it before the engine loads any of it.
    inspect_file(path, document_format)
    try:
        content = engine.extract_office(path, media_type, on_progress=on_progress)
    except UnsupportedFormatError:
        raise
    except Exception as exc:
        raise DocumentParseFailedError(f"cannot parse {document_format.value}: {type(exc).__name__}") from exc
    markdown = OfficeStructure().render(content)
    check_markdown_size(markdown)
    return ParsedMarkdown(markdown=markdown, parser=engine.engine_id)


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
