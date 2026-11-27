"""The result and the one error of the Notion converter (kept apart so the modules of the package can share them)."""

from __future__ import annotations

from dataclasses import dataclass

from notion.model import DatabaseRef, PageRef


class MarkupTooDeepError(Exception):
    """The Markdown is nested too deeply to convert."""


@dataclass(frozen=True)
class ConvertedPage:
    """A page as GFM, and what it points at: ``sub_pages`` (``<page>`` lines) and ``databases`` (``<database>`` lines)
    in document order, each as a reference the import follows."""

    markdown: str
    sub_pages: tuple[PageRef, ...] = ()
    databases: tuple[DatabaseRef, ...] = ()
