"""G12 (parser-level) and the limits of docs/file-parsers/limits.md for the Office and web formats.

No Docling here: ``file_parsers.office.guard`` is the code that reads a file first. Hostile fixtures are made in
the test (nothing large or dangerous is committed): a ZIP bomb whose headers lie about its sizes, an OOXML part
with an external-entity DTD, an XLSX whose dimension tag lies about its cells.
"""

from __future__ import annotations

import io
import struct
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from file_parsers import limits
from file_parsers.convert import file_to_markdown
from file_parsers.formats import DocumentFormat
from file_parsers.limits import DocumentParseFailedError, DocumentTooLargeError
from file_parsers.office import guard
from file_parsers.office.guard import inspect_file
from office_support import (
    DOCX,
    PPTX,
    build_csv,
    build_docx,
    build_pptx,
    build_xlsx,
    lie_about_sizes,
    minimal_deck,
    move_member,
    rewrite_member,
    single_entry_zip,
    with_dtd,
)

ZEROS = b"<a>" + b"x" * (4 * 1024 * 1024) + b"</a>"


def too_large(path: Path, document_format: DocumentFormat) -> DocumentTooLargeError:
    with pytest.raises(DocumentTooLargeError) as caught:
        inspect_file(path, document_format)
    return caught.value


def parse_failed(path: Path, document_format: DocumentFormat) -> str:
    with pytest.raises(DocumentParseFailedError) as caught:
        inspect_file(path, document_format)
    return str(caught.value)


def sheet_xml(cells: int, *, dimension: str = "A1:A1") -> bytes:
    """A worksheet with ``cells`` distinct cells whose dimension tag says ``dimension``."""

    rows = []
    for start in range(0, cells, 50):
        row = start // 50 + 1
        count = min(50, cells - start)
        rows.append(
            f'<row r="{row}">'
            + "".join(
                f'<c r="{chr(65 + i % 26)}{chr(65 + i // 26)}{row}"><v>{start + i}7</v></c>' for i in range(count)
            )
            + "</row>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetData>{"".join(rows)}</sheetData></worksheet>'
    ).encode()


def workbook_with_cells(tmp_path: Path, cells: int, name: str = "w.xlsx") -> Path:
    base = build_xlsx(tmp_path / f"base-{name}", [("S", [["a"]])])
    return rewrite_member(base, tmp_path / name, "xl/worksheets/sheet1.xml", lambda _: sheet_xml(cells))


# --- valid files pass ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX])
def test_a_normal_ooxml_file_passes(document_format: DocumentFormat, tmp_path: Path):
    builders = {
        DocumentFormat.DOCX: lambda: build_docx(tmp_path / "a.docx"),
        DocumentFormat.PPTX: lambda: build_pptx(tmp_path / "a.pptx", [("T", ["x"])]),
        DocumentFormat.XLSX: lambda: build_xlsx(tmp_path / "a.xlsx", [("S", [["a", "b"], [1, 2]])]),
    }
    inspect_file(builders[document_format](), document_format)


def test_a_pdf_is_not_the_guard_s_business(tmp_path: Path):
    with pytest.raises(ValueError, match="check_pdf_file"):
        inspect_file(tmp_path / "x.pdf", DocumentFormat.PDF)


# --- ZIP bombs: counted while inflating, never from the declared sizes ----------------------------------------


def test_a_ratio_bomb_with_a_false_declared_size_is_refused_by_the_ratio(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "bomb.docx", "word/document.xml", ZEROS)
    lie_about_sizes(archive, declared=100)  # the headers claim a 100 byte entry
    error = too_large(archive, DocumentFormat.DOCX)
    assert error.what == "compression ratio"
    assert error.limit == limits.MAX_OOXML_RATIO
    assert error.actual > limits.MAX_OOXML_RATIO


def test_a_ratio_bomb_with_honest_sizes_is_refused_the_same_way(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "bomb.xlsx", "xl/worksheets/sheet1.xml", ZEROS)
    assert too_large(archive, DocumentFormat.XLSX).what == "compression ratio"


def test_a_small_highly_repetitive_entry_is_below_the_ratio_floor(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "ok.docx", "word/document.xml", b"<a/>" * 100_000)  # 400 KB
    inspect_file(archive, DocumentFormat.DOCX)


def test_total_inflated_bytes_over_the_cap_are_refused_while_reading_with_flat_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The cap is lowered to 8 MB and the ratio check relaxed so that the byte count alone decides: a 64 MB
    # entry (about 64 KB compressed) must stop at the cap, reading a few chunks, not 64 MB.
    monkeypatch.setattr(guard, "MAX_OOXML_INFLATED_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(guard, "MAX_OOXML_RATIO", 10**9)
    archive = single_entry_zip(tmp_path / "big.docx", "word/media/blob.bin", b"\x00" * (64 * 1024 * 1024))
    lie_about_sizes(archive, declared=1000)
    tracemalloc.start()
    try:
        error = too_large(archive, DocumentFormat.DOCX)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.what == "inflated bytes"
    assert error.actual <= 8 * 1024 * 1024 + 2 * guard.CHUNK
    assert peak < 2 * 1024 * 1024


def test_the_inflated_total_counts_across_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guard, "MAX_OOXML_INFLATED_BYTES", 300_000)
    monkeypatch.setattr(guard, "MAX_OOXML_RATIO", 10**9)
    path = tmp_path / "two.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/a.bin", b"\x00" * 200_000)
        archive.writestr("word/b.bin", b"\x01" * 200_000)
    assert too_large(path, DocumentFormat.DOCX).what == "inflated bytes"


def test_declared_sizes_that_lie_without_breaking_a_limit_make_the_file_malformed(tmp_path: Path):
    archive = single_entry_zip(
        tmp_path / "liar.docx", "word/document.xml", b"<w:document>" + b"x" * 2000 + b"</w:document>"
    )
    lie_about_sizes(archive, declared=10)
    assert "declared size" in parse_failed(archive, DocumentFormat.DOCX)


def test_an_archive_with_too_many_entries_is_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guard, "MAX_OOXML_ENTRIES", 5)
    path = tmp_path / "many.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(6):
            archive.writestr(f"word/part{index}.xml", "<a/>")
    assert too_large(path, DocumentFormat.DOCX).what == "archive entries"


@pytest.mark.parametrize("content", [b"", b"not a zip at all", b"PK\x03\x04" + b"\x00" * 10])
def test_a_file_that_is_not_a_zip_archive_is_malformed(content: bytes, tmp_path: Path):
    path = tmp_path / "bad.docx"
    path.write_bytes(content)
    assert "ZIP" in parse_failed(path, DocumentFormat.DOCX)


def test_a_truncated_archive_is_malformed(tmp_path: Path):
    good = build_docx(tmp_path / "good.docx")
    (tmp_path / "cut.docx").write_bytes(good.read_bytes()[:-100])
    parse_failed(tmp_path / "cut.docx", DocumentFormat.DOCX)


def test_a_corrupt_deflate_stream_is_malformed(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "x.docx", "word/document.xml", b"<a>" + b"hello world " * 400 + b"</a>")
    data = bytearray(archive.read_bytes())
    for index in range(60, 90):
        data[index] ^= 0xFF
    archive.write_bytes(bytes(data))
    parse_failed(archive, DocumentFormat.DOCX)


def test_a_stored_entry_is_inflated_as_is_and_checked(tmp_path: Path):
    inspect_file(
        single_entry_zip(tmp_path / "s.docx", "word/document.xml", b"<a/>", deflate=False), DocumentFormat.DOCX
    )
    cut = single_entry_zip(tmp_path / "c.docx", "word/document.xml", b"<a>" + b"y" * 5000 + b"</a>", deflate=False)
    data = bytearray(cut.read_bytes())
    struct.pack_into("<L", data, data.index(b"PK\x01\x02") + 20, 10**6)  # directory claims far more bytes than exist
    cut.write_bytes(bytes(data))
    assert "truncated" in parse_failed(cut, DocumentFormat.DOCX)


def test_an_encrypted_entry_is_refused(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "enc.docx", "word/document.xml", b"<a/>")
    data = bytearray(archive.read_bytes())
    data[6] |= 0x1  # local header general purpose flag
    data[data.index(b"PK\x01\x02") + 8] |= 0x1  # central directory flag
    archive.write_bytes(bytes(data))
    assert "encrypted" in parse_failed(archive, DocumentFormat.DOCX)


def test_an_unsupported_compression_method_is_refused(tmp_path: Path):
    path = tmp_path / "bz.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as archive:
        archive.writestr("word/document.xml", "<a/>")
    assert "compression" in parse_failed(path, DocumentFormat.DOCX)


def test_a_bad_local_header_is_refused(tmp_path: Path):
    archive = single_entry_zip(tmp_path / "h.docx", "word/document.xml", b"<a/>")
    data = bytearray(archive.read_bytes())
    data[0:4] = b"XXXX"
    archive.write_bytes(bytes(data))
    parse_failed(archive, DocumentFormat.DOCX)


# --- XML: no DTD, so no external entity ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document_format", "member"),
    [
        (DocumentFormat.DOCX, "word/document.xml"),
        (DocumentFormat.PPTX, "ppt/slides/slide1.xml"),
        (DocumentFormat.XLSX, "xl/worksheets/sheet1.xml"),
        (DocumentFormat.DOCX, "[Content_Types].xml"),
        (DocumentFormat.DOCX, "_rels/.rels"),
    ],
)
def test_an_external_entity_dtd_in_any_xml_part_is_refused(
    document_format: DocumentFormat, member: str, tmp_path: Path
):
    sources = {
        DocumentFormat.DOCX: build_docx(tmp_path / "s.docx"),
        DocumentFormat.PPTX: build_pptx(tmp_path / "s.pptx", [("T", ["x"])]),
        DocumentFormat.XLSX: build_xlsx(tmp_path / "s.xlsx", [("S", [["a"]])]),
    }
    canary = tmp_path / "canary.txt"
    canary.write_text("TOPSECRET", encoding="utf-8")
    hostile = with_dtd(sources[document_format], tmp_path / f"x.{document_format.value}", member, canary)
    assert "DTD" in parse_failed(hostile, document_format)


def test_a_dtd_split_across_two_chunks_is_still_found(tmp_path: Path):
    payload = b"<a>" + b" " * (guard.CHUNK - 5) + b"<!DOCTYPE a []>" + b"</a>"
    archive = single_entry_zip(tmp_path / "split.docx", "word/document.xml", payload, deflate=False)
    assert "DTD" in parse_failed(archive, DocumentFormat.DOCX)


def test_a_dtd_marker_inside_plain_text_is_not_an_xml_dtd(tmp_path: Path):
    plain = single_entry_zip(tmp_path / "m.docx", "word/media/note.txt", b"see <!DOCTYPE html> in the text")
    inspect_file(plain, DocumentFormat.DOCX)  # does not start like XML: ignored


CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
ILLUSTRATOR_SVG = (
    b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
    b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd" [<!ENTITY ns_svg "http://www.w3.org/2000/svg">]>\n'
    b'<svg xmlns="&ns_svg;" width="10" height="10"><rect width="10" height="10"/></svg>'
)
HOSTILE = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'


def package(path: Path, entries: dict[str, bytes], types: str = "") -> Path:
    """A ZIP with a ``[Content_Types].xml`` carrying ``types`` (Default/Override elements) and the entries."""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", f'<Types xmlns="{CONTENT_TYPES_NS}">{types}</Types>')
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def real_package(tmp_path: Path, document_format: DocumentFormat, entries: dict[str, bytes], types: str) -> Path:
    """A real DOCX or PPTX (so it has its main part) with extra content types and entries added."""

    base = (
        build_docx(tmp_path / "base.docx")
        if document_format is DocumentFormat.DOCX
        else build_pptx(tmp_path / "base.pptx", [("T", ["x"])])
    )
    target = tmp_path / f"real.{document_format.value}"
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", types.encode() + b"</Types>")
            out.writestr(info.filename, data)
        for name, payload in entries.items():
            out.writestr(name, payload)
    return target


@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.PPTX])
@pytest.mark.parametrize(
    "types",
    [
        '<Default Extension="svg" ContentType="image/svg+xml"/>',
        '<Override PartName="/word/media/image1.svg" ContentType="image/svg+xml"/>',
        '<Default Extension="svg" ContentType="IMAGE/SVG+XML; charset=utf-8"/>',
    ],
)
def test_an_svg_image_with_an_illustrator_doctype_is_a_binary_image_in_docx_and_pptx(
    document_format: DocumentFormat, types: str, tmp_path: Path
):
    inspect_file(
        real_package(tmp_path, document_format, {"word/media/image1.svg": ILLUSTRATOR_SVG}, types), document_format
    )


@pytest.mark.parametrize(
    ("kind", "extension", "document_format"),
    [("docx", "word", DocumentFormat.DOCX), ("pptx", "ppt", DocumentFormat.PPTX)],
)
def test_a_real_document_with_an_illustrator_svg_still_converts(
    kind: str, extension: str, document_format: DocumentFormat, tmp_path: Path
):
    builders = {
        "docx": (build_docx(tmp_path / "s.docx"), DOCX),
        "pptx": (build_pptx(tmp_path / "s.pptx", [("Title", ["bullet"])]), PPTX),
    }
    base, media_type = builders[kind]
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(tmp_path / f"svg.{kind}", "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"<Default ", b'<Default Extension="svg" ContentType="image/svg+xml"/><Default ', 1)
            out.writestr(info.filename, data)
        out.writestr(f"{extension}/media/image1.svg", ILLUSTRATOR_SVG)
    assert document_format.value == kind
    assert file_to_markdown(tmp_path / f"svg.{kind}", media_type).markdown.strip()


@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.PPTX])
@pytest.mark.parametrize(
    ("name", "types"),
    [
        ("word/document.xml", ""),
        ("ppt/slides/slide1.xml", ""),
        ("_rels/.rels", ""),
        ("word/odd", '<Override PartName="/word/odd" ContentType="application/xml"/>'),
        ("word/odd.dat", '<Default Extension="dat" ContentType="application/xml"/>'),
        ("word/odd", '<Override PartName="/word/odd" ContentType="application/vnd.custom+xml"/>'),
        ("word/odd.svg", '<Default Extension="svg" ContentType="text/xml"/>'),
    ],
)
def test_a_doctype_in_an_entry_the_libraries_parse_as_xml_is_refused(
    document_format: DocumentFormat, name: str, types: str, tmp_path: Path
):
    path = package(tmp_path / f"x.{document_format.value}", {name: HOSTILE}, types)
    assert "DTD" in parse_failed(path, document_format)


@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.PPTX])
def test_a_doctype_in_an_entry_that_is_not_declared_xml_is_not_checked_in_docx_and_pptx(
    document_format: DocumentFormat, tmp_path: Path
):
    entries = {"word/odd": HOSTILE, "word/embed/page.html": HOSTILE, "word/media/note.txt": HOSTILE}
    path = real_package(tmp_path, document_format, entries, '<Default Extension="txt" ContentType="text/plain"/>')
    inspect_file(path, document_format)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_xml_in_a_part_declared_xml_is_refused(encoding: str, tmp_path: Path):
    payload = '<?xml version="1.0"?><a/>'.encode(encoding)
    path = package(
        tmp_path / "u.docx", {"word/odd": payload}, '<Override PartName="/word/odd" ContentType="application/xml"/>'
    )
    assert "UTF-8" in parse_failed(path, DocumentFormat.DOCX)


@pytest.mark.parametrize("name", ["word/media/note.txt", "xl/s.dat", "xl/odd", "xl/embed/page.html"])
def test_xlsx_refuses_a_doctype_in_any_entry_that_looks_like_xml_whatever_its_name(name: str, tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    assert "DTD" in parse_failed(with_entry(base, tmp_path / "x.xlsx", name, HOSTILE), DocumentFormat.XLSX)
    padded = with_entry(base, tmp_path / "y.xlsx", name, b"\xef\xbb\xbf  \n" + HOSTILE)
    assert "DTD" in parse_failed(padded, DocumentFormat.XLSX)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_xlsx_refuses_utf16_xml_under_any_name(encoding: str, tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    path = with_entry(base, tmp_path / "u.xlsx", "xl/s.dat", '<?xml version="1.0"?><a/>'.encode(encoding))
    assert "UTF-8" in parse_failed(path, DocumentFormat.XLSX)


def test_xlsx_still_treats_an_svg_with_a_doctype_as_refused(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    assert "DTD" in parse_failed(
        with_entry(base, tmp_path / "svg.xlsx", "xl/media/i.svg", ILLUSTRATOR_SVG), DocumentFormat.XLSX
    )


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_an_xml_part_in_utf16_is_refused_because_a_dtd_could_hide_in_it(encoding: str, tmp_path: Path):
    payload = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'.encode(encoding)
    archive = single_entry_zip(tmp_path / "u16.docx", "word/document.xml", payload)
    assert "UTF-8" in parse_failed(archive, DocumentFormat.DOCX)


# --- XLSX cells: counted while reading, not from the dimension tag -----------------------------------------------


def test_a_workbook_at_the_cell_cap_passes_and_one_cell_over_is_refused(tmp_path: Path):
    inspect_file(workbook_with_cells(tmp_path, limits.MAX_CELLS, "at.xlsx"), DocumentFormat.XLSX)
    error = too_large(workbook_with_cells(tmp_path, limits.MAX_CELLS + 1, "over.xlsx"), DocumentFormat.XLSX)
    assert (error.what, error.limit) == ("cells", limits.MAX_CELLS)


def test_a_false_dimension_tag_does_not_hide_the_cells(tmp_path: Path):
    path = workbook_with_cells(tmp_path, 1000, "dim.xlsx")  # the tag says A1:A1
    assert b'ref="A1:A1"' in zipfile.ZipFile(path).read("xl/worksheets/sheet1.xml")
    inspect_file(path, DocumentFormat.XLSX)  # 1000 real cells are fine ...
    assert (
        too_large(workbook_with_cells(tmp_path, limits.MAX_CELLS + 5, "dim2.xlsx"), DocumentFormat.XLSX).what == "cells"
    )


def test_a_huge_dimension_tag_over_few_cells_is_not_refused(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    path = rewrite_member(
        base, tmp_path / "huge.xlsx", "xl/worksheets/sheet1.xml", lambda _: sheet_xml(3, dimension="A1:XFD1048576")
    )
    inspect_file(path, DocumentFormat.XLSX)


def test_cells_count_across_sheets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "MAX_CELLS", 150)
    base = build_xlsx(tmp_path / "two.xlsx", [("A", [["a"]]), ("B", [["b"]])])
    first = rewrite_member(base, tmp_path / "step.xlsx", "xl/worksheets/sheet1.xml", lambda _: sheet_xml(100))
    both = rewrite_member(first, tmp_path / "both.xlsx", "xl/worksheets/sheet2.xml", lambda _: sheet_xml(100))
    assert too_large(both, DocumentFormat.XLSX).what == "cells"


def test_a_sheet_that_is_not_xml_is_malformed(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    path = rewrite_member(
        base, tmp_path / "junk.xlsx", "xl/worksheets/sheet1.xml", lambda _: b"<worksheet><sheetData><c></worksheet>"
    )
    assert "not valid" in parse_failed(path, DocumentFormat.XLSX)


# --- PPTX slides --------------------------------------------------------------------------------------------


def test_five_hundred_slides_pass_and_five_hundred_and_one_are_too_large(tmp_path: Path):
    inspect_file(minimal_deck(tmp_path / "d500.pptx", limits.MAX_SLIDES), DocumentFormat.PPTX)
    error = too_large(minimal_deck(tmp_path / "d501.pptx", limits.MAX_SLIDES + 1), DocumentFormat.PPTX)
    assert (error.what, error.actual, error.limit) == ("slides", 501, 500)


def test_slides_stored_outside_ppt_slides_are_counted_through_the_relationships(tmp_path: Path):
    deck = minimal_deck(tmp_path / "moved.pptx", limits.MAX_SLIDES + 1, folder="elsewhere/deep")
    assert not [n for n in zipfile.ZipFile(deck).namelist() if n.startswith("ppt/slides/")]
    assert too_large(deck, DocumentFormat.PPTX).what == "slides"


def test_a_deck_that_lists_the_same_slide_part_again_and_again_is_counted_by_its_entries(tmp_path: Path):
    deck = minimal_deck(tmp_path / "reuse.pptx", 2, listed=limits.MAX_SLIDES + 1)
    assert too_large(deck, DocumentFormat.PPTX).actual == limits.MAX_SLIDES + 1


def test_a_real_deck_with_its_default_paths_passes(tmp_path: Path):
    inspect_file(build_pptx(tmp_path / "real.pptx", [("A", ["x"]), ("B", ["y"])]), DocumentFormat.PPTX)


def test_a_package_without_a_main_part_is_malformed(tmp_path: Path):
    path = single_entry_zip(tmp_path / "nomain.pptx", "ppt/slides/slide1.xml", b"<p:sld/>")
    assert "main document part" in parse_failed(path, DocumentFormat.PPTX)


def test_a_main_part_that_the_relationships_name_but_the_archive_lacks_is_malformed(tmp_path: Path):
    deck = minimal_deck(tmp_path / "gone.pptx", 1)
    with zipfile.ZipFile(deck) as source, zipfile.ZipFile(tmp_path / "gone2.pptx", "w") as out:
        for info in source.infolist():
            if info.filename != "ppt/presentation.xml":
                out.writestr(info.filename, source.read(info.filename))
    assert "main document part" in parse_failed(tmp_path / "gone2.pptx", DocumentFormat.PPTX)


def test_invalid_relationship_xml_is_malformed(tmp_path: Path):
    deck = minimal_deck(tmp_path / "badrels.pptx", 1)
    bad = rewrite_member(deck, tmp_path / "badrels2.pptx", "_rels/.rels", lambda _: b"<Relationships><Relationship")
    assert "not valid XML" in parse_failed(bad, DocumentFormat.PPTX)


def test_external_relationships_are_not_followed(tmp_path: Path):
    deck = minimal_deck(tmp_path / "ext.pptx", 1)

    def make_external(data: bytes) -> bytes:
        return data.replace(b"<Relationship ", b'<Relationship TargetMode="External" ', 1)

    external = rewrite_member(deck, tmp_path / "ext2.pptx", "_rels/.rels", make_external)
    assert "main document part" in parse_failed(external, DocumentFormat.PPTX)


# --- sheets stored anywhere are counted through the workbook relationships -------------------------------------


def moved_workbook(tmp_path: Path, cells: int, name: str) -> Path:
    base = workbook_with_cells(tmp_path, cells, f"orig-{name}")
    return move_member(base, tmp_path / name, "xl/worksheets/sheet1.xml", "xl/other/sheet1.xml")


def test_a_sheet_stored_outside_xl_worksheets_is_counted(tmp_path: Path):
    path = moved_workbook(tmp_path, limits.MAX_CELLS + 1, "moved.xlsx")
    names = zipfile.ZipFile(path).namelist()
    assert "xl/other/sheet1.xml" in names and "xl/worksheets/sheet1.xml" not in names
    assert too_large(path, DocumentFormat.XLSX).what == "cells"


def test_the_moved_sheet_is_the_one_openpyxl_reads(tmp_path: Path):
    path = moved_workbook(tmp_path, 3, "small.xlsx")
    workbook = load_workbook(path)
    assert sum(1 for row in workbook.worksheets[0].iter_rows() for cell in row if cell.value is not None) == 3
    inspect_file(path, DocumentFormat.XLSX)


def test_a_sheet_listed_several_times_is_counted_each_time(tmp_path: Path):
    base = workbook_with_cells(tmp_path, limits.MAX_CELLS // 2 + 1, "once.xlsx")

    def list_thrice(data: bytes) -> bytes:
        text = data.decode()
        start = text.index("<sheet ")
        end = text.index("/>", start) + 2
        element = text[start:end]
        return (
            text[:end]
            + element.replace('name="S"', 'name="S2"')
            + element.replace('name="S"', 'name="S3"')
            + text[end:]
        ).encode()

    path = rewrite_member(base, tmp_path / "thrice.xlsx", "xl/workbook.xml", list_thrice)
    assert too_large(path, DocumentFormat.XLSX).what == "cells"


def test_a_sheet_part_that_no_relationship_reaches_still_counts_the_count_is_an_upper_bound(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    orphan = rewrite_member(
        base, tmp_path / "orphan.xlsx", "xl/worksheets/sheet1.xml", lambda _: sheet_xml(limits.MAX_CELLS + 1)
    )

    def unlink(data: bytes) -> bytes:
        return data.replace(b"worksheets/sheet1.xml", b"worksheets/none.xml")

    unlinked = rewrite_member(orphan, tmp_path / "orphan2.xlsx", "xl/_rels/workbook.xml.rels", unlink)
    assert too_large(unlinked, DocumentFormat.XLSX).what == "cells"


def loaded_cells(path: Path) -> int:
    workbook = load_workbook(path)
    return sum(
        1 for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row if cell.value is not None
    )


def decoy_workbook(tmp_path: Path, cells: int, name: str) -> Path:
    """Root relationships point at an empty decoy; ``[Content_Types].xml`` still marks the real workbook part."""

    base = workbook_with_cells(tmp_path, cells, f"base-{name}")
    redirected = rewrite_member(
        base,
        tmp_path / f"redirected-{name}",
        "_rels/.rels",
        lambda data: data.replace(b"xl/workbook.xml", b"xl/decoy.xml"),
    )
    with zipfile.ZipFile(redirected) as source, zipfile.ZipFile(tmp_path / name, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            out.writestr(info.filename, source.read(info.filename))
        out.writestr("xl/decoy.xml", "")
    return tmp_path / name


def test_a_decoy_main_part_in_the_root_relationships_does_not_hide_the_workbook(tmp_path: Path):
    assert loaded_cells(decoy_workbook(tmp_path, 3, "small.xlsx")) == 3  # openpyxl finds it through the content types
    assert too_large(decoy_workbook(tmp_path, limits.MAX_CELLS + 1, "big.xlsx"), DocumentFormat.XLSX).what == "cells"


@pytest.mark.parametrize("kind", ["dialogsheet", "macrosheet", "foo"])
def test_a_sheet_relationship_of_another_type_is_still_counted(kind: str, tmp_path: Path):
    def retype(data: bytes) -> bytes:
        return data.replace(b'/relationships/worksheet"', f'/relationships/{kind}"'.encode())

    def build(cells: int, name: str) -> Path:
        base = workbook_with_cells(tmp_path, cells, f"base-{name}")
        return rewrite_member(base, tmp_path / name, "xl/_rels/workbook.xml.rels", retype)

    assert loaded_cells(build(3, "small.xlsx")) == 3  # openpyxl follows any relationship type
    assert too_large(build(limits.MAX_CELLS + 1, "big.xlsx"), DocumentFormat.XLSX).what == "cells"


def test_a_part_listed_several_times_by_a_workbook_found_only_through_the_content_types_is_multiplied(tmp_path: Path):
    base = workbook_with_cells(tmp_path, limits.MAX_CELLS // 2 + 1, "once.xlsx")

    def list_thrice(data: bytes) -> bytes:
        text = data.decode()
        start = text.index("<sheet ")
        end = text.index("/>", start) + 2
        element = text[start:end]
        return (text[:end] + element.replace('name="S"', 'name="S2"') * 2 + text[end:]).encode()

    listed = rewrite_member(base, tmp_path / "thrice.xlsx", "xl/workbook.xml", list_thrice)
    decoy = rewrite_member(
        listed,
        tmp_path / "thrice-decoy.xlsx",
        "_rels/.rels",
        lambda data: data.replace(b"xl/workbook.xml", b"xl/decoy.xml"),
    )
    assert too_large(decoy, DocumentFormat.XLSX).what == "cells"


@pytest.mark.parametrize("target", ["s.dat", "s", "s.xml.bak", "deep/dir/sheet.bin"])
def test_a_sheet_stored_under_any_name_is_counted(target: str, tmp_path: Path):
    def build(cells: int, name: str) -> Path:
        base = workbook_with_cells(tmp_path, cells, f"base-{name}")
        renamed = move_member(base, tmp_path / f"renamed-{name}", "xl/worksheets/sheet1.xml", f"xl/{target}")
        assert f"xl/{target}" in zipfile.ZipFile(renamed).namelist()
        return renamed

    assert loaded_cells(build(3, "small.xlsx")) == 3  # openpyxl opens whatever path the relationship names
    assert too_large(build(limits.MAX_CELLS + 1, "big.xlsx"), DocumentFormat.XLSX).what == "cells"


def test_a_sheet_with_a_bom_and_leading_whitespace_before_its_xml_is_still_recognised(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    padded = rewrite_member(
        base,
        tmp_path / "padded.xlsx",
        "xl/worksheets/sheet1.xml",
        lambda _: b"\xef\xbb\xbf \r\n" + sheet_xml(limits.MAX_CELLS + 1).split(b"?>", 1)[1],  # no declaration
    )
    assert too_large(padded, DocumentFormat.XLSX).what == "cells"


def with_entry(base: Path, target: Path, name: str, payload: bytes) -> Path:
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            out.writestr(info.filename, source.read(info.filename))
        out.writestr(name, payload)
    return target


def test_entries_that_do_not_look_like_xml_are_skipped_without_failing(tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    path = with_entry(base, tmp_path / "png.xlsx", "xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    path = with_entry(path, tmp_path / "bin.xlsx", "xl/blob.bin", b"\x00\x01\x02 <!DOCTYPE inside binary")
    path = with_entry(path, tmp_path / "empty.xlsx", "xl/empty.dat", b"")
    inspect_file(path, DocumentFormat.XLSX)


def test_an_svg_image_entry_is_valid_xml_and_passes(tmp_path: Path):
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    inspect_file(with_entry(base, tmp_path / "svg.xlsx", "xl/media/image1.svg", svg), DocumentFormat.XLSX)


def test_a_doctype_inside_a_sheet_stored_as_xl_s_dat_is_refused(tmp_path: Path):
    # An internal DOCTYPE (no external entity) makes the safe parser refuse; it must not be skipped uncounted.
    sheet = b"<!DOCTYPE worksheet [<!ENTITY e 'x'>]>" + sheet_xml(10).split(b"?>", 1)[1]
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    moved = move_member(base, tmp_path / "m.xlsx", "xl/worksheets/sheet1.xml", "xl/s.dat")
    hostile = rewrite_member(moved, tmp_path / "hostile.xlsx", "xl/s.dat", lambda _: sheet)
    assert "DTD" in parse_failed(hostile, DocumentFormat.XLSX)


@pytest.mark.parametrize("payload", [b"<worksheet><sheetData><c></worksheet>", b"<not xml at all <<<", b"<a><b></a>"])
def test_an_unparseable_entry_that_starts_with_a_bracket_is_refused_not_skipped(payload: bytes, tmp_path: Path):
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    moved = move_member(base, tmp_path / "m.xlsx", "xl/worksheets/sheet1.xml", "xl/s.dat")
    hostile = rewrite_member(moved, tmp_path / "hostile.xlsx", "xl/s.dat", lambda _: payload)
    assert "not valid XML" in parse_failed(hostile, DocumentFormat.XLSX)
    named = with_entry(base, tmp_path / "named.xlsx", "xl/blob.bin", payload)
    assert "not valid XML" in parse_failed(named, DocumentFormat.XLSX)


def test_cells_written_with_a_namespace_prefix_are_counted(tmp_path: Path):
    prefixed = (
        b'<?xml version="1.0"?><x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<x:sheetData>"
        + b"".join(b'<x:row r="%d"><x:c r="A%d"><x:v>1</x:v></x:c></x:row>' % (n, n) for n in range(1, 61))
        + b"</x:sheetData></x:worksheet>"
    )
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    path = rewrite_member(base, tmp_path / "prefixed.xlsx", "xl/worksheets/sheet1.xml", lambda _: prefixed)
    assert loaded_cells(path) == 60  # openpyxl reads prefixed SpreadsheetML
    inspect_file(path, DocumentFormat.XLSX)  # 60 cells pass at the default cap
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(limits, "MAX_CELLS", 59)
        assert too_large(path, DocumentFormat.XLSX).actual == 60


def test_cells_in_a_chart_namespace_do_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(limits, "MAX_CELLS", 50)
    chart = (
        b'<c:chartSpace xmlns:c="urn:chart">' + b"".join(b"<c:c>%d</c:c>" % n for n in range(100)) + b"</c:chartSpace>"
    )
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(tmp_path / "chart.xlsx", "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            out.writestr(info.filename, source.read(info.filename))
        out.writestr("xl/charts/chart1.xml", chart)
    inspect_file(tmp_path / "chart.xlsx", DocumentFormat.XLSX)


# --- text formats --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "document_format"),
    [("a.html", DocumentFormat.HTML), ("a.md", DocumentFormat.MARKDOWN), ("a.csv", DocumentFormat.CSV)],
)
def test_text_that_is_not_utf8_is_refused(name: str, document_format: DocumentFormat, tmp_path: Path):
    path = tmp_path / name
    path.write_bytes("a,b\ncafé,2\n".encode("latin-1"))
    assert "UTF-8" in parse_failed(path, document_format)


def test_text_cut_in_the_middle_of_a_character_is_refused(tmp_path: Path):
    path = tmp_path / "cut.md"
    path.write_bytes("# Titel\n\nschön".encode()[:-2])
    parse_failed(path, DocumentFormat.MARKDOWN)


def test_utf8_text_with_multibyte_characters_across_chunk_edges_passes(tmp_path: Path):
    path = tmp_path / "wide.md"
    path.write_text("é" * (guard.CHUNK + 7), encoding="utf-8")
    inspect_file(path, DocumentFormat.MARKDOWN)


def test_a_csv_at_the_cell_cap_passes_and_one_over_is_refused(tmp_path: Path):
    inspect_file(build_csv(tmp_path / "at.csv", "\n".join(["1,2,3,4"] * (limits.MAX_CELLS // 4))), DocumentFormat.CSV)
    error = too_large(
        build_csv(tmp_path / "over.csv", "\n".join(["1,2,3,4"] * (limits.MAX_CELLS // 4) + ["5"])), DocumentFormat.CSV
    )
    assert (error.what, error.limit) == ("cells", limits.MAX_CELLS)


def test_a_csv_cell_over_the_csv_module_field_limit_is_malformed(tmp_path: Path):
    parse_failed(build_csv(tmp_path / "wide.csv", "a,b\n" + "x" * 200_000 + ",2\n"), DocumentFormat.CSV)


def test_an_empty_csv_and_a_csv_with_quotes_and_newlines_pass(tmp_path: Path):
    inspect_file(build_csv(tmp_path / "empty.csv", ""), DocumentFormat.CSV)
    inspect_file(build_csv(tmp_path / "q.csv", 'a,b\n"x, ""y""","l1\nl2"\n'), DocumentFormat.CSV)


def delimited(delimiter: str, rows: int, columns: int) -> str:
    return "\n".join(delimiter.join(f"c{c}" for c in range(columns)) for _ in range(rows))


@pytest.mark.parametrize("delimiter", [";", "\t", "|", ":"])
def test_csv_cells_are_counted_with_the_delimiter_docling_detects(delimiter: str, tmp_path: Path):
    # 10 rows of 20,001 fields: 10 cells for a comma reader, 200,010 for the real delimiter.
    over = build_csv(tmp_path / "over.csv", delimited(delimiter, 10, 20_001))
    assert too_large(over, DocumentFormat.CSV).what == "cells"
    at_cap = build_csv(tmp_path / "at.csv", delimited(delimiter, 10, 20_000))  # exactly 200,000
    inspect_file(at_cap, DocumentFormat.CSV)
    build_csv(tmp_path / "plus.csv", delimited(delimiter, 10, 20_000) + f"{delimiter}x")
    assert too_large(tmp_path / "plus.csv", DocumentFormat.CSV).actual == limits.MAX_CELLS + 1


def test_the_guard_counts_the_same_cells_docling_parses_for_a_semicolon_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = build_csv(tmp_path / "semi.csv", "a;b;c\n1;2;3\n4;5;6\n")
    markdown = file_to_markdown(path, "text/csv").markdown
    assert markdown.startswith("| a | b | c |\n| - | - | - |\n| 1 | 2 | 3 |")
    monkeypatch.setattr(limits, "MAX_CELLS", 9)
    inspect_file(path, DocumentFormat.CSV)  # 9 cells: at the cap
    monkeypatch.setattr(limits, "MAX_CELLS", 8)
    assert too_large(path, DocumentFormat.CSV).actual == 9


def test_a_csv_with_a_bom_and_blank_lines_before_the_header_is_counted_like_docling(tmp_path: Path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf\n\na;b\n1;2\n")
    inspect_file(path, DocumentFormat.CSV)


def test_an_unquoted_csv_that_cannot_be_read_strictly_is_malformed(tmp_path: Path):
    parse_failed(build_csv(tmp_path / "bad.csv", 'a,b\n"unterminated,2\n3,"x"y\n'), DocumentFormat.CSV)


# --- input byte caps (the Drive import has no upload check) -----------------------------------------------------


def sparse(path: Path, size: int) -> Path:
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


@pytest.mark.parametrize("name", ["a.docx", "a.pptx", "a.xlsx"])
def test_an_ooxml_file_over_50_mib_is_too_large_without_being_read(name: str, tmp_path: Path):
    document_format = DocumentFormat(name.split(".")[1])
    error = too_large(sparse(tmp_path / name, 50 * 1024 * 1024 + 1), document_format)
    assert (error.what, error.limit) == ("file bytes", 50 * 1024 * 1024)


@pytest.mark.parametrize(
    ("name", "document_format"),
    [("a.html", DocumentFormat.HTML), ("a.md", DocumentFormat.MARKDOWN), ("a.csv", DocumentFormat.CSV)],
)
def test_a_text_file_over_10_mib_is_too_large_without_being_read(
    name: str, document_format: DocumentFormat, tmp_path: Path
):
    error = too_large(sparse(tmp_path / name, 10 * 1024 * 1024 + 1), document_format)
    assert (error.what, error.limit) == ("file bytes", 10 * 1024 * 1024)


def test_a_file_exactly_at_the_byte_cap_is_not_refused_for_its_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guard, "MAX_TEXT_FILE_BYTES", 12)
    inspect_file(build_csv(tmp_path / "ok.csv", "a," * 5 + "b"), DocumentFormat.CSV)  # 11 bytes
    inspect_file(build_csv(tmp_path / "at.csv", "a," * 6), DocumentFormat.CSV)  # 12 bytes
    assert too_large(build_csv(tmp_path / "big.csv", "a," * 6 + "b"), DocumentFormat.CSV).what == "file bytes"  # 13


# --- unreadable files ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.HTML, DocumentFormat.CSV])
def test_a_directory_or_a_missing_path_is_a_parse_failure_not_an_oserror(
    document_format: DocumentFormat, tmp_path: Path
):
    assert "cannot read" in parse_failed(tmp_path, document_format)
    assert "cannot read" in parse_failed(tmp_path / "missing", document_format)


# --- ZIP shapes: duplicate names and data descriptors -------------------------------------------------------


def zip_with_duplicates(path: Path, first: bytes, second: bytes, name: str = "word/document.xml") -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, first)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(name, second)
    return path


def test_a_duplicate_entry_name_hides_nothing_every_copy_is_scanned(tmp_path: Path):
    hostile = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/hostname">]><a>&x;</a>'
    # The first copy is the one a "last entry wins" reader never opens.
    assert "DTD" in parse_failed(zip_with_duplicates(tmp_path / "d1.docx", hostile, b"<a/>"), DocumentFormat.DOCX)
    assert "DTD" in parse_failed(zip_with_duplicates(tmp_path / "d2.docx", b"<a/>", hostile), DocumentFormat.DOCX)


def test_duplicate_entries_add_up_in_the_inflated_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guard, "MAX_OOXML_INFLATED_BYTES", 300_000)
    monkeypatch.setattr(guard, "MAX_OOXML_RATIO", 10**9)
    path = zip_with_duplicates(tmp_path / "dup.docx", b"\x00" * 200_000, b"\x01" * 200_000, name="word/media/a.bin")
    assert too_large(path, DocumentFormat.DOCX).what == "inflated bytes"


class Unseekable(io.RawIOBase):
    """A write-only stream: zipfile must then write data descriptors (sizes after the data)."""

    def __init__(self) -> None:
        self.data = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, chunk: bytes | bytearray | memoryview) -> int:
        self.data += bytes(chunk)
        return len(chunk)


def streamed_zip(path: Path, name: str, payload: bytes) -> Path:
    sink = Unseekable()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    path.write_bytes(bytes(sink.data))
    with zipfile.ZipFile(path) as check:
        assert check.infolist()[0].flag_bits & 0x08  # the sizes are in a data descriptor
    return path


def test_an_entry_with_a_data_descriptor_is_read_from_its_stream_and_passes(tmp_path: Path):
    inspect_file(
        streamed_zip(tmp_path / "dd.docx", "word/document.xml", b"<a>" + b"hello " * 5000 + b"</a>"),
        DocumentFormat.DOCX,
    )


def test_a_bomb_in_a_data_descriptor_entry_is_refused_by_the_ratio(tmp_path: Path):
    assert (
        too_large(streamed_zip(tmp_path / "ddb.docx", "word/document.xml", ZEROS), DocumentFormat.DOCX).what
        == "compression ratio"
    )


def test_a_dtd_in_a_data_descriptor_entry_is_refused(tmp_path: Path):
    payload = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'
    assert "DTD" in parse_failed(streamed_zip(tmp_path / "ddx.docx", "word/document.xml", payload), DocumentFormat.DOCX)


def crlf_csv(path: Path, rows: int, columns: int, line_ending: str) -> Path:
    header = '(x, y);1:2;"w""z"'
    body = line_ending.join(";".join("v" for _ in range(columns)) for _ in range(rows))
    return build_csv(path, header + line_ending + body + line_ending)


def test_a_crlf_csv_is_sniffed_and_counted_after_docling_s_line_ending_normalisation(tmp_path: Path):
    # Docling: 210,003 cells (the header sniffs as ';'). Without the normalisation the guard saw 2,102.
    path = crlf_csv(tmp_path / "crlf.csv", 2100, 100, "\r\n")
    assert too_large(path, DocumentFormat.CSV).what == "cells"
    assert too_large(crlf_csv(tmp_path / "cr.csv", 2100, 100, "\r"), DocumentFormat.CSV).what == "cells"
    assert too_large(crlf_csv(tmp_path / "lf.csv", 2100, 100, "\n"), DocumentFormat.CSV).what == "cells"


def test_the_guard_and_docling_agree_on_the_cells_of_a_crlf_and_a_cr_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for ending in ("\r\n", "\r", "\n"):
        path = crlf_csv(tmp_path / "x.csv", 5, 4, ending)  # 3 header cells + 5 x 4
        markdown = file_to_markdown(path, "text/csv").markdown
        assert markdown.count("| v | v | v | v |") == 5
        monkeypatch.setattr(limits, "MAX_CELLS", 23)
        inspect_file(path, DocumentFormat.CSV)  # exactly 23
        monkeypatch.setattr(limits, "MAX_CELLS", 22)
        assert too_large(path, DocumentFormat.CSV).actual == 23
        monkeypatch.undo()
