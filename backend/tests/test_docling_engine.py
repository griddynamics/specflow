"""DoclingEngine: the options guard (DESIGN 'Docling configuration'), argument checks and real page ranges."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import TableFormerMode, TableStructureOptions

from file_parsers.convert import default_engine
from file_parsers.engines import docling as docling_module
from file_parsers.engines.docling import (
    ALLOWED_FORMATS,
    POST_PROCESSING_VERSION,
    DoclingEngine,
    default_threads,
    docling_version,
    engine_id,
    pipeline_options,
)
from file_parsers.engines.port import DocumentEngine, UnsupportedFormatError
from file_parsers.formats import canonical_media_types
from file_parsers.pdf.pdfium_typed import png_size
from logger_harness import events, fields
from pdf_builder_support import Line, write_pdf

FIXTURE = Path(__file__).parent / "data" / "specs" / "extraction.pdf"
PDF = "application/pdf"


def _prose(word: str) -> list[Line]:
    """A page of three plain lines, enough for the layout model to see a text block."""

    return [
        Line(f"This is the {word} page of the sample document and it holds plain prose.", 12),
        Line("The sentences are ordinary text that a reader would call a paragraph of the report.", 12),
        Line("Nothing on the page is a heading, a figure or a table, only running words.", 12),
    ]


# --- the options guard: what the converter is built with -------------------------------------


@pytest.mark.parametrize("images", [False, True])
def test_the_pipeline_options_are_fast_tables_without_ocr_or_enrichment_or_remote_access(*, images: bool):
    options = pipeline_options(images=images, threads=2)

    assert isinstance(options.table_structure_options, TableStructureOptions)
    assert options.do_table_structure is True
    assert options.table_structure_options.mode is TableFormerMode.FAST
    assert options.table_structure_options.do_cell_matching is True

    assert options.do_ocr is False
    assert options.do_picture_classification is False
    assert options.do_picture_description is False
    assert options.do_chart_extraction is False
    assert options.do_code_enrichment is False
    assert options.do_formula_enrichment is False
    assert options.extract_form_fields is False
    assert options.enable_remote_services is False
    assert options.allow_external_plugins is False
    assert options.heading_hierarchy_options.enabled is False  # SpecFlow ranks the headings
    assert (options.generate_page_images, options.generate_parsed_pages) == (False, False)

    assert options.accelerator_options.num_threads == 2
    assert options.accelerator_options.device == "cpu"
    assert options.generate_picture_images is images
    assert options.images_scale == (170 / 72 if images else 1.0)


def test_the_artifacts_path_is_passed_through_and_defaults_to_none(tmp_path: Path):
    assert pipeline_options(images=False, threads=1).artifacts_path is None
    assert pipeline_options(images=False, threads=1, artifacts_path=tmp_path).artifacts_path == tmp_path


def test_each_converter_allows_exactly_one_of_the_seven_formats_and_never_fetches():
    engine = DoclingEngine(threads=2)
    for input_format in ALLOWED_FORMATS:
        assert engine.build_converter(input_format).allowed_formats == [input_format]
    assert set(ALLOWED_FORMATS) == {
        InputFormat.PDF,
        InputFormat.DOCX,
        InputFormat.PPTX,
        InputFormat.XLSX,
        InputFormat.HTML,
        InputFormat.MD,
        InputFormat.CSV,
    }
    for kind in (InputFormat.HTML, InputFormat.MD, InputFormat.CSV):
        backend = engine.build_converter(kind).format_to_options[kind].backend_options
        assert backend is not None
        assert (backend.enable_remote_fetch, backend.enable_local_fetch) == (False, False)
    options = {kind: engine.build_converter(kind).format_to_options[kind] for kind in ALLOWED_FORMATS}
    for kind in (InputFormat.HTML, InputFormat.MD):
        assert getattr(options[kind].backend_options, "fetch_images", None) is False
    assert getattr(options[InputFormat.HTML].backend_options, "render_page", None) is False  # no headless browser
    pdf_options = options[InputFormat.PDF].pipeline_options
    assert pdf_options is not None
    assert pdf_options.do_ocr is False


def test_the_engine_id_names_the_docling_version_and_the_post_processing_version():
    assert engine_id() == f"docling@{docling_version()}+sf{POST_PROCESSING_VERSION}"
    assert engine_id() == "docling@2.145.0+sf1"
    assert DoclingEngine(threads=1).engine_id == engine_id()


def test_the_engine_satisfies_the_port_and_accepts_all_seven_formats():
    engine = DoclingEngine(threads=1)
    assert isinstance(engine, DocumentEngine)
    assert engine.formats == frozenset(canonical_media_types())
    assert PDF in engine.formats and len(engine.formats) == 7


def test_the_default_engine_is_one_docling_engine_per_process():
    assert isinstance(default_engine(), DoclingEngine)
    assert default_engine() is default_engine()


def test_the_thread_default_is_the_cpus_of_the_process():
    assert default_threads() >= 1
    assert DoclingEngine()._threads == default_threads()


def test_the_thread_default_is_the_quota_capped_cpu_count(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(docling_module, "available_cpus", lambda: 3)
    assert default_threads() == 3
    assert DoclingEngine()._threads == 3


# --- argument checks (nothing is converted, no model is loaded) --------------------------------


def test_a_media_type_other_than_pdf_raises_the_typed_error():
    with pytest.raises(UnsupportedFormatError) as error:
        DoclingEngine(threads=1).extract(FIXTURE, "text/csv")
    assert error.value.media_type == "text/csv"


def test_images_without_an_assets_dir_is_refused():
    with pytest.raises(ValueError, match="assets_dir"):
        DoclingEngine(threads=1).extract(FIXTURE, PDF, images=True)


@pytest.mark.parametrize("page_range", [(0, 3), (3, 2), (-1, 2)])
def test_an_invalid_range_is_refused(page_range: tuple[int, int]):
    with pytest.raises(ValueError, match="invalid page range"):
        DoclingEngine(threads=1).extract(FIXTURE, PDF, page_range)


def test_a_range_beyond_the_document_is_refused():
    with pytest.raises(ValueError, match=r"page range \(7, 9\) is beyond the 8 pages of the document"):
        DoclingEngine(threads=1).extract(FIXTURE, PDF, (7, 9))


# --- real conversions (the models come from the local cache or DOCLING_ARTIFACTS_PATH) ---------


@pytest.fixture(scope="module")
def engine() -> DocumentEngine:
    return default_engine()


def test_a_range_returns_exactly_those_pages_with_their_own_numbers_and_reports_progress(
    engine: DocumentEngine, tmp_path: Path
):
    pdf = write_pdf(tmp_path / "three.pdf", [_prose("first"), _prose("second"), _prose("third")])
    progress: list[tuple[int, int]] = []

    document = engine.extract(pdf, PDF, (2, 3), on_progress=lambda done, total: progress.append((done, total)))

    assert [page.page_number for page in document.pages] == [2, 3]
    assert "second page" in document.pages[0].markdown
    assert "third page" in document.pages[1].markdown
    assert "first page" not in "".join(page.markdown for page in document.pages)
    assert progress == [(2, 2)]
    assert document.source_path == pdf
    assert all(page.figures == [] and page.tables == [] for page in document.pages)


def test_pages_without_a_text_layer_are_logged_and_come_back_empty(
    engine: DocumentEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    pdf = write_pdf(tmp_path / "scan.pdf", [_prose("first"), [], []])
    caplog.set_level(logging.INFO, logger="file_parsers.engines.docling")

    document = engine.extract(pdf, PDF)

    assert [page.page_number for page in document.pages] == [1, 2, 3]
    assert document.pages[1].markdown == document.pages[2].markdown == ""
    [warning] = events(caplog, "pdf.pages.no_text_layer")
    assert warning.levelname == "WARNING"
    assert fields(warning)["pages"] == [2, 3]
    assert fields(warning)["count"] == 2
    [done] = events(caplog, "pdf.range.done")
    assert (fields(done)["first_page"], fields(done)["last_page"], fields(done)["pages"]) == (1, 3, 3)


def test_a_table_page_gets_table_boxes_in_pdf_points_and_no_figure_images(engine: DocumentEngine):
    document = engine.extract(FIXTURE, PDF, (4, 4))
    [page] = document.pages
    assert len(re.findall(r"^\| - \|", page.markdown, re.MULTILINE)) == 2  # two GFM separator rows
    assert len(page.tables) == 2
    for box in page.tables:
        assert 0 <= box.x0 < box.x1 <= 612
        assert 0 <= box.y0 < box.y1 <= 792
    assert page.tables[0].y0 < page.tables[1].y0  # top-left origin, in reading order
    assert page.figures == []


def test_figure_placeholders_are_numbered_per_page_and_no_png_is_written_without_images(
    engine: DocumentEngine, tmp_path: Path
):
    document = engine.extract(FIXTURE, PDF, (2, 2))
    [page] = document.pages
    assert "![p2.fig0](images/p2.fig0.png)" in page.markdown
    assert page.figures == []
    assert list(tmp_path.iterdir()) == []


def test_images_true_writes_the_figure_pngs_and_describes_them(tmp_path: Path):
    assets = tmp_path / "images"
    document = DoclingEngine(threads=2).extract(FIXTURE, PDF, (5, 5), images=True, assets_dir=assets)
    [page] = document.pages
    [figure] = page.figures
    assert figure.id == "p5.fig0"
    assert figure.page_number == 5
    assert figure.asset_ref == "images/p5.fig0.png"
    assert figure.image_path == assets / "p5.fig0.png"
    assert png_size(figure.image_path) == (figure.width, figure.height)
    assert figure.width > 64 and figure.height > 64
    assert "![p5.fig0](images/p5.fig0.png)" in page.markdown
    assert [path.name for path in assets.iterdir()] == ["p5.fig0.png"]
