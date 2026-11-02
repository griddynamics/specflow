"""Bytes the fake Drive serves for the formats the parser reads.

Each file is a few hundred bytes, built here with the standard library only (no Office library,
nothing binary committed), and real parsers read it: one paragraph, one slide title or one cell
that names the Drive file, so two files never share content. PDF is served by ``drive.fake``.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Mapping
from typing import Final
from xml.sax.saxutils import escape

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_SLIDE = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
_OFFICE = "application/vnd.openxmlformats-officedocument."
_XML = '<?xml version="1.0" encoding="UTF-8"?>'


def _package(parts: Mapping[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _relationships(*items: tuple[str, str, str]) -> str:
    entries = "".join(f'<Relationship Id="{i}" Type="{_REL}/{kind}" Target="{target}"/>' for i, kind, target in items)
    return f'{_XML}<Relationships xmlns="{_PACKAGE_REL}">{entries}</Relationships>'


def _content_types(*overrides: tuple[str, str]) -> str:
    entries = "".join(f'<Override PartName="{part}" ContentType="{kind}"/>' for part, kind in overrides)
    return (
        f'{_XML}<Types xmlns="{_TYPES}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>{entries}</Types>'
    )


def _docx(label: str) -> bytes:
    document = (
        f'{_XML}<w:document xmlns:w="{_WORD}"><w:body><w:p><w:r><w:t>{label}</w:t></w:r></w:p></w:body></w:document>'
    )
    return _package(
        {
            "[Content_Types].xml": _content_types(
                ("/word/document.xml", _OFFICE + "wordprocessingml.document.main+xml")
            ),
            "_rels/.rels": _relationships(("rId1", "officeDocument", "word/document.xml")),
            "word/document.xml": document,
        }
    )


def _xlsx(label: str) -> bytes:
    workbook = (
        f'{_XML}<workbook xmlns="{_SHEET}" xmlns:r="{_REL}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    sheet = (
        f'{_XML}<worksheet xmlns="{_SHEET}"><sheetData><row r="1">'
        f'<c r="A1" t="inlineStr"><is><t>{label}</t></is></c></row></sheetData></worksheet>'
    )
    return _package(
        {
            "[Content_Types].xml": _content_types(
                ("/xl/workbook.xml", _OFFICE + "spreadsheetml.sheet.main+xml"),
                ("/xl/worksheets/sheet1.xml", _OFFICE + "spreadsheetml.worksheet+xml"),
            ),
            "_rels/.rels": _relationships(("rId1", "officeDocument", "xl/workbook.xml")),
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": _relationships(("rId1", "worksheet", "worksheets/sheet1.xml")),
            "xl/worksheets/sheet1.xml": sheet,
        }
    )


def _pptx(label: str) -> bytes:
    namespaces = f'xmlns:p="{_SLIDE}" xmlns:a="{_DRAWING}" xmlns:r="{_REL}"'
    tree = '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    title = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr>'
        f"</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>{label}</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    colors = " ".join(f'{n}="{n}"' for n in ("accent1", "accent2", "accent3", "accent4", "accent5", "accent6"))
    return _package(
        {
            "[Content_Types].xml": _content_types(
                ("/ppt/presentation.xml", _OFFICE + "presentationml.presentation.main+xml"),
                ("/ppt/slideMasters/slideMaster1.xml", _OFFICE + "presentationml.slideMaster+xml"),
                ("/ppt/slideLayouts/slideLayout1.xml", _OFFICE + "presentationml.slideLayout+xml"),
                ("/ppt/slides/slide1.xml", _OFFICE + "presentationml.slide+xml"),
            ),
            "_rels/.rels": _relationships(("rId1", "officeDocument", "ppt/presentation.xml")),
            "ppt/presentation.xml": (
                f"{_XML}<p:presentation {namespaces}>"
                '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": _relationships(
                ("rId1", "slideMaster", "slideMasters/slideMaster1.xml"), ("rId2", "slide", "slides/slide1.xml")
            ),
            "ppt/slideMasters/slideMaster1.xml": (
                f"{_XML}<p:sldMaster {namespaces}>{tree}</p:spTree></p:cSld>"
                '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
                f'{colors} hlink="hlink" folHlink="folHlink"/>'
                '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>'
            ),
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": _relationships(
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")
            ),
            "ppt/slideLayouts/slideLayout1.xml": (
                f'{_XML}<p:sldLayout {namespaces} type="title">{tree}</p:spTree></p:cSld></p:sldLayout>'
            ),
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _relationships(
                ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")
            ),
            "ppt/slides/slide1.xml": f"{_XML}<p:sld {namespaces}>{tree}{title}</p:spTree></p:cSld></p:sld>",
            "ppt/slides/_rels/slide1.xml.rels": _relationships(
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")
            ),
        }
    )


def _text(builder: Callable[[str], str]) -> Callable[[str], bytes]:
    return lambda label: builder(label).encode("utf-8")


_BUILDERS: Final[Mapping[str, Callable[[str], bytes]]] = {
    _OFFICE + "wordprocessingml.document": _docx,
    _OFFICE + "presentationml.presentation": _pptx,
    _OFFICE + "spreadsheetml.sheet": _xlsx,
    "text/html": _text(lambda label: f"<html><body><h1>{label}</h1><p>Fake Drive file.</p></body></html>\n"),
    "text/markdown": _text(lambda label: f"# {label}\n\nFake Drive file.\n"),
    "text/csv": _text(lambda label: f"name,value\n{label},1\n"),
}


def sample_bytes(mime_type: str, file_id: str) -> bytes | None:
    """The sample file of a type the parser reads (not PDF), or ``None`` for any other type."""

    builder = _BUILDERS.get(mime_type)
    return None if builder is None else builder(escape(file_id))
