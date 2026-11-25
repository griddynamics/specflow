"""Notion-flavored Markdown (NFM v2) to plain GitHub-flavored Markdown (docs/notion/source.md "Conversion"). Stream B
fills this package; the prototype is ``plans/NOTION/prototype/nfm_to_gfm.py`` and the rules are the table of
``docs/notion/discovery.md``.

Rules the implementation keeps: a line-based pass with no HTML parser; no NFM tag is left in the output; no e-mail is
left in the output or in ``scrub_original``; images are named, never fetched; the output is deterministic; input too
deeply nested raises ``MarkupTooDeepError`` (the import records ``convert_failed``, step ``convert``); hostile input
(a tag injected in text, a huge page) never raises anything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


def to_markdown(nfm: str) -> ConvertedPage:
    """Convert the Markdown of ``GET /v1/pages/{id}/markdown`` (v2)."""

    raise NotImplementedError("stream B fills to_markdown")


def render_row(title: str, properties: Mapping[str, Any], body: ConvertedPage) -> str:
    """The document of a database row: ``# <title>``, a table of its properties (Notion's property map; people as
    display names only; an ``email`` property is shown as "(e-mail hidden)"; relations and files by name; no user id),
    then the converted ``body``."""

    raise NotImplementedError("stream B fills render_row")


def scrub_original(nfm: str) -> str:
    """The Markdown with every e-mail removed from ``user://<id>/<email>`` mentions (and from any other text), for the
    original that the import stores (N5)."""

    raise NotImplementedError("stream B fills scrub_original")
