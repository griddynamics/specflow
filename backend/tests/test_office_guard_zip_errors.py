"""Every ZIP failure met while the guard reads an OOXML file is a ``DocumentParseFailedError`` (review M1).

A raw ``zipfile.BadZipFile`` (or ``RuntimeError``, ``NotImplementedError``...) is not in the parse activity's map of
final errors, so it would be retried under a generic code. The damaged archives are patched out of valid ones; the
errors zipfile only raises on archives that cannot be built here (ZIP64 limits, a short stream, corrupt deflate data
behind a valid scan) are injected at ``ZipFile.open``.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from file_parsers.formats import DocumentFormat
from file_parsers.limits import DocumentParseFailedError
from file_parsers.office import guard
from file_parsers.office.guard import inspect_file
from office_support import build_docx, build_pptx, build_xlsx

CONTENT_TYPES = "[Content_Types].xml"


def _header(data: bytes, name: str, magic: bytes, name_at: int) -> int:
    """Offset of the header with signature ``magic`` whose file name (at ``name_at``) is ``name``."""

    at = data.find(magic)
    while at >= 0:
        if data[at + name_at : at + name_at + len(name)] == name.encode():
            return at
        at = data.find(magic, at + 1)
    raise AssertionError(f"no header for {name}")


def _local(data: bytes, name: str) -> int:
    return _header(data, name, b"PK\x03\x04", 30)


def _central(data: bytes, name: str) -> int:
    return _header(data, name, b"PK\x01\x02", 46)


def _patched(source: Path, target: Path, name: str, *, local: dict[int, bytes], central: dict[int, bytes]) -> Path:
    """``source`` with bytes of entry ``name`` overwritten at offsets of its local and central headers."""

    data = bytearray(source.read_bytes())
    at_local, at_central = _local(bytes(data), name), _central(bytes(data), name)
    for offset, value in local.items():
        data[at_local + offset : at_local + offset + len(value)] = value
    for offset, value in central.items():
        data[at_central + offset : at_central + offset + len(value)] = value
    target.write_bytes(bytes(data))
    return target


def _flags(source: Path, target: Path, name: str, flag: int) -> Path:
    data = source.read_bytes()
    local_flags = struct.unpack_from("<H", data, _local(data, name) + 6)[0] | flag
    central_flags = struct.unpack_from("<H", data, _central(data, name) + 8)[0] | flag
    return _patched(
        source,
        target,
        name,
        local={6: struct.pack("<H", local_flags)},
        central={8: struct.pack("<H", central_flags)},
    )


def _refused(path: Path, document_format: DocumentFormat, cause: type[BaseException] | None) -> None:
    with pytest.raises(DocumentParseFailedError) as error:
        inspect_file(path, document_format)
    if cause is not None:
        assert isinstance(error.value.__cause__, cause)


def test_a_bad_crc_on_the_content_types_is_refused(tmp_path: Path):
    # The review repro: the CRC of [Content_Types].xml zeroed in both headers.
    source = build_docx(tmp_path / "s.docx")
    bad = _patched(source, tmp_path / "bad.docx", CONTENT_TYPES, local={14: b"\0\0\0\0"}, central={16: b"\0\0\0\0"})
    _refused(bad, DocumentFormat.DOCX, zipfile.BadZipFile)


def test_a_bad_crc_on_a_worksheet_is_refused(tmp_path: Path):
    source = build_xlsx(tmp_path / "s.xlsx", [("S", [["a", 1]])])
    name = "xl/worksheets/sheet1.xml"
    bad = _patched(source, tmp_path / "bad.xlsx", name, local={14: b"\0\0\0\0"}, central={16: b"\0\0\0\0"})
    _refused(bad, DocumentFormat.XLSX, zipfile.BadZipFile)


def test_a_local_header_name_that_differs_from_the_directory_is_refused(tmp_path: Path):
    source = build_docx(tmp_path / "s.docx")
    data = bytearray(source.read_bytes())
    at = _local(bytes(data), CONTENT_TYPES) + 30
    data[at : at + 1] = b"{"  # "{Content_Types].xml" in the local header only
    bad = tmp_path / "bad.docx"
    bad.write_bytes(bytes(data))
    _refused(bad, DocumentFormat.DOCX, zipfile.BadZipFile)


@pytest.mark.parametrize("flag", [0x20, 0x40], ids=["compressed-patched", "strong-encryption"])
def test_an_entry_flag_zipfile_does_not_implement_is_refused(flag: int, tmp_path: Path):
    source = build_docx(tmp_path / "s.docx")
    _refused(_flags(source, tmp_path / "bad.docx", CONTENT_TYPES, flag), DocumentFormat.DOCX, NotImplementedError)


def test_an_encrypted_entry_is_refused(tmp_path: Path):
    source = build_docx(tmp_path / "s.docx")
    _refused(_flags(source, tmp_path / "bad.docx", CONTENT_TYPES, 0x1), DocumentFormat.DOCX, None)


def test_an_unsupported_compression_method_is_refused(tmp_path: Path):
    source = build_docx(tmp_path / "s.docx")
    method = struct.pack("<H", zipfile.ZIP_LZMA)
    bad = _patched(source, tmp_path / "bad.docx", CONTENT_TYPES, local={8: method}, central={10: method})
    _refused(bad, DocumentFormat.DOCX, None)


@pytest.mark.parametrize("keep", [0.5, 0.95])
def test_a_truncated_archive_is_refused(keep: float, tmp_path: Path):
    data = build_docx(tmp_path / "s.docx").read_bytes()
    bad = tmp_path / "bad.docx"
    bad.write_bytes(data[: int(len(data) * keep)])
    _refused(bad, DocumentFormat.DOCX, None)


@pytest.mark.parametrize(
    "failure",
    [
        zipfile.BadZipFile("Bad CRC-32"),
        zipfile.LargeZipFile("ZIP64"),
        RuntimeError("File is encrypted, password required for extraction"),
        NotImplementedError("compression type 99"),
        EOFError(),
        zlib.error("invalid stored block lengths"),
    ],
    ids=lambda failure: type(failure).__name__,
)
@pytest.mark.parametrize("document_format", [DocumentFormat.DOCX, DocumentFormat.XLSX])
def test_every_zip_error_raised_while_reading_a_part_is_mapped(
    failure: Exception, document_format: DocumentFormat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = (
        build_docx(tmp_path / "s.docx")
        if document_format is DocumentFormat.DOCX
        else build_xlsx(tmp_path / "s.xlsx", [("S", [["a"]])])
    )

    def broken_open(self: zipfile.ZipFile, name: object, *args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(zipfile.ZipFile, "open", broken_open)
    _refused(source, document_format, type(failure))


@pytest.mark.parametrize(
    ("document_format", "counter"),
    [(DocumentFormat.PPTX, "_count_slides"), (DocumentFormat.XLSX, "_count_workbook_cells")],
)
def test_a_runtime_error_of_the_counting_code_is_not_reported_as_a_corrupt_archive(
    document_format: DocumentFormat, counter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Only the zipfile calls are mapped: a bug in the counting code must surface as itself.
    source = (
        build_pptx(tmp_path / "s.pptx", [("T", ["x"])])
        if document_format is DocumentFormat.PPTX
        else build_xlsx(tmp_path / "s.xlsx", [("S", [["a"]])])
    )

    def broken(*_: object) -> int:
        raise RuntimeError("bug in the counting code")

    monkeypatch.setattr(guard, counter, broken)
    with pytest.raises(RuntimeError, match="bug in the counting code"):
        inspect_file(source, document_format)
