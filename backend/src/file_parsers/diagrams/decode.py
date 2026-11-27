"""Decode a diagrams.net file or link payload into a ``Diagram`` (stdlib only).

Both functions are pure and deterministic: no network, no files, no logging, and no input content in any error.
Both raise ``DiagramDecodeError`` and nothing else for bad input. Safety rules: refuse any input that contains
``<!DOCTYPE`` or ``<!ENTITY`` before parsing; cap the base64 length before decoding, the PNG chunk length and the
inflate output (``zlib.decompressobj(-15).decompress(data, MAX_XML_BYTES)``); count cells while parsing.
"""

from __future__ import annotations

import zlib
from xml.etree import ElementTree as ET

from file_parsers.diagrams._page import build_page
from file_parsers.diagrams._png import embedded_text
from file_parsers.diagrams._xml import Budget, decode_utf8, iter_events, load, refuse_svg_dtd, text_to_xml
from file_parsers.diagrams.model import (
    MAX_INPUT_BYTES,
    MAX_PAGES,
    MAX_XML_BYTES,
    Diagram,
    DiagramDecodeError,
    Page,
)

_SVG_MEDIA = "application/vnd.graphico.drawio-svg"
_PNG_MEDIA = "application/vnd.graphico.drawio-png"
_UNEXPECTED = (ValueError, RecursionError, MemoryError, ET.ParseError, zlib.error)  # nothing else leaves


def decode(data: bytes, *, hint: str) -> Diagram:
    """Decode the bytes of a diagram file.

    ``hint`` is the file name or the stored media type, lower-cased by the caller or not: the form is chosen by it
    (a name ending ``.drawio.svg`` or the media type ``application/vnd.graphico.drawio-svg`` reads the ``content``
    attribute of ``<svg>``; ``.drawio.png`` or ``application/vnd.graphico.drawio-png`` reads the ``tEXt`` chunk
    ``mxfile`` or the ``zTXt`` chunk ``mxGraphModel``; anything else is read as ``.drawio`` XML, an ``<mxfile>`` or a
    bare ``<mxGraphModel>`` with plain or compressed ``<diagram>`` text). ``Diagram.name`` is empty for a file.
    Raises ``DiagramDecodeError("too_large")`` over ``MAX_INPUT_BYTES`` or ``MAX_XML_BYTES`` and
    ``DiagramDecodeError("not_a_diagram")`` for anything that is not a diagram.
    """

    try:
        return _decode_file(data, hint)
    except DiagramDecodeError:
        raise
    except _UNEXPECTED:
        raise DiagramDecodeError("not_a_diagram") from None


def decode_link_payload(payload: str) -> Diagram:
    """Decode the text after ``#R`` of a diagrams.net link (the caller strips the ``#R`` and the host).

    The payload is ``decodeURIComponent``-ed; when the result does not start with ``<`` it is compressed (base64, raw
    inflate, ``decodeURIComponent``). The result is an ``<mxfile>`` or an ``<mxGraphModel>``. The same errors and
    caps as ``decode``.
    """

    try:
        return _decode_payload(payload)
    except DiagramDecodeError:
        raise
    except _UNEXPECTED:
        raise DiagramDecodeError("not_a_diagram") from None


def _decode_file(data: bytes, hint: str) -> Diagram:
    if len(data) > MAX_INPUT_BYTES:
        raise DiagramDecodeError("too_large")
    budget = Budget()
    name = hint.strip().lower()
    if name.endswith(".drawio.svg") or name.startswith(_SVG_MEDIA):
        text = _svg_content(data)
    elif name.endswith(".drawio.png") or name.startswith(_PNG_MEDIA):
        text = embedded_text(data, budget)
    else:
        text = _file_text(data)
    return _diagram(text_to_xml(text, budget), budget)


def _decode_payload(payload: str) -> Diagram:
    if len(payload) > MAX_INPUT_BYTES:
        raise DiagramDecodeError("too_large")
    budget = Budget()
    return _diagram(text_to_xml(payload, budget), budget)


def _file_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise DiagramDecodeError("not_a_diagram") from None
    return decode_utf8(data.removeprefix(b"\xef\xbb\xbf"))


def _svg_content(data: bytes) -> str:
    """The ``content`` attribute of the root ``<svg>``; only the first tag is parsed."""

    text = refuse_svg_dtd(_file_text(data))
    try:
        for _, elem in iter_events(text, ("start",)):
            content = elem.get("content")
            if elem.tag.rsplit("}", 1)[-1] != "svg" or not content:
                break
            return str(content)
    except ET.ParseError:
        pass
    raise DiagramDecodeError("not_a_diagram")


def _diagram(xml: str, budget: Budget) -> Diagram:
    if len(xml) > MAX_XML_BYTES:
        raise DiagramDecodeError("too_large")
    loaded = load(xml)
    capped = loaded.capped
    root = loaded.root
    pages: list[Page] = []
    if root.tag == "mxGraphModel":
        pages.append(build_page("page-1", "Page-1", root))
    elif root.tag == "mxfile":
        found = root.findall("diagram")
        if not found:
            raise DiagramDecodeError("not_a_diagram")
        for index, node in enumerate(found[:MAX_PAGES], 1):
            model = node.find("mxGraphModel")
            text = (node.text or "").strip()
            if model is None and text:
                inner = load(text_to_xml(text, budget))
                capped = capped or inner.capped
                if inner.root.tag != "mxGraphModel":
                    raise DiagramDecodeError("not_a_diagram")
                model = inner.root
            pages.append(build_page(node.get("id") or f"page-{index}", node.get("name") or f"Page-{index}", model))
    else:
        raise DiagramDecodeError("not_a_diagram")
    return Diagram(name="", pages=tuple(pages), capped=capped)
