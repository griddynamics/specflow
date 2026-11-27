"""The model of a decoded diagram and the closed codes of a failed decode (stdlib only).

One ``Diagram`` is one diagrams.net file or link; each of its pages is a ``Page``. The model is plain data: no XML
tree, no style strings. ``decode`` (file or link) builds it, ``to_markdown`` and ``file_markdown`` render it, and the
API reads the counts of it (``len(page.shapes)``, ``len(page.arrows)``) for the check of a link.

Caps (code constants, not settings). Exceeding ``MAX_INPUT_BYTES`` (the file, the fetched body or the decoded link
payload) or ``MAX_XML_BYTES`` (the inflated or unescaped XML) raises ``DiagramDecodeError("too_large")``. A diagram with
more than ``MAX_PAGES`` pages keeps the first ``MAX_PAGES`` and has ``capped = True``; a page with more than
``MAX_CELLS_PER_PAGE`` cells keeps the first ``MAX_CELLS_PER_PAGE`` cells in document order and the diagram has
``capped = True``. ``MAX_LINK_BYTES`` is the length of a pasted link (``api`` request body).
Rendered Markdown over ``MAX_MARKDOWN_BYTES`` (one page, ``to_markdown``) or ``MAX_MARKDOWN_FILE_BYTES`` (a file,
``file_markdown``) is ``too_large`` too.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

MiB: Final = 1024 * 1024
MAX_INPUT_BYTES: Final = 10 * MiB
MAX_XML_BYTES: Final = 20 * MiB
MAX_PAGES: Final = 50
MAX_CELLS_PER_PAGE: Final = 5000
MAX_LINK_BYTES: Final = 256 * 1024
MAX_MARKDOWN_BYTES: Final = 2 * MiB
MAX_MARKDOWN_FILE_BYTES: Final = 10 * MiB

DecodeCode = Literal["not_a_diagram", "too_large"]


class DiagramDecodeError(Exception):
    """A decode that stopped with a closed ``code``.

    ``not_a_diagram``: the bytes are no diagrams.net content (no mxfile or mxGraphModel, a ``.drawio.png`` without the
    embedded chunk, bad base64 or deflate, malformed XML, or a DTD or entity declaration). ``too_large``: a cap above.
    The message never carries input content.
    """

    def __init__(self, code: DecodeCode) -> None:
        super().__init__(code)
        self.code: DecodeCode = code


class Role(StrEnum):
    """What a shape is, from its style: rhombus is ``decision``, cylinder or datastore ``store``, document or note
    ``document``, text ``note``, swimlane, container or a vertex that is the parent of another ``group``, actor or
    umlActor ``actor``, anything else ``step``."""

    STEP = "step"
    DECISION = "decision"
    STORE = "store"
    DOCUMENT = "document"
    NOTE = "note"
    GROUP = "group"
    ACTOR = "actor"


@dataclass(frozen=True, slots=True)
class Shape:
    """A labelled vertex. ``label`` is HTML-stripped and whitespace-collapsed (``<br>`` becomes `` · ``); a vertex with
    an empty label is not a ``Shape``. ``group`` is the ``id`` of the containing group shape, else ``None``.
    ``layer`` is the layer name, ``None`` when the page has one layer. ``x`` and ``y`` are the absolute geometry (the
    sum over the parents). ``props`` are the ``UserObject``/``object`` attributes other than ``label``,
    ``placeholders`` and ``id``, as ``(key, value)`` pairs in source order."""

    id: str
    label: str
    role: Role
    group: str | None
    layer: str | None
    x: float
    y: float
    props: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Arrow:
    """An edge. ``source`` and ``target`` are shape ids, ``None`` when the end is missing, unlabelled or not a ``Shape``
    (the Markdown names such an end ``(unlabelled <role>)``). ``label`` may be empty."""

    source: str | None
    target: str | None
    label: str


@dataclass(frozen=True, slots=True)
class Page:
    """One ``<diagram>``. ``shapes`` are in output order (``y``, then ``x``; the order of the source breaks ties),
    ``arrows`` in source order, ``layers`` the layer names in source order (one name or none for a single layer).
    ``original`` is the single-page mxfile of this page (that ``<diagram>``, serialized again, UTF-8): what a link
    import stores as the original of the page's document, so each page has its own content hash (``page_original``)."""

    id: str
    name: str
    shapes: tuple[Shape, ...]
    arrows: tuple[Arrow, ...]
    layers: tuple[str, ...] = ()
    original: bytes = b""


@dataclass(frozen=True, slots=True)
class Diagram:
    """A decoded file or link. ``name`` is the file or diagram name (may be empty; the caller falls back to the file
    name or the host). ``pages`` are in source order, at most ``MAX_PAGES``. ``capped`` is true when a cap dropped
    pages or cells."""

    name: str
    pages: tuple[Page, ...]
    capped: bool = False


def page_original(page: Page) -> bytes:
    """The single-page mxfile of ``page`` (media type ``application/vnd.jgraph.mxfile``), for the importer to store."""

    return page.original
