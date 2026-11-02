"""Build small text PDFs for the parser tests (no PDF library: a PDF is a few objects and an xref table).

Text uses the standard Helvetica font, so pdfium extracts it without an embedded font. A ``Line`` can
carry its size either as the font size (``Tf``) or as a text-matrix scale with font size 1 (``Tm``),
the layout in which the nominal font size alone is useless.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PAGE_WIDTH, PAGE_HEIGHT = 612, 792


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    size: float = 12.0
    via_matrix: bool = False


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(lines: list[Line]) -> bytes:
    commands = ["BT"]
    y = PAGE_HEIGHT - 72.0
    for line in lines:
        if line.via_matrix:
            commands.append(f"/F1 1 Tf {line.size} 0 0 {line.size} 72 {y} Tm ({_escape(line.text)}) Tj")
        else:
            commands.append(f"/F1 {line.size} Tf 1 0 0 1 72 {y} Tm ({_escape(line.text)}) Tj")
        y -= line.size * 1.8
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")


def build_pdf(pages: list[list[Line]]) -> bytes:
    """A PDF with one page per entry of ``pages`` (an empty list is a blank page)."""

    count = len(pages)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(count))}] /Count {count} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, lines in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        stream = _content(lines)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    body = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number, data in objects.items():
        offsets[number] = len(body)
        body += f"{number} 0 obj\n".encode() + data + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for number in objects:
        body += f"{offsets[number]:010d} 00000 n \n".encode()
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(body)


def write_pdf(path: Path, pages: list[list[Line]]) -> Path:
    path.write_bytes(build_pdf(pages))
    return path
