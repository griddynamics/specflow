"""``DoclingEngine``: PDF to per-page Markdown, Office and web files to sections, with Docling, behind the port.

The configuration is the one measured in the design (docs/docling-pdf-parser.md,
'Docling configuration'): docling-parse backend, TableFormer v1 in FAST mode with cell matching, no
OCR, no picture or formula enrichment, no remote services, no Docling heading hierarchy (SpecFlow
ranks the headings in ``file_parsers.pdf.structure``), Markdown with raw ``_``/``<``/``>`` and compact
tables. One engine holds one converter per format, so the models load once per process (about 4 s, PDF only);
call ``extract`` for 10-page ranges, which keeps the memory flat (1.8 GB instead of 4.7 GB).

Each converter allows ONE format, the stored one. Docling picks the backend from the bytes first (magic,
``filetype``) and the file extension second, while ``file_parsers.office.guard`` checks the limits of the stored
format; a converter allowing every format would run a PDF stored as Markdown through the PDF pipeline, or XLSX
bytes stored as DOCX through the XLSX backend, without their limits. With one allowed format, a guessed format
that differs makes Docling skip the input (``ConversionStatus.SKIPPED``, "File format not allowed") and, with
``raises_on_error``, raise ``ConversionError``; the facade maps it to ``document_parse_failed`` (Office/web) or
``pdf_parse_failed`` (PDF). A legitimate text file is never affected by Docling's MD/HTML/CSV guess: the parse
activity names the file ``original<extension>`` (``temporal.documents.conversion.local_name``), and Docling takes
the extension's format for text without magic bytes, before any content sniffing.

This module imports Docling at the top (eager). Only worker-parse code imports it, through
``file_parsers.convert`` from ``temporal.documents.activities``; the api and worker-agents processes never
do (lazy queue registration), and their images do not carry the ``parse`` extra.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.backend_options import CsvBackendOptions, HTMLBackendOptions, MarkdownBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import (
    HeadingHierarchyOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import (
    CsvFormatOption,
    DocumentConverter,
    FormatOption,
    HTMLFormatOption,
    MarkdownFormatOption,
    PdfFormatOption,
)
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.picture.picture import PictureItem
from docling_core.types.doc.items.table.table import TableItem

from file_parsers.engines.cpu import available_cpus
from file_parsers.engines.docling_office import to_office_content
from file_parsers.engines.port import PageRange, ProgressCallback, UnsupportedFormatError
from file_parsers.formats import PDF_MEDIA_TYPE, DocumentFormat, canonical_media_types, format_for_media_type
from file_parsers.office.model import OfficeContent
from file_parsers.pdf.pdfium_typed import open_document
from file_parsers.pdf.schema import PDFDocument, PDFFigure, PDFPageContent, PDFTableBox
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("file_parsers.engines.docling")

POST_PROCESSING_VERSION = 1
"""The ``sf`` part of the engine id: bump it whenever ``PdfStructure`` can change the output."""

IMAGES_DIR_NAME = "images"
FIGURE_DPI = 170
INPUT_FORMATS: dict[DocumentFormat, InputFormat] = {
    DocumentFormat.PDF: InputFormat.PDF,
    DocumentFormat.DOCX: InputFormat.DOCX,
    DocumentFormat.PPTX: InputFormat.PPTX,
    DocumentFormat.XLSX: InputFormat.XLSX,
    DocumentFormat.HTML: InputFormat.HTML,
    DocumentFormat.MARKDOWN: InputFormat.MD,
    DocumentFormat.CSV: InputFormat.CSV,
}
"""The Docling format of each SpecFlow format: the only one its converter allows."""
ALLOWED_FORMATS = tuple(INPUT_FORMATS.values())

_IMAGE_MARK = "￼SPECFLOW-IMAGE￼"
_BLANK_RUN = re.compile(r"\n{3,}")


def docling_version() -> str:
    return metadata.version("docling-slim")


def engine_id() -> str:
    """``docling@<docling version>+sf<post-processing version>``, for every format."""

    return f"docling@{docling_version()}+sf{POST_PROCESSING_VERSION}"


def default_threads() -> int:
    """The CPUs this process may use: its affinity capped by the cgroup CPU quota (``engines.cpu``).

    Docling hands this number to torch (``torch.set_num_threads``, layout and TableFormer) for every conversion.
    """

    return available_cpus()


def pipeline_options(*, images: bool, threads: int, artifacts_path: Path | None = None) -> PdfPipelineOptions:
    """The PDF pipeline options (everything not named here keeps Docling's default, which is off)."""

    options = PdfPipelineOptions()
    options.artifacts_path = artifacts_path
    options.accelerator_options = AcceleratorOptions(num_threads=threads, device="cpu")
    options.enable_remote_services = False
    options.allow_external_plugins = False
    options.do_ocr = False
    options.do_table_structure = True
    options.table_structure_options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.FAST)
    options.do_picture_classification = False
    options.do_picture_description = False
    options.do_chart_extraction = False
    options.do_code_enrichment = False
    options.do_formula_enrichment = False
    options.extract_form_fields = False
    options.generate_page_images = False
    options.generate_parsed_pages = False
    options.generate_picture_images = images
    options.images_scale = FIGURE_DPI / 72 if images else 1.0
    options.heading_hierarchy_options = HeadingHierarchyOptions(enabled=False)
    return options


def format_options(pdf_options: PdfPipelineOptions) -> dict[InputFormat, FormatOption]:
    """Per-format options: PDF as configured; HTML, Markdown and CSV never fetch anything."""

    return {
        InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
        InputFormat.HTML: HTMLFormatOption(
            backend_options=HTMLBackendOptions(enable_remote_fetch=False, enable_local_fetch=False, fetch_images=False)
        ),
        InputFormat.MD: MarkdownFormatOption(
            backend_options=MarkdownBackendOptions(
                enable_remote_fetch=False, enable_local_fetch=False, fetch_images=False
            )
        ),
        InputFormat.CSV: CsvFormatOption(
            backend_options=CsvBackendOptions(enable_remote_fetch=False, enable_local_fetch=False)
        ),
    }


class DoclingEngine:
    """Converts with one long-lived Docling converter per format (and, for PDF, per ``images`` setting).

    Models: ``artifacts_path`` when given; else Docling's own ``DOCLING_ARTIFACTS_PATH`` setting (the
    worker-parse image and the CI prefetch set it); else the Hugging Face cache.
    """

    formats = frozenset(canonical_media_types())

    def __init__(self, *, threads: int | None = None, artifacts_path: Path | None = None) -> None:
        self._threads = threads if threads is not None else default_threads()
        self._artifacts_path = artifacts_path
        # One converter per (format, images): each allows only its format. A process mixing both `images`
        # values loads the PDF models twice (production uses images=False only).
        self._converters: dict[tuple[InputFormat, bool], DocumentConverter] = {}
        self._lock = threading.Lock()

    @property
    def engine_id(self) -> str:
        return engine_id()

    def build_converter(self, input_format: InputFormat, *, images: bool = False) -> DocumentConverter:
        """A new converter that allows only ``input_format`` (models not loaded yet); the engine caches them."""

        if input_format not in ALLOWED_FORMATS:
            raise ValueError(f"{input_format} is not a format of the engine")
        options = pipeline_options(images=images, threads=self._threads, artifacts_path=self._artifacts_path)
        return DocumentConverter(allowed_formats=[input_format], format_options=format_options(options))

    def extract(
        self,
        path: Path,
        media_type: str,
        page_range: PageRange | None = None,
        *,
        images: bool = False,
        assets_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PDFDocument:
        if media_type != PDF_MEDIA_TYPE:
            raise UnsupportedFormatError(media_type)
        if images and assets_dir is None:
            raise ValueError("images=True needs assets_dir")
        path = Path(path)  # always a path: a str source would let Docling treat it as a URL
        with open_document(path) as pdf:
            pages_in_file = len(pdf)
            first, last = _checked_range(page_range, pages_in_file)
            blank = [n for n in range(first, last + 1) if pdf.char_count(n - 1) == 0]
        if blank:
            log_event(logger, WARNING, "pdf.pages.no_text_layer", pages=blank, count=len(blank))
        with self._lock:
            converter = self._converter(InputFormat.PDF, images=images)
            result = converter.convert(path, page_range=(first, last), raises_on_error=True)
        if result.status is not ConversionStatus.SUCCESS:
            raise RuntimeError(f"Docling did not convert every page: {result.status.value}")
        document = _to_pdf_document(result.document, path, first, last, assets_dir if images else None)
        total = last - first + 1
        log_event(logger, INFO, "pdf.range.done", first_page=first, last_page=last, pages=total)
        if on_progress is not None:
            on_progress(total, total)
        return document

    def extract_office(
        self,
        path: Path,
        media_type: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> OfficeContent:
        spec = format_for_media_type(media_type)
        if spec is None or spec.format is DocumentFormat.PDF:
            raise UnsupportedFormatError(media_type)
        path = Path(path)  # always a path: a str source would let Docling treat it as a URL
        with self._lock:
            result = self._converter(INPUT_FORMATS[spec.format]).convert(path, raises_on_error=True)
        if result.status is not ConversionStatus.SUCCESS:
            raise RuntimeError(f"Docling did not convert the document: {result.status.value}")
        content = to_office_content(result.document, spec.format)
        log_event(logger, INFO, "office.document.done", format=spec.format.value, sections=len(content.sections))
        if on_progress is not None:
            on_progress(len(content.sections), len(content.sections))
        return content

    def _converter(self, input_format: InputFormat, *, images: bool = False) -> DocumentConverter:
        # Caller holds the lock. The PDF models load on the first PDF conversion only; the Office and web
        # converters never load them.
        key = (input_format, images)
        converter = self._converters.get(key)
        if converter is None:
            converter = self.build_converter(input_format, images=images)
            self._converters[key] = converter
        return converter


def _checked_range(page_range: PageRange | None, pages_in_file: int) -> PageRange:
    if page_range is None:
        return 1, pages_in_file
    first, last = page_range
    if first < 1 or last < first:
        raise ValueError(f"invalid page range {page_range}")
    if last > pages_in_file:
        raise ValueError(f"page range {page_range} is beyond the {pages_in_file} pages of the document")
    return first, last


def _to_pdf_document(
    document: DoclingDocument, source: Path, first: int, last: int, assets_dir: Path | None
) -> PDFDocument:
    pictures = _group_by_page(item for item, _ in document.iterate_items() if isinstance(item, PictureItem))
    tables = _group_by_page(item for item, _ in document.iterate_items() if isinstance(item, TableItem))
    pages: list[PDFPageContent] = []
    for number in range(first, last + 1):
        markdown = document.export_to_markdown(
            page_no=number,
            escape_underscores=False,
            escape_html=False,
            compact_tables=True,
            image_placeholder=_IMAGE_MARK,
        )
        markdown, figures = _place_figures(markdown, number, pictures.get(number, []), document, assets_dir)
        boxes = _table_boxes(tables.get(number, []), document, number)
        pages.append(PDFPageContent(page_number=number, markdown=markdown, figures=figures, tables=boxes))
    return PDFDocument(source_path=source, pages=pages)


def _group_by_page[T: PictureItem | TableItem](items: Iterator[T]) -> dict[int, list[T]]:
    grouped: dict[int, list[T]] = {}
    for item in items:
        if item.prov:
            grouped.setdefault(item.prov[0].page_no, []).append(item)
    return grouped


def _place_figures(
    markdown: str,
    page_number: int,
    pictures: list[PictureItem],
    document: DoclingDocument,
    assets_dir: Path | None,
) -> tuple[str, list[PDFFigure]]:
    """Number the figure placeholders ``pN.figK`` in order; write the PNGs when ``assets_dir`` is given."""

    figures: list[PDFFigure] = []
    counter = itertools.count()  # K for every placeholder, however many there are

    def place(_: re.Match[str]) -> str:
        index = next(counter)
        figure_id = f"p{page_number}.fig{index}"
        name = f"{figure_id}.png"
        directory_name = assets_dir.name if assets_dir is not None else IMAGES_DIR_NAME
        if assets_dir is not None and index < len(pictures):
            figure = _write_figure(
                pictures[index],
                document,
                assets_dir=assets_dir,
                figure_id=figure_id,
                page_number=page_number,
                name=name,
            )
            if figure is not None:
                figures.append(figure)
        return f"![{figure_id}]({directory_name}/{name})"

    markdown = re.sub(re.escape(_IMAGE_MARK), place, markdown)
    markdown = "\n".join(line.rstrip() for line in markdown.splitlines())
    return _BLANK_RUN.sub("\n\n", markdown).strip(), figures


def _write_figure(
    picture: PictureItem,
    document: DoclingDocument,
    *,
    assets_dir: Path,
    figure_id: str,
    page_number: int,
    name: str,
) -> PDFFigure | None:
    image = picture.get_image(document)
    if image is None:
        return None
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / name
    image.save(target, format="PNG")
    return PDFFigure(
        id=figure_id,
        page_number=page_number,
        asset_ref=f"{assets_dir.name}/{name}",
        image_path=target,
        width=image.width,
        height=image.height,
    )


def _table_boxes(tables: list[TableItem], document: DoclingDocument, page_number: int) -> list[PDFTableBox]:
    page = document.pages.get(page_number)
    if page is None:
        return []
    boxes: list[PDFTableBox] = []
    for table in tables:
        box = table.prov[0].bbox.to_top_left_origin(page.size.height)
        boxes.append(PDFTableBox(x0=box.l, y0=box.t, x1=box.r, y1=box.b))
    return boxes
