"""A minimal PNG chunk reader: finds the draw.io text chunk (stdlib only)."""

from __future__ import annotations

from file_parsers.diagrams._xml import Budget, decode_utf8, inflate
from file_parsers.diagrams.model import DiagramDecodeError

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def embedded_text(data: bytes, budget: Budget) -> str:
    """The text of the ``tEXt`` chunk ``mxfile`` or the ``zTXt`` chunk ``mxGraphModel``; ``not_a_diagram`` without one.

    A chunk that claims more bytes than the file holds (cut short or forged) is ``not_a_diagram``; chunks are skipped
    by their length and never copied unless they are the wanted one.
    """

    if not data.startswith(_SIGNATURE):
        raise DiagramDecodeError("not_a_diagram")
    pos = len(_SIGNATURE)
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        pos += 8
        if length > len(data) - pos - 4:
            raise DiagramDecodeError("not_a_diagram")
        body = data[pos : pos + length]
        pos += length + 4
        if kind == b"tEXt":
            key, sep, value = body.partition(b"\x00")
            if sep and key == b"mxfile":
                return value.decode("latin-1")
        elif kind == b"zTXt":
            key, sep, value = body.partition(b"\x00")
            if sep and key == b"mxGraphModel" and value[:1] == b"\x00":
                return decode_utf8(inflate(value[1:], budget, wbits=15))
        elif kind == b"IEND":
            break
    raise DiagramDecodeError("not_a_diagram")
