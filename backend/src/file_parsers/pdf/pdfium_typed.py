"""Typed access to the few pypdfium2 (PDFium) calls the PDF parser uses.

pypdfium2 ships no type information, so strict mypy sees it as ``Any``. Everything the parser needs is
declared once here, through ``Protocol`` views of exactly the methods used: open a PDF, read the text of
a page with the effective font size of each character, render a page or a clip to PNG, and read the size
of a PNG.

PDFium is not thread-safe: use one ``PdfiumDocument`` from one thread at a time.
"""

from __future__ import annotations

import ctypes
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Protocol, cast

import pypdfium2
from pypdfium2 import raw

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_END = 24
POINTS_PER_INCH = 72


class _Image(Protocol):
    """The part of a Pillow image used here (Pillow is pypdfium2's own optional dependency)."""

    width: int
    height: int

    def convert(self, mode: str) -> _Image: ...

    def crop(self, box: tuple[int, int, int, int]) -> _Image: ...

    def save(self, fp: BinaryIO, **options: str) -> None: ...


class _Bitmap(Protocol):
    def to_pil(self) -> _Image: ...


class _TextPage(Protocol):
    raw: object

    def count_chars(self) -> int: ...

    def get_text_range(self, index: int, count: int) -> str: ...

    def close(self) -> None: ...


class _Page(Protocol):
    def get_textpage(self) -> _TextPage: ...

    def render(self, *, scale: float) -> _Bitmap: ...

    def close(self) -> None: ...


class _Document(Protocol):
    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> _Page: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PageText:
    """The text layer of one page: ``sizes[i]`` is the effective font size (points) of ``text[i]``."""

    text: str
    sizes: list[float]


class PdfiumDocument:
    """An open PDF; pages are zero-based."""

    def __init__(self, document: _Document) -> None:
        self._document = document

    def __len__(self) -> int:
        return len(self._document)

    def char_count(self, index: int) -> int:
        """Number of characters in the text layer of the page (0 for a scan without text)."""

        page = self._document[index]
        try:
            text_page = page.get_textpage()
            try:
                return text_page.count_chars()
            finally:
                text_page.close()
        finally:
            page.close()

    def page_text(self, index: int, *, with_sizes: bool = True) -> PageText:
        """Text of the page and, unless ``with_sizes=False``, the effective size of every character.

        Effective size = the nominal font size x the text-matrix scale: the nominal size alone is
        1.0 in many PDFs (the size sits in the matrix).
        """

        page = self._document[index]
        try:
            text_page = page.get_textpage()
            try:
                count = text_page.count_chars()
                if not count:
                    return PageText("", [])
                text = text_page.get_text_range(0, count)
                return PageText(text, _char_sizes(text_page, count) if with_sizes else [])
            finally:
                text_page.close()
        finally:
            page.close()

    def render_png(self, index: int, dpi: int, clip: Sequence[float] | None = None) -> bytes:
        """Render the page (or ``clip`` = ``(x0, y0, x1, y1)`` in points, origin top left) to PNG bytes."""

        page = self._document[index]
        try:
            scale = dpi / POINTS_PER_INCH
            image = page.render(scale=scale).to_pil().convert("RGB")
            if clip is not None:
                x0, y0, x1, y1 = (round(value * scale) for value in clip)
                image = image.crop((max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1)))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            page.close()

    def close(self) -> None:
        self._document.close()


def _char_sizes(text_page: _TextPage, count: int) -> list[float]:
    matrix = raw.FS_MATRIX()
    sizes: list[float] = []
    for char in range(count):
        nominal = float(raw.FPDFText_GetFontSize(text_page.raw, char))
        scale = 1.0
        if raw.FPDFText_GetMatrix(text_page.raw, char, ctypes.byref(matrix)):
            scale = (matrix.c**2 + matrix.d**2) ** 0.5 or 1.0
        sizes.append(nominal * scale)
    return sizes


@contextmanager
def open_document(path: Path) -> Iterator[PdfiumDocument]:
    """Open the PDF at ``path``; raises ``pypdfium2.PdfiumError`` for a file that is not a PDF."""

    document = PdfiumDocument(cast(_Document, pypdfium2.PdfDocument(str(path))))
    try:
        yield document
    finally:
        document.close()


def page_count(path: Path) -> int:
    """Number of pages of the PDF at ``path`` (reads the page tree only)."""

    with open_document(path) as document:
        return len(document)


def png_size(path: Path) -> tuple[int, int]:
    """``(width, height)`` of the PNG at ``path``, read from its header."""

    with path.open("rb") as handle:
        header = handle.read(_IHDR_END)
    if len(header) < _IHDR_END or not header.startswith(_PNG_SIGNATURE):
        raise ValueError(f"not a PNG file: {path.name}")
    width, height = cast(tuple[int, int], struct.unpack(">II", header[16:_IHDR_END]))
    return width, height
