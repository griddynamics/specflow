"""XML and text helpers of the diagrams decoder (stdlib only): the safety rules live here."""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from file_parsers.diagrams.model import (
    MAX_CELLS_PER_PAGE,
    MAX_INPUT_BYTES,
    MAX_PAGES,
    MAX_XML_BYTES,
    DiagramDecodeError,
)

_FORBIDDEN = re.compile(r"<!(?:doctype|entity)", re.IGNORECASE)
# the outer svg of a ``.drawio.svg`` may carry draw.io's own DOCTYPE (an external id, no internal subset); expat
# never fetches an external DTD, so there only ONE bounded DOCTYPE without an internal subset (``[``) is allowed and
# any ENTITY is refused
_DOCTYPE = "<!doctype"
_ENTITY = "<!entity"
_MAX_DOCTYPE = 2048
WRAPPERS = ("UserObject", "object")
_MAX_DEPTH = 64
_FEED = 64 * 1024


@dataclass(slots=True)
class Budget:
    """What is left of ``MAX_XML_BYTES`` for everything inflated while decoding one input."""

    remaining: int = MAX_XML_BYTES


@dataclass(frozen=True, slots=True)
class Loaded:
    root: ET.Element
    capped: bool


def refuse_dtd(text: str) -> str:
    if _FORBIDDEN.search(text):
        raise DiagramDecodeError("not_a_diagram")
    return text


def refuse_svg_dtd(text: str) -> str:
    """``text`` when it has no ENTITY and at most one DOCTYPE with no internal subset; every step is linear."""

    lowered = text.lower()
    if _ENTITY in lowered:
        raise DiagramDecodeError("not_a_diagram")
    start = lowered.find(_DOCTYPE)
    if start < 0:
        return text
    if lowered.find(_DOCTYPE, start + 1) >= 0:
        raise DiagramDecodeError("not_a_diagram")
    quote = ""
    for char in lowered[start + len(_DOCTYPE) : start + _MAX_DOCTYPE]:
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "[":
            raise DiagramDecodeError("not_a_diagram")
        elif char == ">":
            return text
    raise DiagramDecodeError("not_a_diagram")  # no end within the bound


def inflate(data: bytes, budget: Budget, *, wbits: int) -> bytes:
    """Inflate at most ``budget.remaining`` bytes; more is ``too_large``, a broken stream ``not_a_diagram``."""

    stream = zlib.decompressobj(wbits)
    try:
        out = stream.decompress(data, budget.remaining + 1)
    except zlib.error:
        raise DiagramDecodeError("not_a_diagram") from None
    if len(out) > budget.remaining:
        raise DiagramDecodeError("too_large")
    budget.remaining -= len(out)
    return out


def decode_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise DiagramDecodeError("not_a_diagram") from None


def decode_compressed(text: str, budget: Budget) -> str:
    """base64, raw inflate, then decodeURIComponent (the text of a compressed ``<diagram>`` or link)."""

    packed = "".join(text.split())
    if len(packed) > MAX_INPUT_BYTES:
        raise DiagramDecodeError("too_large")
    packed += "=" * (-len(packed) % 4)
    try:
        raw = base64.b64decode(packed, validate=True)
    except (binascii.Error, ValueError):
        raise DiagramDecodeError("not_a_diagram") from None
    return unquote(decode_utf8(inflate(raw, budget, wbits=-15)))


def text_to_xml(text: str, budget: Budget) -> str:
    """Plain XML as it is, URL-encoded XML unescaped, anything else read as compressed."""

    if len(text) > MAX_XML_BYTES:
        raise DiagramDecodeError("too_large")
    if not text.lstrip().startswith("<"):
        text = unquote(text)
        if not text.lstrip().startswith("<"):
            text = decode_compressed(text, budget)
    if len(text) > MAX_XML_BYTES:
        raise DiagramDecodeError("too_large")
    return refuse_dtd(text)


class _Counter:
    """Depth, page and cell counts while parsing; clears what is over a cap as it closes."""

    def __init__(self) -> None:
        self.capped = False
        self.depth = self.pages = self.cells = 0
        self._tags: list[str] = []

    def start(self, elem: ET.Element) -> None:
        self._tags.append(elem.tag)
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise DiagramDecodeError("not_a_diagram")
        if elem.tag == "diagram":
            self.pages += 1
        if elem.tag in ("diagram", "mxGraphModel"):
            self.cells = 0

    def end(self, elem: ET.Element) -> None:
        self.depth -= 1
        self._tags.pop()
        inner = elem.tag == "mxCell" and bool(self._tags) and self._tags[-1] in WRAPPERS
        if elem.tag in WRAPPERS or (elem.tag == "mxCell" and not inner):  # a wrapper counts once, with its mxCell
            self.cells += 1
            over = self.cells > MAX_CELLS_PER_PAGE
        else:
            over = elem.tag == "diagram" and self.pages > MAX_PAGES
        if over:
            self.capped = True
            elem.clear()


def iter_events(text: str, events: tuple[Literal["start", "end"], ...]) -> Iterator[tuple[str, ET.Element]]:
    """Parse events of ``text`` fed in chunks, so a consumer that stops early stops the work too."""

    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=events)
    for start in range(0, len(text), _FEED):
        parser.feed(text[start : start + _FEED])
        yield from _elements(parser)
    parser.close()
    yield from _elements(parser)


def _elements(parser: ET.XMLPullParser[ET.Element]) -> Iterator[tuple[str, ET.Element]]:
    for event in parser.read_events():
        elem = event[-1]
        if isinstance(elem, ET.Element):
            yield event[0], elem


def load(text: str) -> Loaded:
    """Parse with ``iterparse``: depth is bounded, cells and pages beyond their caps are cleared as they close.

    The DTD and entity refusal above is what makes the stdlib parser safe here (expat never loads an external DTD).
    """

    refuse_dtd(text)
    root: ET.Element | None = None
    count = _Counter()
    try:
        for event, elem in iter_events(text, ("start", "end")):
            if event == "end":
                count.end(elem)
                continue
            if root is None:
                root = elem
            count.start(elem)
    except ET.ParseError:
        raise DiagramDecodeError("not_a_diagram") from None
    if root is None:
        raise DiagramDecodeError("not_a_diagram")
    return Loaded(root, count.capped)
