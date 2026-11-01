"""Inspect an Office or web file before it is converted (DESIGN N2, G12).

Everything is counted while reading; no declared size is trusted (a ZIP header and an XLSX dimension tag can
both lie). A file over a limit raises ``DocumentTooLargeError``; a file that cannot be read as what it claims
to be raises ``DocumentParseFailedError``. Memory stays at one 64 KiB chunk (plus one XML element) per file,
whatever the input.

OOXML (DOCX, PPTX, XLSX): each deflate stream is inflated chunk by chunk from its raw bytes. The total
may not pass 200 MB; one entry may not inflate past 100:1 of the compressed bytes it consumed; the inflated size
must equal the declared one (the readers that follow trust the declared one); XML parts must be UTF-8 and may
not carry a DTD, so no reader can resolve an external entity (OOXML never needs one). A PPTX is counted in
slides and an XLSX in ``<c>`` cells, streamed through ``defusedxml``.

The sheets and slides that are counted are the ones the libraries load: they are resolved through the package
relationships (``_rels/.rels`` -> main part -> its relationships), never by their path, so a part moved to
``xl/other/sheet1.xml`` is counted like a default one. No other structure scan is made (a deep or recursive
XML tree makes a parser raise, and the facade maps every engine failure to ``document_parse_failed``).

The N2 byte caps (OOXML 50 MiB, HTML/Markdown/CSV 10 MiB) are checked first: the Drive import streams files
without the upload check.

HTML, Markdown and CSV: the bytes must be UTF-8. A CSV is counted with the dialect Docling's own CSV backend
detects (``,`` ``;`` tab ``|`` ``:``), through the same sniffer, so the count is the one Docling parses.
"""

from __future__ import annotations

import codecs
import csv
import io
import posixpath
import struct
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeElementTree
from docling.backend.csv_backend import _SNIFF_SAMPLE_SIZE, _sniff_dialect

from file_parsers.formats import DocumentFormat
from file_parsers.limits import (
    MAX_OOXML_ENTRIES,
    MAX_OOXML_FILE_BYTES,
    MAX_OOXML_INFLATED_BYTES,
    MAX_OOXML_RATIO,
    MAX_TEXT_FILE_BYTES,
    RATIO_FLOOR_BYTES,
    DocumentParseFailedError,
    DocumentTooLargeError,
    check_cells,
    check_slides,
)

CHUNK = 64 * 1024
OOXML_FORMATS = frozenset({DocumentFormat.DOCX, DocumentFormat.PPTX, DocumentFormat.XLSX})

_LOCAL_HEADER = struct.Struct("<4s5H3L2H")  # signature, 5 x uint16, crc, sizes, name and extra length
_LOCAL_MAGIC = b"PK\x03\x04"
_ENCRYPTED = 0x1
_ROOT_RELS = "_rels/.rels"
_CONTENT_TYPES = "[Content_Types].xml"
_XML_SUFFIXES = (".xml", ".rels")
_NOT_UTF8_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def inspect_file(path: Path, document_format: DocumentFormat) -> None:
    """Run the checks of ``document_format`` on the file at ``path`` (PDF has its own, in ``limits``)."""

    if document_format is DocumentFormat.PDF:
        raise ValueError("PDF is checked by limits.check_pdf_file")
    path = Path(path)
    ooxml = document_format in OOXML_FORMATS
    try:
        size = path.stat().st_size
        limit = MAX_OOXML_FILE_BYTES if ooxml else MAX_TEXT_FILE_BYTES
        if size > limit:
            raise DocumentTooLargeError("file bytes", size, limit)
        if ooxml:
            _inspect_ooxml(path, document_format)
        else:
            _inspect_text(path, document_format)
    except OSError as exc:  # missing, unreadable, not a regular file
        raise DocumentParseFailedError(f"cannot read the file ({type(exc).__name__})") from exc


def _inspect_ooxml(path: Path, document_format: DocumentFormat) -> None:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, ValueError, NotImplementedError, EOFError) as exc:
        raise DocumentParseFailedError(f"not a readable ZIP archive ({type(exc).__name__})") from exc
    with archive, path.open("rb") as raw:
        infos = archive.infolist()
        if len(infos) > MAX_OOXML_ENTRIES:
            raise DocumentTooLargeError("archive entries", len(infos), MAX_OOXML_ENTRIES)
        inflated = 0
        # The content types come first: for DOCX and PPTX they say which entries the libraries parse as XML.
        files = [info for info in infos if not info.is_dir()]
        first = [info for info in files if info.filename == _CONTENT_TYPES]
        for info in first:
            inflated += _inspect_entry(raw, info, inflated, _XmlSniffer(always=True, by_content=False))
        declared = frozenset() if document_format is DocumentFormat.XLSX else _declared_xml(archive, files)
        for info in files:  # every other entry, duplicates included
            if info.filename != _CONTENT_TYPES:
                inflated += _inspect_entry(raw, info, inflated, _sniffer_for(info, document_format, declared))
        # The structure checks parse XML, so they run after the scan has bounded every entry.
        if document_format is DocumentFormat.PPTX:
            check_slides(_count_slides(archive))
        elif document_format is DocumentFormat.XLSX:
            _count_workbook_cells(archive)


def _sniffer_for(info: zipfile.ZipInfo, document_format: DocumentFormat, declared: frozenset[str]) -> _XmlSniffer:
    """DOCX and PPTX: the entries named ``.xml``/``.rels`` or declared XML. XLSX: those, and every XML-looking entry."""

    named = info.filename.lower().endswith(_XML_SUFFIXES) or info.filename in declared
    return _XmlSniffer(always=named, by_content=document_format is DocumentFormat.XLSX)


def _declared_xml(archive: zipfile.ZipFile, files: list[zipfile.ZipInfo]) -> frozenset[str]:
    """Entry names whose content type in ``[Content_Types].xml`` is XML (``*/xml`` or ``*+xml``).

    An Override decides for its part, else the Default of the file extension does. ``image/svg+xml`` is not
    XML here: python-docx, python-pptx and Docling keep an SVG as a binary image (Illustrator and Inkscape
    write a DOCTYPE into them). Nothing is declared when the part is missing.
    """

    entries = _streamed(archive, _CONTENT_TYPES, frozenset({"Default", "Override"}))
    by_extension = {e["Extension"].lower(): _is_xml_type(e.get("ContentType", "")) for e in entries if "Extension" in e}
    by_part = {e["PartName"].lstrip("/"): _is_xml_type(e.get("ContentType", "")) for e in entries if "PartName" in e}
    declared = set()
    for info in files:
        name = info.filename
        if by_part.get(name, by_extension.get(name.rpartition(".")[2].lower(), False)):
            declared.add(name)
    return frozenset(declared)


def _is_xml_type(content_type: str) -> bool:
    kind = content_type.split(";", maxsplit=1)[0].strip().lower()
    return kind != "image/svg+xml" and (kind.endswith(("/xml", "+xml")))


def _inspect_entry(raw: BinaryIO, info: zipfile.ZipInfo, inflated_before: int, sniffer: _XmlSniffer) -> int:
    """Inflate one entry from its raw bytes, enforce the limits, and return the bytes it inflated to."""

    if info.flag_bits & _ENCRYPTED:
        raise DocumentParseFailedError("encrypted archive entry")
    inflated = 0
    try:
        for chunk, consumed in _entry_chunks(raw, info):
            inflated += len(chunk)
            if inflated_before + inflated > MAX_OOXML_INFLATED_BYTES:
                raise DocumentTooLargeError("inflated bytes", inflated_before + inflated, MAX_OOXML_INFLATED_BYTES)
            if inflated > RATIO_FLOOR_BYTES and inflated > MAX_OOXML_RATIO * max(consumed, 1):
                raise DocumentTooLargeError("compression ratio", inflated // max(consumed, 1), MAX_OOXML_RATIO)
            sniffer.feed(chunk)
    except (zlib.error, OSError) as exc:  # OSError: an offset that points outside the file
        raise DocumentParseFailedError("corrupt archive entry") from exc
    if inflated != info.file_size:
        raise DocumentParseFailedError("archive entry size differs from its declared size")
    return inflated


def _entry_chunks(raw: BinaryIO, info: zipfile.ZipInfo) -> Iterator[tuple[bytes, int]]:
    """Yield ``(inflated chunk, compressed bytes consumed so far)`` until the entry's stream ends."""

    raw.seek(_data_start(raw, info))
    if info.compress_type == zipfile.ZIP_STORED:
        consumed = 0
        while consumed < info.compress_size:
            chunk = raw.read(min(CHUNK, info.compress_size - consumed))
            if not chunk:
                raise DocumentParseFailedError("truncated archive entry")
            consumed += len(chunk)
            yield chunk, consumed
        return
    if info.compress_type != zipfile.ZIP_DEFLATED:
        raise DocumentParseFailedError("unsupported compression method")
    stream = zlib.decompressobj(-zlib.MAX_WBITS)
    fed = 0
    while True:
        block = raw.read(CHUNK)
        if not block:
            raise DocumentParseFailedError("truncated archive entry")
        fed += len(block)
        data = block
        while True:
            out = stream.decompress(data, CHUNK)
            data = stream.unconsumed_tail
            yield out, fed - len(data) - len(stream.unused_data)
            if stream.eof or (len(out) < CHUNK and not data):
                break
        if stream.eof:
            return


def _data_start(raw: BinaryIO, info: zipfile.ZipInfo) -> int:
    raw.seek(info.header_offset)
    header = raw.read(_LOCAL_HEADER.size)
    if len(header) != _LOCAL_HEADER.size:
        raise DocumentParseFailedError("truncated archive entry")
    fields = _LOCAL_HEADER.unpack(header)
    if fields[0] != _LOCAL_MAGIC:
        raise DocumentParseFailedError("bad archive entry header")
    name_length, extra_length = int(fields[9]), int(fields[10])
    return info.header_offset + _LOCAL_HEADER.size + name_length + extra_length


def _xml_head(head: bytes) -> bool:
    """Whether ``head`` starts like an XML document: ``<`` after an optional UTF-8 BOM and whitespace."""

    return head.removeprefix(codecs.BOM_UTF8).lstrip().startswith(b"<")


def _utf16_head(head: bytes) -> bool:
    """Whether ``head`` starts like UTF-16 text that is XML (a BOM, or ``<`` with a zero byte beside it)."""

    return head.startswith(_NOT_UTF8_BOMS) or head[:2] in (b"<\x00", b"\x00<")


class _XmlSniffer:
    """Refuses an entry that is XML but not UTF-8, or that declares a DTD (entities need one).

    ``always``: the entry is XML by its name or its declared content type, so it is always checked.
    ``by_content``: any other entry is also checked when its first bytes look like XML (XLSX: openpyxl opens
    whatever path a relationship names). Entries that are neither are ignored (images, binaries, SVG).
    """

    def __init__(self, *, always: bool, by_content: bool) -> None:
        self._always = always
        self._by_content = by_content
        self._first = True
        self._active = always
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        if self._first and chunk:
            self._first = False
            head = chunk[:64]
            if (self._always and (_utf16_head(head) or b"\x00" in head[:4])) or (
                self._by_content and _utf16_head(head)
            ):
                raise DocumentParseFailedError("XML part is not UTF-8")
            self._active = self._always or (self._by_content and _xml_head(head))
        if not self._active:
            return
        window = self._tail + chunk
        if b"<!DOCTYPE" in window:
            raise DocumentParseFailedError("XML part declares a DTD")
        self._tail = window[-8:]  # one byte shorter than the marker: it cannot hide across two chunks


def _rels_name(part: str) -> str:
    directory, _, name = part.rpartition("/")
    return f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"


def _resolve(source: str, target: str) -> str:
    """The archive name a relationship ``target`` of the part ``source`` points to."""

    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _streamed(archive: zipfile.ZipFile, name: str, tags: frozenset[str]) -> list[dict[str, str]]:
    """The attributes of every element named in ``tags`` (local names) of the XML part ``name``, in order."""

    found: list[dict[str, str]] = []
    try:
        with archive.open(name) as stream:
            for _, element in SafeElementTree.iterparse(stream, events=("end",), forbid_dtd=True):
                if element.tag.rpartition("}")[2] in tags:
                    found.append({key.rpartition("}")[2]: value for key, value in element.attrib.items()})
                element.clear()
    except KeyError:
        return []
    except (ParseError, DefusedXmlException) as exc:
        raise DocumentParseFailedError(f"{name} is not valid XML ({type(exc).__name__})") from exc
    return found


def _relationships(archive: zipfile.ZipFile, source: str) -> list[tuple[str, str, str]]:
    """``(id, type, target name)`` of the internal relationships of ``source`` ("" is the package root)."""

    rels = _streamed(archive, _rels_name(source) if source else _ROOT_RELS, frozenset({"Relationship"}))
    return [
        (rel.get("Id", ""), rel.get("Type", ""), _resolve(source, rel["Target"]))
        for rel in rels
        if "Target" in rel and rel.get("TargetMode") != "External"
    ]


def _main_part(archive: zipfile.ZipFile) -> str:
    for _, kind, target in _relationships(archive, ""):
        if kind.endswith("/officeDocument"):
            if target not in archive.namelist():
                break
            return target
    raise DocumentParseFailedError("the package has no main document part")


def _count_slides(archive: zipfile.ZipFile) -> int:
    """Slides as python-pptx loads them: the ``sldId`` entries of the presentation (and, at least, its slide parts)."""

    main = _main_part(archive)
    entries = len(_streamed(archive, main, frozenset({"sldId"})))
    parts = sum(1 for _, kind, _ in _relationships(archive, main) if kind.endswith("/slide"))
    return max(entries, parts)


_SPREADSHEET_NAMESPACES = frozenset(
    {"http://schemas.openxmlformats.org/spreadsheetml/2006/main", "http://purl.oclc.org/ooxml/spreadsheetml/main"}
)
_CELL = frozenset(f"{{{ns}}}c" for ns in _SPREADSHEET_NAMESPACES)
_SHEET_ENTRY = frozenset(f"{{{ns}}}sheet" for ns in _SPREADSHEET_NAMESPACES)
_SHEET_DATA = frozenset(f"{{{ns}}}sheetData" for ns in _SPREADSHEET_NAMESPACES)
_ROW = frozenset(f"{{{ns}}}row" for ns in _SPREADSHEET_NAMESPACES)


def _count_workbook_cells(archive: zipfile.ZipFile) -> None:
    """Refuse a workbook with more than ``MAX_CELLS`` cells, counted as an upper bound of what openpyxl loads.

    How openpyxl finds the workbook part (content types) and each sheet (any relationship type) differs from
    the package's own relationships and could be steered, so nothing is discovered: every SpreadsheetML ``<c>``
    element of every entry that looks like XML counts, whatever its name or extension (openpyxl opens whatever
    path a sheet relationship names: ``xl/s.dat``, ``xl/s``) and whatever points at it. Entries that do not start
    with ``<`` (images, binaries) are skipped; one that does start with ``<`` must parse with the safe parser and
    carry no DOCTYPE, or the file is refused (an SVG without a DOCTYPE passes).
    A chart has another namespace and does not count.
    openpyxl may parse one part once per ``<sheet>`` entry that lists it, so the cells of each part are also
    summed per listing, resolving the listing's relationship id through the relationships of whichever part
    holds the ``<sheet>`` entries (any type); the larger total decides.
    """

    own: dict[str, int] = {}
    listings: dict[str, list[str]] = {}  # part holding <sheet> entries -> their relationship ids
    cells = 0
    for name in dict.fromkeys(info.filename for info in archive.infolist()):
        if _looks_like_xml(archive, name):
            count, sheet_ids = _scan_part(archive, name, cells)
            own[name], cells = count, cells + count
            if sheet_ids:
                listings[name] = sheet_ids
    listed = 0
    for holder, ids in listings.items():
        targets = {rid: target for rid, _, target in _relationships(archive, holder)}
        for rid in ids:
            listed += own.get(targets.get(rid, ""), 0)
            check_cells(listed)


def _looks_like_xml(archive: zipfile.ZipFile, name: str) -> bool:
    """Whether the entry's first bytes, after an optional UTF-8 BOM and whitespace, start with ``<``."""

    if archive.getinfo(name).is_dir():
        return False
    with archive.open(name) as stream:
        head = stream.read(64)
    return _xml_head(head)


def _scan_part(archive: zipfile.ZipFile, name: str, cells_before: int) -> tuple[int, list[str]]:
    """Count the spreadsheet ``<c>`` elements of one part (stopping at the cap) and collect its ``<sheet>`` ids."""

    count = 0
    sheet_ids: list[str] = []
    sheet_data = None
    try:
        with archive.open(name) as stream:
            for event, element in SafeElementTree.iterparse(stream, events=("start", "end"), forbid_dtd=True):
                if event == "start":
                    if element.tag in _SHEET_DATA:
                        sheet_data = element
                elif element.tag in _CELL:
                    count += 1
                    check_cells(cells_before + count)
                elif element.tag in _SHEET_ENTRY:
                    sheet_ids.extend(value for key, value in element.attrib.items() if key.rpartition("}")[2] == "id")
                elif element.tag in _ROW and sheet_data is not None:
                    sheet_data.clear()  # keeps memory flat: finished rows are dropped
    except (ParseError, DefusedXmlException) as exc:  # an entry that looks like XML must be XML: never skipped
        raise DocumentParseFailedError(f"{name} is not valid XML ({type(exc).__name__})") from exc
    return count, sheet_ids


def _inspect_text(path: Path, document_format: DocumentFormat) -> None:
    try:
        with path.open("rb") as raw:
            if document_format is DocumentFormat.CSV:
                _count_csv_cells(raw)
            else:
                _require_utf8(raw)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise DocumentParseFailedError(f"not valid UTF-8 text or CSV ({type(exc).__name__})") from exc


def _require_utf8(raw: BinaryIO) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    while chunk := raw.read(CHUNK):
        decoder.decode(chunk)
    decoder.decode(b"", final=True)


def _count_csv_cells(raw: BinaryIO) -> None:
    """Count the cells Docling's CSV backend will parse: same decoding, same dialect sniffing, same reader.

    The file is at most ``MAX_TEXT_FILE_BYTES`` (checked before), and Docling holds it as text too.
    """

    # Docling's decode_text turns \r\n and lone \r into \n before it sniffs and parses; so does this.
    text = raw.read().decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    content = io.StringIO(text)
    head = content.readline()
    while head in ("\n", "\r\n", "\r"):
        head = content.readline()
    sample_start = content.tell() - len(head)

    def read_sample() -> str:
        content.seek(sample_start)
        return content.read(_SNIFF_SAMPLE_SIZE)

    try:
        dialect = _sniff_dialect(head, read_sample)
    except csv.Error:
        dialect = csv.excel  # what Docling falls back to
    content.seek(0)
    cells = 0
    for row in csv.reader(content, dialect=dialect, doublequote=True, strict=True):
        cells += len(row)
        check_cells(cells)
