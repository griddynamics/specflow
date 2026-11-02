"""Builders for the Office and web test fixtures: nothing binary is committed, every file is generated here.

``Blocks`` is the one content definition (headings, paragraphs, a table); ``truth_markdown`` renders it as
the Markdown a perfect parser would produce, and ``build_docx``/``build_html``/``build_markdown`` write the
same content in each format. The hostile fixtures (ZIP bomb with false sizes, DTD, false dimension tag) are
patched out of valid archives.
"""

from __future__ import annotations

import struct
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class Table:
    rows: tuple[tuple[str, ...], ...]


Blocks = tuple[Heading | Paragraph | Table, ...]

SPEC: Blocks = (
    Heading(1, "Platform Overview"),
    Paragraph("The platform stores every requirement together with its rationale and the people who approved it."),
    Heading(2, "Identity and Access"),
    Paragraph("Users authenticate through the corporate provider and receive roles per project."),
    Table(
        (
            ("Identifier", "Capability", "Owner"),
            ("FR-IAM-001", "Single sign-on", "Platform"),
            ("FR-IAM-002", "Role mapping", "Security"),
        )
    ),
    Heading(3, "Audit Trail"),
    Paragraph("Every change is recorded with the actor, the time and the previous value."),
    Heading(2, "Projects"),
    Paragraph("A project groups documents, requirements and the knowledge graph built from them."),
    Table((("Identifier", "Capability"), ("FR-PRJ-001", "Create a project"), ("FR-PRJ-002", "Archive a project"))),
)


def truth_markdown(blocks: Blocks, *, level_offset: int = 0) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            parts.append(f"{'#' * (block.level + level_offset)} {block.text}")
        elif isinstance(block, Paragraph):
            parts.append(block.text)
        else:
            header, *body = block.rows
            lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
            lines += ["| " + " | ".join(row) + " |" for row in body]
            parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def build_docx(path: Path, blocks: Blocks = SPEC, *, title: str | None = None) -> Path:
    document = Document()
    if title is not None:
        document.add_heading(title, 0)  # the "Title" style
    for block in blocks:
        if isinstance(block, Heading):
            document.add_heading(block.text, block.level)
        elif isinstance(block, Paragraph):
            document.add_paragraph(block.text)
        else:
            table = document.add_table(rows=len(block.rows), cols=len(block.rows[0]))
            for r, row in enumerate(block.rows):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
    document.save(path)
    return path


def build_html(path: Path, blocks: Blocks = SPEC) -> Path:
    parts = ["<!doctype html><html><head><title>t</title></head><body>"]
    for block in blocks:
        if isinstance(block, Heading):
            parts.append(f"<h{block.level}>{block.text}</h{block.level}>")
        elif isinstance(block, Paragraph):
            parts.append(f"<p>{block.text}</p>")
        else:
            header, *body = block.rows
            parts.append("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in header) + "</tr></thead><tbody>")
            parts += ["<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in body]
            parts.append("</tbody></table>")
    parts.append("</body></html>")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def build_markdown(path: Path, blocks: Blocks = SPEC) -> Path:
    path.write_text(truth_markdown(blocks), encoding="utf-8")
    return path


def build_pptx(path: Path, slides: list[tuple[str, list[str]]]) -> Path:
    """One slide per ``(title, bullets)``, on the "Title and Content" layout (real title placeholders)."""

    deck = Presentation()
    for title, bullets in slides:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = "\n".join(bullets)
    deck.save(path)
    return path


def build_xlsx(path: Path, sheets: list[tuple[str, list[list[str | int]]]]) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    workbook.save(path)
    return path


def build_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def rewrite_member(source: Path, target: Path, member: str, transform: Callable[[bytes], bytes]) -> Path:
    """Copy the archive ``source`` to ``target`` with one member's bytes passed through ``transform``."""

    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            zout.writestr(info.filename, transform(data) if info.filename == member else data)
    return target


def with_dtd(source: Path, target: Path, member: str, canary: Path) -> Path:
    """The archive with an external-entity DTD (``&xxe;`` points at ``canary``) added to one XML member."""

    dtd = f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file://{canary}">]>'.encode()

    def add(data: bytes) -> bytes:
        if data.startswith(b"<?xml"):
            end = data.index(b"?>") + 2
            return data[:end] + dtd + data[end:]
        return dtd + data

    return rewrite_member(source, target, member, add)


def lie_about_sizes(archive: Path, declared: int) -> Path:
    """Overwrite the declared uncompressed size of the first entry, in the local and the central header."""

    data = bytearray(archive.read_bytes())
    struct.pack_into("<L", data, 22, declared)  # local header: uncompressed size
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<L", data, central + 24, declared)  # central directory: uncompressed size
    archive.write_bytes(bytes(data))
    return archive


def single_entry_zip(path: Path, name: str, payload: bytes, *, deflate: bool = True) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as archive:
        archive.writestr(name, payload)
    return path


REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="ppt/presentation.xml"/></Relationships>"""


def minimal_deck(path: Path, slides: int, *, folder: str = "slides", listed: int | None = None) -> Path:
    """A package with ``slides`` slide parts stored under ``ppt/<folder>/`` and found through the relationships.

    ``listed`` is the number of ``sldId`` entries (default: one per part); a larger number lists parts again.
    """

    listed = slides if listed is None else listed
    entries = "".join(f'<p:sldId id="{256 + i}" r:id="rId{1 + i % slides}"/>' for i in range(listed))
    relationships = "".join(
        f'<Relationship Id="rId{n}" Type="{REL_NS}/slide" Target="{folder}/slide{n}.xml"/>'
        for n in range(1, slides + 1)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", PACKAGE_RELS)
        archive.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="urn:p" xmlns:r="{REL_NS}"><p:sldIdLst>{entries}</p:sldIdLst></p:presentation>',
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}</Relationships>',
        )
        for number in range(1, slides + 1):
            archive.writestr(f"ppt/{folder}/slide{number}.xml", "<p:sld/>")
    return path


def move_member(source: Path, target: Path, old: str, new: str) -> Path:
    """The archive with the part ``old`` renamed ``new`` and every mention of its name in the other parts updated."""

    short_old, short_new = old.removeprefix("xl/"), new.removeprefix("xl/")
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == old:
                zout.writestr(new, data)
            else:
                zout.writestr(info.filename, data.replace(short_old.encode(), short_new.encode()))
    return target
