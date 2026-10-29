from __future__ import annotations

import json
import time
from pathlib import Path

from anthropic import AnthropicError

from file_parsers.pdf.enricher import DEFAULT_MODEL, PdfFigureEnricher
from file_parsers.pdf.markdown_extractor import PdfMarkdownExtractor
from file_parsers.pdf.math_enricher import PdfMathEnricher
from file_parsers.pdf.mermaid_enricher import PdfMermaidEnricher
from file_parsers.pdf.renderer import PDFRenderer
from file_parsers.pdf.schema import PDFDocument
from llm.client import Client
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("file_parsers.pdf.parser")


class _StageTracker:
    """Logs pipeline stage transitions with per-stage timing and token usage.

    Each call to :meth:`enter` closes out the previous stage (logging its
    duration and, when an LLM ``Client`` is supplied, the token delta) and
    announces the new active stage. :meth:`finish` closes the final stage.
    Logs through the module-level ``logger`` via ``log_event`` -- nothing
    external constructs this with its own logger, so it no longer takes one.
    """

    def __init__(self) -> None:
        self._previous: str | None = None
        self._active: str | None = None
        self._start: float = 0.0
        self._client: Client | None = None
        self._base_in = 0
        self._base_out = 0

    def enter(self, name: str, *, client: Client | None = None) -> None:
        now = time.perf_counter()
        if self._active is not None:
            self._close(now)
        self._previous = self._active
        self._active = name
        self._start = now
        self._client = client
        self._base_in = getattr(client, "input_tokens", 0) if client else 0
        self._base_out = getattr(client, "output_tokens", 0) if client else 0
        log_event(
            logger,
            INFO,
            "pdf.stage.start",
            stage=self._active,
            previous_stage=self._previous,
        )

    def finish(self) -> None:
        if self._active is not None:
            self._close(time.perf_counter())
            self._previous = self._active
            self._active = None

    def _close(self, now: float) -> None:
        duration_ms = round((now - self._start) * 1000)
        if self._client is not None:
            input_tokens = getattr(self._client, "input_tokens", 0) - self._base_in
            output_tokens = getattr(self._client, "output_tokens", 0) - self._base_out
            cache_read_tokens = getattr(self._client, "cache_read_input_tokens", 0)
            cache_creation_tokens = getattr(self._client, "cache_creation_input_tokens", 0)
            log_event(
                logger,
                INFO,
                "pdf.stage.done",
                stage=self._active,
                duration_ms=duration_ms,
                llm=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )
        else:
            log_event(
                logger,
                INFO,
                "pdf.stage.done",
                stage=self._active,
                duration_ms=duration_ms,
                llm=False,
            )


class PDFParser:
    def __init__(self, pdf_path: Path, output_dir: Path) -> None:
        self.pdf_path = pdf_path
        self.output_dir = output_dir
        self.assets_dir = output_dir / "images"
        self.markdown_path = output_dir / "document.md"
        self.manifest_path = output_dir / "figures.json"
        self.document: PDFDocument | None = None
        self.pdf_markdown: str | None = None
        self.enrichment_error: str | None = None
        self.stages = _StageTracker()

    def parse(self) -> PDFDocument:
        self.stages.enter("parse")
        extractor = PdfMarkdownExtractor(self.pdf_path, self.assets_dir)
        self.document = extractor.extract()
        return self.document

    def enrich(self, model: str = DEFAULT_MODEL) -> PDFDocument:
        if self.document is None:
            raise RuntimeError("Call parse() before enrich().")
        deterministic_document = self.document.model_copy(deep=True)
        client = Client()
        try:
            self.stages.enter("enrich:figure-description", client=client)
            self.document = PdfFigureEnricher(client, model=model).enrich(self.document)
            self.stages.enter("enrich:mermaid", client=client)
            self.document = PdfMermaidEnricher(client, model=model).enrich(self.document)
            self.stages.enter("enrich:math", client=client)
            self.document = PdfMathEnricher(client, model=model).enrich(self.document)
        except AnthropicError as error:
            self.document = deterministic_document
            self.enrichment_error = type(error).__name__
            log_event(
                logger,
                WARNING,
                "pdf.enrich.fallback",
                exc_info=True,
                error_type=self.enrichment_error,
            )
        return self.document

    def render(self) -> str:
        if self.document is None:
            raise RuntimeError("Call parse() before render().")
        self.stages.enter("render")
        renderer = PDFRenderer(self.document)
        self.pdf_markdown = renderer.render()
        self.manifest = renderer.manifest()
        return self.pdf_markdown

    def save(self) -> None:
        if self.pdf_markdown is None:
            raise RuntimeError("Call render() before save().")
        self.stages.enter("save")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.markdown_path.write_text(self.pdf_markdown, encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse
    import sys
    from datetime import datetime

    from logger.correlation import new_request_context
    from logger.logger import close

    cli = argparse.ArgumentParser(description="Convert a PDF into Markdown.")
    cli.add_argument(
        "pdf_path",
        nargs="?",
        type=Path,
        default=Path("tests/data/specs/extraction.pdf"),
        help="PDF to convert (default: %(default)s)",
    )
    cli.add_argument(
        "--output-root",
        type=Path,
        default=Path(".local/data"),
        help="Parent directory for the timestamped output folder (default: %(default)s)",
    )
    cli.add_argument(
        "--enrich",
        "--describe-figures",
        dest="enrich",
        action="store_true",
        help=(
            "Add LLM-generated figure descriptions, Mermaid diagrams, LaTeX "
            "math, and formula tables; makes one or more API calls per page"
        ),
    )
    cli.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Vision model for --enrich (default: %(default)s)",
    )
    args = cli.parse_args()

    with new_request_context():
        try:
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            output_dir = args.output_root / f"{stamp}_{args.pdf_path.stem}"

            parser = PDFParser(args.pdf_path, output_dir)
            parser.parse()

            if args.enrich:
                from dotenv import load_dotenv

                load_dotenv()

            if args.enrich:
                parser.enrich(model=args.model)

            parser.render()
            parser.save()

            if parser.document is None:
                raise RuntimeError("PDFParser produced no document")
            figures = parser.document.figures
            described = sum(1 for figure in figures if figure.summary)
            sys.stdout.write(f"markdown: {parser.markdown_path}\n")
            sys.stdout.write(f"manifest: {parser.manifest_path}\n")
            sys.stdout.write(f"figures:  {len(figures)} ({described} described)\n")
            if parser.enrichment_error:
                sys.stdout.write(
                    f"enrichment: stopped after {parser.enrichment_error}; deterministic fallback was saved\n"
                )
            parser.stages.finish()
        finally:
            close()
