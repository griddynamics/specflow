"""A file whose bytes are another format than the stored one (review H1): refused by the guard and by the engine.

Docling picks its backend from the bytes first, the extension second; the guard checks the limits of the stored
format. Both layers refuse a mismatch: ``office.guard`` (via ``office.kind``) before Docling reads anything, and
``DoclingEngine``, whose converter allows only the stored format, when the guard is bypassed. Legitimate files whose
content Docling could guess differently (Markdown full of HTML, HTML without a doctype, a CSV that looks like a
Markdown table) still convert. Files are named ``original<extension>`` like the parse activity names them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docling.datamodel.base_models import InputFormat
from docling.exceptions import ConversionError

from engine_stub_support import OfficeStubEngine
from file_parsers import convert as convert_module
from file_parsers.convert import file_to_markdown
from file_parsers.engines.docling import ALLOWED_FORMATS, INPUT_FORMATS, DoclingEngine
from file_parsers.formats import FORMATS, DocumentFormat, FormatSpec
from file_parsers.limits import DocumentParseFailedError
from file_parsers.office.guard import inspect_file
from file_parsers.office.kind import check_main_parts, main_format
from office_support import build_docx, build_pptx, build_xlsx

TEXT_FORMATS = (DocumentFormat.MARKDOWN, DocumentFormat.HTML, DocumentFormat.CSV)
OOXML_SWAPS = [
    (actual, stored)
    for actual in (DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX)
    for stored in (DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX)
    if actual is not stored
]


def spec_for(document_format: DocumentFormat) -> FormatSpec:
    return next(spec for spec in FORMATS if spec.format is document_format)


def ascii_pdf(pages: int) -> str:
    """A valid PDF made of ASCII only (so it passes a UTF-8 check), with ``pages`` empty pages."""

    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"]
    objects += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n"


def original(tmp_path: Path, stored: DocumentFormat) -> Path:
    return tmp_path / f"original{spec_for(stored).extension}"


def build_ooxml(tmp_path: Path, actual: DocumentFormat) -> Path:
    source = tmp_path / f"source{spec_for(actual).extension}"
    if actual is DocumentFormat.DOCX:
        return build_docx(source)
    if actual is DocumentFormat.PPTX:
        return build_pptx(source, [("Title", ["bullet"])])
    return build_xlsx(source, [("Sheet", [["a", 1], ["b", 2]])])


def swapped(tmp_path: Path, actual: DocumentFormat, stored: DocumentFormat) -> Path:
    return Path(shutil.copy(build_ooxml(tmp_path, actual), original(tmp_path, stored)))


def pdf_as(tmp_path: Path, stored: DocumentFormat) -> Path:
    path = original(tmp_path, stored)
    path.write_text(ascii_pdf(3), encoding="ascii")
    return path


@pytest.fixture
def no_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the guard so the file reaches the real engine (defence in depth)."""

    monkeypatch.setattr(convert_module, "inspect_file", lambda *_: None)


# --- the guard refuses a mismatch before any engine runs ------------------------------------------


@pytest.mark.parametrize("stored", TEXT_FORMATS)
def test_the_guard_refuses_pdf_bytes_stored_as_a_text_format(stored: DocumentFormat, tmp_path: Path):
    with pytest.raises(DocumentParseFailedError, match="is a PDF"):
        inspect_file(pdf_as(tmp_path, stored), stored)


@pytest.mark.parametrize("stored", TEXT_FORMATS)
def test_the_guard_refuses_zip_bytes_stored_as_a_text_format(stored: DocumentFormat, tmp_path: Path):
    path = original(tmp_path, stored)
    path.write_bytes(build_docx(tmp_path / "s.docx").read_bytes())
    with pytest.raises(DocumentParseFailedError):  # a DOCX is not UTF-8 either; the signature is checked first
        inspect_file(path, stored)
    path.write_bytes(b"PK\x03\x04" + b"a,b\n1,2\n")  # the signature alone, followed by valid UTF-8
    with pytest.raises(DocumentParseFailedError, match="is a ZIP"):
        inspect_file(path, stored)


@pytest.mark.parametrize(("actual", "stored"), OOXML_SWAPS)
def test_the_guard_refuses_an_ooxml_package_of_another_format(
    actual: DocumentFormat, stored: DocumentFormat, tmp_path: Path
):
    with pytest.raises(DocumentParseFailedError, match=f"declares a {actual.value} main document"):
        inspect_file(swapped(tmp_path, actual, stored), stored)


@pytest.mark.parametrize("stored", TEXT_FORMATS)
def test_pdf_bytes_as_text_fail_through_the_facade_before_the_engine(stored: DocumentFormat, tmp_path: Path):
    engine = OfficeStubEngine()
    with pytest.raises(DocumentParseFailedError):
        file_to_markdown(pdf_as(tmp_path, stored), spec_for(stored).media_type, engine=engine)
    assert engine.office_calls == []
    assert engine.calls == []  # the PDF pipeline never ran either


@pytest.mark.parametrize(("actual", "stored"), OOXML_SWAPS)
def test_an_ooxml_swap_fails_through_the_facade_before_the_engine(
    actual: DocumentFormat, stored: DocumentFormat, tmp_path: Path
):
    engine = OfficeStubEngine()
    with pytest.raises(DocumentParseFailedError):
        file_to_markdown(swapped(tmp_path, actual, stored), spec_for(stored).media_type, engine=engine)
    assert engine.office_calls == []


# --- the engine allows only the stored format (the guard bypassed) ----------------------------------


@pytest.mark.parametrize("stored", TEXT_FORMATS)
@pytest.mark.usefixtures("no_guard")
def test_without_the_guard_docling_refuses_pdf_bytes_stored_as_text(stored: DocumentFormat, tmp_path: Path):
    path = pdf_as(tmp_path, stored)
    with pytest.raises(ConversionError, match=r"skipped|not allowed"):
        DoclingEngine(threads=1).extract_office(path, spec_for(stored).media_type)
    with pytest.raises(DocumentParseFailedError) as error:
        file_to_markdown(path, spec_for(stored).media_type, engine=DoclingEngine(threads=1))
    assert isinstance(error.value.__cause__, ConversionError)


@pytest.mark.parametrize(("actual", "stored"), OOXML_SWAPS)
@pytest.mark.usefixtures("no_guard")
def test_without_the_guard_docling_refuses_an_ooxml_swap(
    actual: DocumentFormat, stored: DocumentFormat, tmp_path: Path
):
    path = swapped(tmp_path, actual, stored)
    with pytest.raises(DocumentParseFailedError) as error:
        file_to_markdown(path, spec_for(stored).media_type, engine=DoclingEngine(threads=1))
    assert isinstance(error.value.__cause__, ConversionError)


def test_each_converter_allows_only_its_format_and_is_cached_per_format_and_images():
    engine = DoclingEngine(threads=1)
    assert set(INPUT_FORMATS) == set(DocumentFormat)
    for input_format in ALLOWED_FORMATS:
        converter = engine._converter(input_format)
        assert converter.allowed_formats == [input_format]
        assert engine._converter(input_format) is converter
    assert engine._converter(InputFormat.PDF, images=True) is not engine._converter(InputFormat.PDF)
    with pytest.raises(ValueError, match="not a format of the engine"):
        engine.build_converter(InputFormat.IMAGE)


# --- legitimate files still convert ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "text", "expected"),
    [
        (DocumentFormat.MARKDOWN, "# Notes\n\nSome <b>inline</b> HTML and <br/> a break.\n", "inline"),
        (DocumentFormat.MARKDOWN, "<html><body><p>Starts like HTML</p></body></html>\n\n# Then Markdown\n", "Markdown"),
        (DocumentFormat.MARKDOWN, "a,b,c\n1,2,3\n", "a,b,c"),  # sniffs as CSV
        (DocumentFormat.HTML, "<p>No doctype, no html element.</p><table><tr><td>cell</td></tr></table>", "cell"),
        (DocumentFormat.HTML, "# Not a heading\n\n<p>Markdown-looking HTML</p>", "Markdown-looking"),
        (DocumentFormat.CSV, "| name | value |\n|---|---|\n| alpha | 1 |\n", "alpha"),
        (DocumentFormat.CSV, "<b>head</b>,other\n<i>x</i>,y\n", "head"),
        (DocumentFormat.CSV, "%PDX,code\n1,2\n", "PDX"),  # a near-signature is not a signature
    ],
)
def test_legitimate_text_files_convert_whatever_docling_would_sniff(
    stored: DocumentFormat, text: str, expected: str, tmp_path: Path
):
    path = original(tmp_path, stored)
    path.write_text(text, encoding="utf-8")
    inspect_file(path, stored)
    assert expected in file_to_markdown(path, spec_for(stored).media_type).markdown


@pytest.mark.parametrize("stored", [DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX])
def test_legitimate_ooxml_files_pass_the_guard_and_convert(stored: DocumentFormat, tmp_path: Path):
    path = Path(shutil.copy(build_ooxml(tmp_path, stored), original(tmp_path, stored)))
    inspect_file(path, stored)
    assert file_to_markdown(path, spec_for(stored).media_type).markdown.strip()


# --- the content-type rule itself ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml", DocumentFormat.DOCX),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml", DocumentFormat.DOCX),
        ("application/vnd.ms-word.document.macroEnabled.main+xml", DocumentFormat.DOCX),
        ("application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml", DocumentFormat.PPTX),
        ("application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml", DocumentFormat.PPTX),
        ("Application/VND.openxmlformats-officedocument.spreadsheetml.sheet.main+xml; x=1", DocumentFormat.XLSX),
        ("application/vnd.ms-excel.sheet.macroEnabled.main+xml", DocumentFormat.XLSX),
        ("application/vnd.ms-excel.sheet.binary.macroEnabled.main", DocumentFormat.XLSX),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml", None),
        ("application/vnd.openxmlformats-officedocument.drawingml.chart+xml", None),
        ("application/vnd.openxmlformats-package.relationships+xml", None),
        ("application/xml", None),
        ("", None),
    ],
)
def test_main_format_names_only_main_document_types(content_type: str, expected: DocumentFormat | None):
    assert main_format(content_type) is expected


def test_types_that_are_not_main_documents_never_refuse_a_package():
    chart = {"PartName": "/word/charts/chart1.xml", "ContentType": "application/vnd.ms-office.chartex+xml"}
    embedded = {"Extension": "xlsx", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    check_main_parts([chart, embedded, {"Extension": "xml"}], DocumentFormat.DOCX)
    with pytest.raises(DocumentParseFailedError):  # a Default can name the main type too (openpyxl reads it)
        check_main_parts(
            [{"Extension": "xml", "ContentType": "application/vnd.ms-excel.sheet.macroEnabled.main+xml"}],
            DocumentFormat.DOCX,
        )


@pytest.mark.parametrize("stored", TEXT_FORMATS)
@pytest.mark.parametrize(("payload", "name"), [(b"%PDF-1.4\n", "PDF"), (b"PK\x03\x04a,b\n", "ZIP")], ids=["pdf", "zip"])
def test_the_guard_refuses_a_signature_behind_a_utf8_bom(
    stored: DocumentFormat, payload: bytes, name: str, tmp_path: Path
):
    path = original(tmp_path, stored)
    path.write_bytes(b"\xef\xbb\xbf" + payload + b"1,2\n")
    with pytest.raises(DocumentParseFailedError, match=f"is a {name}"):
        inspect_file(path, stored)


@pytest.mark.parametrize("stored", TEXT_FORMATS)
def test_a_utf8_bom_before_ordinary_text_passes_the_guard(stored: DocumentFormat, tmp_path: Path):
    path = original(tmp_path, stored)
    path.write_bytes(b"\xef\xbb\xbfname,value\nPK,1\n")
    inspect_file(path, stored)
