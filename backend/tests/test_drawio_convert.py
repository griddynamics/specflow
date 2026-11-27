"""draw.io in ``file_to_markdown`` and in the Drive routing: the decoder is monkeypatched, Docling is never loaded."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from drive.categories import Category, ProcessingClass, categorize, drive_format
from file_parsers import convert
from file_parsers.convert import file_to_markdown
from file_parsers.diagrams import DiagramDecodeError
from file_parsers.diagrams.fake import fake_diagram
from file_parsers.formats import (
    DRAWIO_MEDIA_TYPE,
    DRAWIO_PNG_MEDIA_TYPE,
    DRAWIO_SVG_MEDIA_TYPE,
)
from file_parsers.limits import DocumentParseFailedError, DocumentTooLargeError
from temporal.sources.activities import DriveImportActivities

TYPES = [DRAWIO_MEDIA_TYPE, DRAWIO_SVG_MEDIA_TYPE, DRAWIO_PNG_MEDIA_TYPE]


@pytest.fixture(autouse=True)
def no_docling(monkeypatch):
    def boom():
        raise AssertionError("Docling must not be loaded for a diagram")

    monkeypatch.setattr(convert, "default_engine", boom)


@pytest.mark.parametrize("media_type", TYPES)
def test_each_format_is_decoded_and_rendered(media_type, tmp_path, monkeypatch):
    seen = {}

    def fake_decode(data, *, hint):
        seen.update(data=data, hint=hint)
        return fake_diagram()

    monkeypatch.setattr(convert, "decode", fake_decode)
    monkeypatch.setattr(convert, "file_markdown", lambda diagram: f"# {diagram.name}\n")
    path = tmp_path / "f.bin"
    path.write_bytes(b"payload")

    parsed = file_to_markdown(path, media_type)

    assert (parsed.markdown, parsed.parser) == ("# Flow\n", "drawio-convert")
    assert seen == {"data": b"payload", "hint": media_type}


@pytest.mark.parametrize(
    ("code", "error"), [("not_a_diagram", DocumentParseFailedError), ("too_large", DocumentTooLargeError)]
)
def test_decode_errors_map_to_the_parse_errors(code, error, tmp_path, monkeypatch):
    def fail(data, *, hint):
        raise DiagramDecodeError(code)

    monkeypatch.setattr(convert, "decode", fail)
    path = tmp_path / "f.drawio"
    path.write_bytes(b"x")
    with pytest.raises(error):
        file_to_markdown(path, DRAWIO_MEDIA_TYPE)


def test_a_file_over_the_input_cap_is_too_large_before_decoding(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "MAX_INPUT_BYTES", 4)

    def no_decode(data, *, hint):
        pytest.fail("decode must not run")

    monkeypatch.setattr(convert, "decode", no_decode)
    path = tmp_path / "f.drawio"
    path.write_bytes(b"12345")
    with pytest.raises(DocumentTooLargeError):
        file_to_markdown(path, DRAWIO_MEDIA_TYPE)


def test_a_tiny_real_drawio_end_to_end(tmp_path):
    xml = (
        b'<mxfile><diagram id="p1" name="Main"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        b'<mxCell id="a" value="Start" vertex="1" parent="1">'
        b'<mxGeometry x="0" y="0" width="80" height="40" as="geometry"/>'
        b"</mxCell></root></mxGraphModel></diagram></mxfile>"
    )
    path = tmp_path / "f.drawio"
    path.write_bytes(xml)
    parsed = file_to_markdown(path, DRAWIO_MEDIA_TYPE)
    assert parsed.parser == "drawio-convert"
    assert "Start" in parsed.markdown


# --- Drive ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "name", "media_type"),
    [
        ("image/png", "Flow.drawio.png", DRAWIO_PNG_MEDIA_TYPE),
        ("image/png", "FLOW.DrawIO.PNG", DRAWIO_PNG_MEDIA_TYPE),
        ("image/svg+xml", "Flow.drawio.svg", DRAWIO_SVG_MEDIA_TYPE),
        ("application/octet-stream", "Flow.drawio", DRAWIO_MEDIA_TYPE),
        ("text/xml", "Flow.drawio", DRAWIO_MEDIA_TYPE),
        ("application/vnd.jgraph.mxfile", "Flow", DRAWIO_MEDIA_TYPE),
        ("application/vnd.jgraph.mxfile", None, DRAWIO_MEDIA_TYPE),
        ("application/vnd.jgraph.mxfile.realtime", "Flow", DRAWIO_MEDIA_TYPE),
        ("application/vnd.jgraph.mxfile.rtlegacy", "Flow", DRAWIO_MEDIA_TYPE),
    ],
)
def test_drive_categorizes_drawio_as_processed(mime, name, media_type):
    assert categorize(mime, name) == (Category.OTHER_FILES, ProcessingClass.PROCESSED)
    assert drive_format(mime, name).media_type == media_type


@pytest.mark.parametrize(
    ("mime", "name", "expected"),
    [
        ("image/png", "photo.png", (Category.IMAGES, ProcessingClass.STORED_ONLY)),
        ("image/png", None, (Category.IMAGES, ProcessingClass.STORED_ONLY)),
        ("image/svg+xml", "logo.svg", (Category.IMAGES, ProcessingClass.STORED_ONLY)),
        ("application/octet-stream", "x.drawio.bak", (Category.OTHER_FILES, ProcessingClass.STORED_ONLY)),
        ("application/octet-stream", ".drawio", (Category.OTHER_FILES, ProcessingClass.STORED_ONLY)),
        ("application/vnd.google-apps.document", "x.drawio", (Category.GOOGLE_DOCS, ProcessingClass.PROCESSED)),
    ],
)
def test_other_names_keep_their_mime_category(mime, name, expected):
    assert categorize(mime, name) == expected


def _row(mime, name):
    category, klass = categorize(mime, name)
    return SimpleNamespace(category=category.value, processing_class=klass.value, mime_type=mime, name=name)


def test_kind_of_a_drawio_png_is_document():
    assert DriveImportActivities._kind(_row("image/png", "Flow.drawio.png")) == "document"
    assert DriveImportActivities._kind(_row("image/png", "photo.png")) == "stored"
