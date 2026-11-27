"""Offline tests of the diagrams.net decoder: every input form, the caps and the refusals."""

from __future__ import annotations

import base64
import socket
import struct
import urllib.request
import zlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest

from file_parsers.diagrams import (
    MAX_CELLS_PER_PAGE,
    MAX_INPUT_BYTES,
    MAX_PAGES,
    MAX_XML_BYTES,
    Diagram,
    DiagramDecodeError,
    Role,
    decode,
    decode_link_payload,
    page_original,
)

FIXTURES = Path(__file__).parent / "fixtures" / "drawio"
FLOW = (FIXTURES / "flow.drawio").read_bytes()
STRUCTURE = (FIXTURES / "structure.drawio").read_bytes()


def _deflate(text: str) -> str:
    """The draw.io compression: encodeURIComponent, raw deflate, base64."""

    raw = zlib.compressobj(wbits=-15)
    packed = raw.compress(quote(text, safe="").encode()) + raw.flush()
    return base64.b64encode(packed).decode()


def _model(cells: str = '<mxCell id="a" value="A" vertex="1" parent="1"/>') -> str:
    return f'<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>{cells}</root></mxGraphModel>'


def _mxfile(*pages: str, compress: bool = False) -> str:
    body = "".join(
        f'<diagram id="d{i}" name="Page {i}">{_deflate(m) if compress else m}</diagram>' for i, m in enumerate(pages, 1)
    )
    return f"<mxfile>{body}</mxfile>"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(*chunks: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", b"\x00" * 13) + b"".join(chunks) + _chunk(b"IEND", b"")


def _shape_labels(d: Diagram, page: int = 0) -> list[str]:
    return [s.label for s in d.pages[page].shapes]


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(DiagramDecodeError) as info:
        call()
    return info.value.code


# --- forms -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("compress", [False, True])
def test_mxfile_plain_and_compressed(*, compress: bool) -> None:
    xml = _mxfile(_model(), _model('<mxCell id="b" value="B" vertex="1" parent="1"/>'), compress=compress)
    d = decode(xml.encode(), hint="x.drawio")
    assert [p.name for p in d.pages] == ["Page 1", "Page 2"]
    assert _shape_labels(d, 0) == ["A"]
    assert _shape_labels(d, 1) == ["B"]
    assert d.name == ""
    assert not d.capped


def test_bare_model() -> None:
    d = decode(_model().encode(), hint="")
    assert len(d.pages) == 1
    assert _shape_labels(d) == ["A"]


@pytest.mark.parametrize("compress", [False, True])
def test_link_payload_forms(*, compress: bool) -> None:
    xml = _mxfile(_model(), compress=compress)
    payload = _deflate(xml) if compress else quote(xml, safe="")
    d = decode_link_payload(payload)
    assert _shape_labels(d) == ["A"]
    if compress:  # a compressed payload is carried url-encoded in a link
        assert _shape_labels(decode_link_payload(quote(payload, safe=""))) == ["A"]


def test_link_payload_bare_model_compressed() -> None:
    assert _shape_labels(decode_link_payload(_deflate(_model()))) == ["A"]


def _svg(content: str) -> bytes:
    esc = content.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" content="{esc}"><g/></svg>'.encode()


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("hint", ["a.drawio.svg", "A.DRAWIO.SVG", "application/vnd.graphico.drawio-svg"])
def test_svg_content_attribute(*, compress: bool, hint: str) -> None:
    d = decode(_svg(_mxfile(_model(), compress=compress)), hint=hint)
    assert _shape_labels(d) == ["A"]


def test_svg_without_content_or_not_svg() -> None:
    assert _code(lambda: decode(b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>', hint="a.drawio.svg")) == (
        "not_a_diagram"
    )
    assert _code(lambda: decode(b"<html content='x'/>", hint="a.drawio.svg")) == "not_a_diagram"
    assert _code(lambda: decode(b"<svg", hint="a.drawio.svg")) == "not_a_diagram"


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("hint", ["a.drawio.png", "application/vnd.graphico.drawio-png"])
def test_png_text_chunk_mxfile(*, compress: bool, hint: str) -> None:
    xml = _mxfile(_model(), compress=compress)
    data = _png(_chunk(b"tEXt", b"mxfile\x00" + quote(xml, safe="").encode()))
    assert _shape_labels(decode(data, hint=hint)) == ["A"]


def test_png_ztxt_chunk_mxgraphmodel() -> None:
    body = b"mxGraphModel\x00\x00" + zlib.compress(quote(_model(), safe="").encode())
    assert _shape_labels(decode(_png(_chunk(b"zTXt", body)), hint="a.drawio.png")) == ["A"]


def test_png_chunks_before_and_other_text() -> None:
    other = _chunk(b"tEXt", b"Software\x00x")
    idat = _chunk(b"IDAT", b"\x00" * 100)
    xml = quote(_mxfile(_model()), safe="").encode()
    data = _png(other, idat, _chunk(b"tEXt", b"mxfile\x00" + xml))
    assert _shape_labels(decode(data, hint="a.drawio.png")) == ["A"]


def test_png_without_chunk() -> None:
    assert _code(lambda: decode(_png(_chunk(b"tEXt", b"Software\x00x")), hint="a.drawio.png")) == "not_a_diagram"
    assert _code(lambda: decode(b"not a png", hint="a.drawio.png")) == "not_a_diagram"


def test_png_truncated() -> None:
    data = _png(_chunk(b"tEXt", b"mxfile\x00" + quote(_mxfile(_model()), safe="").encode()))
    for cut in (10, 30, len(data) - 40, len(data) - 25):
        assert _code(lambda cut=cut: decode(data[:cut], hint="a.drawio.png")) == "not_a_diagram"


def test_png_forged_chunk_length() -> None:
    forged = struct.pack(">I", 0x7FFFFFFF) + b"tEXt" + b"mxfile\x00abc"
    assert _code(lambda: decode(b"\x89PNG\r\n\x1a\n" + forged, hint="a.drawio.png")) == "not_a_diagram"


def test_png_ztxt_bomb() -> None:
    bomb = zlib.compress(b"A" * (MAX_XML_BYTES + 1024), 9)
    data = _png(_chunk(b"zTXt", b"mxGraphModel\x00\x00" + bomb))
    assert _code(lambda: decode(data, hint="a.drawio.png")) == "too_large"


def test_bom_and_utf16() -> None:
    xml = _mxfile(_model())
    assert _shape_labels(decode(b"\xef\xbb\xbf" + xml.encode(), hint="a.drawio")) == ["A"]
    assert _shape_labels(decode(xml.encode("utf-16"), hint="a.drawio")) == ["A"]


# --- model -------------------------------------------------------------------------------------------------------


def test_flow_fixture_shapes_roles_props() -> None:
    d = decode(FLOW, hint="flow.drawio")
    page = d.pages[0]
    assert (page.id, page.name) == ("d1", "Flow")
    by = {s.id: s for s in page.shapes}
    assert [s.id for s in page.shapes] == ["start", "who", "note", "gate", "db", "doc"]  # y, then x
    assert "blank" not in by
    assert by["gate"].role is Role.DECISION
    assert by["gate"].label == 'Approved? · "Yes, I reviewed" & signed'
    assert by["db"].role is Role.STORE
    assert by["doc"].role is Role.DOCUMENT
    assert by["who"].role is Role.ACTOR
    assert by["note"].role is Role.NOTE
    assert by["note"].label == "# Legend · [click](http://x) *bold* _it_"
    assert by["start"].role is Role.STEP
    assert by["doc"].props == (("link", "https://example.com/spec?a=1&b=[2]"), ("owner", "ops"))
    assert by["who"].props == (("team", ""),)
    assert all(s.layer is None and s.group is None for s in page.shapes)


def test_flow_fixture_arrows() -> None:
    page = decode(FLOW, hint="flow.drawio").pages[0]
    got = [(a.source, a.target, a.label) for a in page.arrows]
    assert got == [
        ("start", "gate", ""),
        ("gate", "db", "yes · late"),  # the edgeLabel child cell joins its edge
        ("db", None, "to nowhere"),  # missing target
        (None, "doc", ""),  # unlabelled source
        ("doc", None, ""),  # dangling target id
    ]


def test_groups_layers_and_absolute_geometry() -> None:
    d = decode(STRUCTURE, hint="structure.drawio")
    assert len(d.pages) == 2
    page = d.pages[0]
    assert page.layers == ("Business", "Tech")
    by = {s.id: s for s in page.shapes}
    assert by["lane"].role is Role.GROUP and by["box"].role is Role.GROUP
    assert by["inner"].group == "lane" and by["inner"].layer == "Business"
    assert (by["inner"].x, by["inner"].y) == (150.0, 150.0)
    assert (by["deep"].x, by["deep"].y) == (105.0, 225.0)
    assert by["deep"].group == "box" and by["box"].group == "lane"
    assert by["api"].layer == "Tech" and by["top"].group is None
    assert [s.id for s in page.shapes][:5] == ["top", "lane", "api", "inner2", "inner"]
    assert [a.label for a in page.arrows] == ["calls", ""]


def test_unlabelled_group_gives_nearest_labelled_ancestor() -> None:
    cells = (
        '<mxCell id="g" value="G" vertex="1" parent="1"/>'
        '<mxCell id="u" value="" vertex="1" parent="g"/>'
        '<mxCell id="c" value="C" vertex="1" parent="u"><mxGeometry x="1" y="2" as="geometry"/></mxCell>'
    )
    shapes = {s.id: s for s in decode(_model(cells).encode(), hint="").pages[0].shapes}
    assert shapes["c"].group == "g"
    assert "u" not in shapes


def test_single_unnamed_layer_has_no_layers() -> None:
    page = decode(_model().encode(), hint="").pages[0]
    assert page.layers == () and page.shapes[0].layer is None


def test_parent_cycle_does_not_hang() -> None:
    cells = '<mxCell id="a" value="A" vertex="1" parent="b"/><mxCell id="b" value="B" vertex="1" parent="a"/>'
    assert len(decode(_model(cells).encode(), hint="").pages[0].shapes) == 2


def test_label_cleanup() -> None:
    cells = (
        '<mxCell id="a" value="&lt;b&gt;Hi&lt;/b&gt;&amp;nbsp;there&lt;br/&gt;&lt;br&gt;  x&amp;lt;y  " '
        'style="html=1;" vertex="1" parent="1"/>'
        '<mxCell id="b" value="line1&#10;line2 &lt;i&gt;" vertex="1" parent="1">'
        '<mxGeometry y="9" as="geometry"/></mxCell>'
        '<mxCell id="c" value="&lt;style&gt;p{}&lt;/style&gt;&lt;p&gt;one&lt;/p&gt;&lt;p&gt;two&lt;/p&gt;" '
        'style="html=1" vertex="1" parent="1"><mxGeometry y="20" as="geometry"/></mxCell>'
    )
    labels = _shape_labels(decode(_model(cells).encode(), hint=""))
    assert labels == ["Hi there · x<y", "line1 · line2 <i>", "one · two"]


# --- original ----------------------------------------------------------------------------------------------------


def test_original_is_single_page_uncompressed_mxfile() -> None:
    for compress in (False, True):
        two = _mxfile(_model(), _model('<mxCell id="b" value="B" vertex="1" parent="1"/>'), compress=compress)
        d = decode(two.encode(), hint="")
        originals = [page_original(p) for p in d.pages]
        assert originals[0] != originals[1]
        for page, original in zip(d.pages, originals, strict=True):
            assert original.startswith(b'<mxfile><diagram id="')
            assert original.count(b"<diagram ") == 1 and f'name="{page.name}"'.encode() in original
            assert b"<mxGraphModel>" in original and b"<?xml" not in original
            again = decode(original, hint="x.drawio")
            assert _shape_labels(again) == [s.label for s in page.shapes]
            assert page_original(again.pages[0]) == original  # a fixed point


def test_determinism() -> None:
    for data in (FLOW, STRUCTURE):
        a, b = decode(data, hint="x.drawio"), decode(data, hint="x.drawio")
        assert a == b
        assert [p.original for p in a.pages] == [p.original for p in b.pages]


# --- caps and refusals -------------------------------------------------------------------------------------------


def test_input_over_cap() -> None:
    assert _code(lambda: decode(b"<" + b" " * MAX_INPUT_BYTES, hint="a.drawio")) == "too_large"
    assert _code(lambda: decode_link_payload("A" * (MAX_INPUT_BYTES + 1))) == "too_large"


def test_zip_bomb_inflate_cap() -> None:
    bomb = base64.b64encode(zlib.compress(b"A" * (MAX_XML_BYTES + 4096), 9)[2:-4]).decode()  # raw deflate
    assert len(bomb) < MAX_INPUT_BYTES
    assert _code(lambda: decode(f'<mxfile><diagram id="a">{bomb}</diagram></mxfile>'.encode(), hint="")) == "too_large"
    assert _code(lambda: decode_link_payload(bomb)) == "too_large"


def test_inflate_budget_is_shared_between_pages() -> None:
    chunk = _deflate("<!--" + "x" * (MAX_XML_BYTES // 3) + "-->" + _model())  # five pages inflate past the budget
    xml = "<mxfile>" + "".join(f'<diagram id="d{i}">{chunk}</diagram>' for i in range(5)) + "</mxfile>"
    assert _code(lambda: decode(xml.encode(), hint="")) == "too_large"


def test_oversized_base64_refused_before_decoding() -> None:
    text = "A" * (MAX_INPUT_BYTES + 1)
    xml = f'<mxfile><diagram id="a">{text}</diagram></mxfile>'
    assert _code(lambda: decode(xml.encode(), hint="")) == "too_large"


@pytest.mark.parametrize("bad", ["<!DOCTYPE x>", "<!doctype x>", '<!DOCTYPE x [<!ENTITY a "b">]>', '<!ENTITY a "b">'])
def test_doctype_and_entity_refused(bad: str) -> None:
    xml = f"{bad}{_mxfile(_model())}"
    assert _code(lambda: decode(xml.encode(), hint="a.drawio")) == "not_a_diagram"
    assert _code(lambda: decode_link_payload(quote(xml, safe=""))) == "not_a_diagram"
    assert _code(lambda: decode(_svg(xml), hint="a.drawio.svg")) == "not_a_diagram"


DRAWIO_DOCTYPE = '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'


def _before_root(declaration: str) -> bytes:
    return _svg(_mxfile(_model())).replace(b"<svg", f"{declaration}<svg".encode(), 1)


def test_svg_with_drawio_doctype_decodes() -> None:
    for declaration in (DRAWIO_DOCTYPE, '<?xml version="1.0" encoding="UTF-8"?>\n' + DRAWIO_DOCTYPE):
        assert _shape_labels(decode(_before_root(declaration), hint="a.drawio.svg")) == ["A"]


@pytest.mark.parametrize(
    "bad",
    ['<!DOCTYPE svg [<!ENTITY a "b">]>', "<!DOCTYPE svg []>", '<!DOCTYPE svg PUBLIC "x" "y" [ ]>', '<!ENTITY a "b">'],
)
def test_svg_doctype_with_subset_or_entity_refused(bad: str) -> None:
    assert _code(lambda: decode(_before_root(bad), hint="a.drawio.svg")) == "not_a_diagram"
    assert _code(lambda: decode(_before_root(DRAWIO_DOCTYPE + bad), hint="a.drawio.svg")) == "not_a_diagram"


def test_entity_after_the_root_tag_of_an_svg_refused() -> None:
    data = _svg(_mxfile(_model())).replace(b"</svg>", b'<!ENTITY a "b"></svg>')
    assert _code(lambda: decode(data, hint="a.drawio.svg")) == "not_a_diagram"


def test_external_dtd_is_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_a: object, **_k: object) -> None:
        pytest.fail("the decoder opened a connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    assert _shape_labels(decode(_before_root(DRAWIO_DOCTYPE), hint="a.drawio.svg")) == ["A"]


def test_doctype_inside_compressed_diagram_refused() -> None:
    inner = '<!DOCTYPE x [<!ENTITY a "b">]>' + _model()
    xml = f'<mxfile><diagram id="a">{_deflate(inner)}</diagram></mxfile>'
    assert _code(lambda: decode(xml.encode(), hint="")) == "not_a_diagram"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello",
        b"<a/>",
        b"<mxfile/>",
        b"<mxfile><diagram/></mxfile>x",
        b"<mxfile><diagram id='a'>!!notbase64!!</diagram></mxfile>",
        b"<mxfile><diagram id='a'>aGVsbG8=</diagram></mxfile>",  # base64, not deflate
        b"<mxGraphModel><root></mxGraphModel>",
        b"\xff\xfe\x00",
        b"\x80\x81",
        b"<" * 5000,
        b"<a>" * 200,
    ],
)
def test_garbage_is_not_a_diagram(data: bytes) -> None:
    assert _code(lambda: decode(data, hint="x.drawio")) == "not_a_diagram"
    assert _code(lambda: decode_link_payload(data.decode("latin-1"))) in ("not_a_diagram", "too_large")


def test_error_never_carries_input() -> None:
    marker = "SECRET-LABEL"
    with pytest.raises(DiagramDecodeError) as info:
        decode(f"<mxfile><x>{marker}</mxfile>".encode(), hint="")
    assert marker not in str(info.value) and marker not in repr(info.value.__cause__)


def test_too_many_cells_dropped_and_capped() -> None:
    cells = "".join(f'<mxCell id="c{i}" value="n{i}" vertex="1" parent="1"/>' for i in range(MAX_CELLS_PER_PAGE + 25))
    d = decode(_model(cells).encode(), hint="")
    assert d.capped
    assert len(d.pages[0].shapes) == MAX_CELLS_PER_PAGE - 2  # cells 0 and 1 are the root and the layer
    assert b"n4998" not in page_original(d.pages[0])


def test_too_many_cells_in_compressed_page() -> None:
    cells = "".join(f'<mxCell id="c{i}" value="n{i}" vertex="1" parent="1"/>' for i in range(MAX_CELLS_PER_PAGE + 5))
    d = decode(_mxfile(_model(cells), compress=True).encode(), hint="")
    assert d.capped and len(d.pages[0].shapes) == MAX_CELLS_PER_PAGE - 2


def test_cells_at_the_cap_are_not_capped() -> None:
    cells = "".join(f'<mxCell id="c{i}" value="n{i}" vertex="1" parent="1"/>' for i in range(MAX_CELLS_PER_PAGE - 2))
    d = decode(_model(cells).encode(), hint="")
    assert not d.capped and len(d.pages[0].shapes) == MAX_CELLS_PER_PAGE - 2


@pytest.mark.parametrize("compress", [False, True])
def test_too_many_pages_dropped_and_capped(*, compress: bool) -> None:
    xml = _mxfile(*[_model()] * (MAX_PAGES + 3), compress=compress)
    d = decode(xml.encode(), hint="")
    assert d.capped and len(d.pages) == MAX_PAGES
    assert d.pages[-1].name == f"Page {MAX_PAGES}"


def test_pages_at_the_cap_are_not_capped() -> None:
    d = decode(_mxfile(*[_model()] * MAX_PAGES).encode(), hint="")
    assert not d.capped and len(d.pages) == MAX_PAGES
