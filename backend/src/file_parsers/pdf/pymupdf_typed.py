"""Typed views of the few untyped PyMuPDF entry points the PDF enrichers use.

PyMuPDF ships no complete annotations, so its constructors and page methods are
untyped under strict mypy. Each entry point is declared once here through a
``Protocol`` that states exactly what the callers rely on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, cast

import pymupdf


class PixmapView(Protocol):
    """A rendered raster image."""

    width: int
    height: int

    def tobytes(self, output: str = "png") -> bytes: ...


class RectView(Protocol):
    """A page rectangle."""


class TableView(Protocol):
    """A table found on a page."""

    bbox: Sequence[float]


class PageView(Protocol):
    """One page of an open document."""

    def get_pixmap(self, *, dpi: int, alpha: bool, clip: RectView | None = None) -> PixmapView: ...

    def find_tables(self) -> Sequence[TableView]: ...


class DocumentView(Protocol):
    """An open document indexed by zero-based page number."""

    def __getitem__(self, index: int) -> PageView: ...


_open_document = cast(Callable[[Path], AbstractContextManager[DocumentView]], pymupdf.open)
_open_pixmap = cast(Callable[[str], PixmapView], pymupdf.Pixmap)
_make_rect = cast(Callable[[Sequence[float]], RectView], pymupdf.Rect)


def open_document(path: Path) -> AbstractContextManager[DocumentView]:
    """Open the PDF at ``path`` as a context manager."""

    return _open_document(path)


def open_pixmap(path: Path) -> PixmapView:
    """Load the image at ``path``."""

    return _open_pixmap(str(path))


def make_rect(bbox: Sequence[float]) -> RectView:
    """Build a rectangle from a four-number bounding box."""

    return _make_rect(bbox)
