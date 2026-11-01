"""Upload API for every accepted format: types, aliases, first bytes, byte caps.

Both routes (legacy ``POST /documents`` and the batch ``POST /sources/{sid}/files``) share one path:
the file extension picks the canonical type, the header must agree (or be a browser quirk of the
table), the stream checks the first bytes and counts the bytes against the cap of the format.

Sequence of one accepted upload (the stream never holds more than one buffer):

    client -> router: body, ?filename=notes.md, Content-Type: ""
    router -> uploads.accepted_format: ("", "notes.md") -> Markdown spec     (else 415 before any byte)
    router -> stream_upload: Content-Length > cap? -> 413 before the first byte
    stream_upload -> _Stream.feed: count bytes (> cap -> 413, writer aborted), check head, open writer
    stream_upload -> store: key projects/{pid}/documents/{did}/original.md, content type text/markdown
    router -> db: media_type stored, item category by format
"""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from api import uploads
from db import batches
from file_parsers.formats import FORMATS, DocumentFormat
from specflow_api_support import PDF, bearer
from storage.base import CHUNK_SIZE, MiB

ALICE = "alice@example.com"
ZIP = b"PK\x03\x04" + b"\x00" * 30 + b"synthetic ooxml body"
BODIES = {
    DocumentFormat.PDF: PDF,
    DocumentFormat.DOCX: ZIP,
    DocumentFormat.PPTX: ZIP,
    DocumentFormat.XLSX: ZIP,
    DocumentFormat.HTML: b"<html><body><h1>Title</h1></body></html>",
    DocumentFormat.MARKDOWN: "# Titel\n\nZażółć gęślą jaźń\n".encode(),
    DocumentFormat.CSV: b"a,b\n1,2\n",
}
CATEGORY = {
    DocumentFormat.PDF: "pdf",
    DocumentFormat.DOCX: "word",
    DocumentFormat.PPTX: "powerpoint",
    DocumentFormat.XLSX: "excel",
    DocumentFormat.HTML: "other_files",
    DocumentFormat.MARKDOWN: "other_files",
    DocumentFormat.CSV: "other_files",
}
CANONICAL = [pytest.param(spec, id=spec.extension) for spec in FORMATS]


async def project(harness) -> str:
    response = await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer(ALICE))
    return response.json()["id"]


async def legacy(harness, pid, body, name, content_type):
    headers = {
        **bearer(ALICE),
        **({} if content_type is None else {"Content-Type": content_type}),
    }
    return await harness.client.post(
        f"/api/v1/projects/{pid}/documents?filename={name}",
        content=body,
        headers=headers,
    )


async def batch(harness, pid, body, name, content_type, *, sid=None):
    if sid is None:
        sid = (
            await harness.client.post(
                f"/api/v1/projects/{pid}/sources/uploads",
                json={"expected_files": 3},
                headers=bearer(ALICE),
            )
        ).json()["source"]["id"]
    headers = {
        **bearer(ALICE),
        **({} if content_type is None else {"Content-Type": content_type}),
    }
    return await harness.client.post(
        f"/api/v1/projects/{pid}/sources/{sid}/files?filename={name}",
        content=body,
        headers=headers,
    )


async def chunks(data: bytes, size: int):
    for start in range(0, len(data), size):
        yield data[start : start + size]


def code(response) -> str:
    return response.json()["error"]["code"]


def nothing_kept(harness) -> bool:
    return not harness.store.objects and not harness.repo.documents


# ---- every accepted format ------------------------------------------------------------------


@pytest.mark.parametrize("spec", CANONICAL)
async def test_the_legacy_upload_of_each_format_stores_the_canonical_type_and_key(harness, spec):
    pid, body = await project(harness), BODIES[spec.format]
    response = await legacy(harness, pid, body, f"file{spec.extension}", spec.media_type)
    assert response.status_code == 201
    document = response.json()["document"]
    key = f"projects/{pid}/documents/{document['id']}/original{spec.extension}"
    assert harness.store.objects == {key: body}
    assert harness.store.content_types[key] == spec.media_type
    row = harness.repo.documents[UUID(document["id"])]
    assert (row.media_type, row.original_key, row.size_bytes) == (
        spec.media_type,
        key,
        len(body),
    )
    assert row.content_hash == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("spec", CANONICAL)
async def test_the_batch_upload_of_each_format_stores_the_type_key_and_category(harness, spec):
    pid, body = await project(harness), BODIES[spec.format]
    response = await batch(harness, pid, body, f"file{spec.extension}", spec.media_type)
    assert response.status_code == 201
    assert response.json()["file"]["category"] == CATEGORY[spec.format]
    (document,) = harness.repo.documents.values()
    assert document.media_type == spec.media_type
    assert document.original_key == f"projects/{pid}/documents/{document.id}/original{spec.extension}"
    assert list(harness.store.objects) == [document.original_key]


async def test_the_pdf_key_is_still_original_pdf(harness):
    pid = await project(harness)
    response = await legacy(harness, pid, PDF, "a.pdf", "application/pdf")
    assert f"projects/{pid}/documents/{response.json()['document']['id']}/original.pdf" in harness.store.objects


# ---- browser quirks and the extension -------------------------------------------------------


QUIRKS = [
    (
        "notes.md",
        None,
        "text/markdown",
        DocumentFormat.MARKDOWN,
    ),  # no Content-Type at all
    ("notes.md", "", "text/markdown", DocumentFormat.MARKDOWN),  # an empty type
    ("notes.md", "text/plain", "text/markdown", DocumentFormat.MARKDOWN),
    ("NOTES.MD", "", "text/markdown", DocumentFormat.MARKDOWN),  # upper case extension
    (
        "Notes.Md",
        "TEXT/Markdown; charset=utf-8",
        "text/markdown",
        DocumentFormat.MARKDOWN,
    ),
    (
        "table.csv",
        "application/vnd.ms-excel",
        "text/csv",
        DocumentFormat.CSV,
    ),  # Windows
    ("table.csv", "text/plain", "text/csv", DocumentFormat.CSV),
    ("TABLE.CSV", "text/csv; charset=utf-8", "text/csv", DocumentFormat.CSV),
    (
        "REPORT.PDF",
        "Application/PDF; charset=binary",
        "application/pdf",
        DocumentFormat.PDF,
    ),
    ("page.HTML", "text/html; charset=utf-8", "text/html", DocumentFormat.HTML),
]


@pytest.mark.parametrize(("name", "header", "media_type", "format_"), QUIRKS)
@pytest.mark.parametrize("route", [legacy, batch], ids=["legacy", "batch"])
async def test_browser_quirks_are_accepted_and_stored_with_the_canonical_type(
    harness, route, *, name, header, media_type, format_
):
    pid = await project(harness)
    response = await route(harness, pid, BODIES[format_], name, header)
    assert response.status_code == 201
    (document,) = harness.repo.documents.values()
    assert document.media_type == media_type
    assert document.original_key.endswith(f"/original{next(s.extension for s in FORMATS if s.format == format_)}")


async def test_a_known_extension_decides_over_a_trusted_but_different_header(harness):
    pid = await project(harness)
    response = await legacy(harness, pid, PDF, "notes.pdf", "text/markdown")
    assert response.status_code == 415 and nothing_kept(harness)


async def test_an_unknown_extension_needs_a_trusted_header_type(harness):
    pid = await project(harness)
    assert (await legacy(harness, pid, BODIES[DocumentFormat.MARKDOWN], "notes.txt", "")).status_code == 415
    assert (await legacy(harness, pid, BODIES[DocumentFormat.MARKDOWN], "notes.txt", "text/plain")).status_code == 415
    assert nothing_kept(harness)
    accepted = await legacy(harness, pid, BODIES[DocumentFormat.MARKDOWN], "notes.txt", "text/markdown")
    assert accepted.status_code == 201
    (document,) = harness.repo.documents.values()
    assert document.media_type == "text/markdown" and document.original_key.endswith("/original.md")


async def test_a_filename_cannot_choose_the_key(harness):
    pid = await project(harness)
    response = await legacy(harness, pid, PDF, "..%2F..%2Fx.pdf", "application/pdf")
    assert response.status_code == 201
    (key,) = harness.store.objects
    assert key == f"projects/{pid}/documents/{response.json()['document']['id']}/original.pdf"


# ---- 415 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "header"),
    [
        ("old.doc", "application/msword"),
        (
            "old.doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("old.ppt", "application/vnd.ms-powerpoint"),
        (
            "old.xls",
            "application/vnd.ms-excel",
        ),  # the CSV quirk type must not make .xls a CSV
        ("old.XLS", "application/vnd.ms-excel"),
        ("data.json", "application/json"),
        ("image.png", "image/png"),
        ("archive.zip", "application/zip"),
        ("noext", "application/octet-stream"),
        ("report.docx", "application/pdf"),
        ("report.docx", ""),
        ("page.html", "text/plain"),  # the text/plain quirk is for .md and .csv only
        ("sheet.xlsx", "application/vnd.ms-excel"),
    ],
)
@pytest.mark.parametrize("route", [legacy, batch], ids=["legacy", "batch"])
async def test_an_unsupported_file_is_415_unsupported_format_and_leaves_nothing(harness, route, name, header):
    pid = await project(harness)
    response = await route(harness, pid, PDF, name, header)
    assert response.status_code == 415 and code(response) == "unsupported_format"
    assert nothing_kept(harness)


async def test_the_415_message_names_every_accepted_type(harness):
    pid = await project(harness)
    message = (await legacy(harness, pid, PDF, "old.doc", "application/msword")).json()["error"]["message"]
    for spec in FORMATS:
        assert spec.media_type in message and spec.extension in message


async def test_a_batch_415_is_answered_before_the_batch_state_matters(harness):
    pid = await project(harness)
    response = await batch(harness, pid, PDF, "old.doc", "application/msword")
    assert response.status_code == 415 and not harness.batches.items


# ---- first bytes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "header", "body"),
    [
        ("a.pdf", "application/pdf", b"GIF89a not a pdf"),
        ("a.pdf", "application/pdf", b"PK\x03\x04zip"),  # an Office file named .pdf
        ("a.pdf", "application/pdf", b"%PD"),
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            PDF,
        ),
        (
            "a.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            b"PK\x03",
        ),
        (
            "a.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            b"<html></html>",
        ),
        ("a.md", "", PDF),  # a PDF named .md
        ("a.md", "", b"PK\x03\x04zip"),
        ("a.csv", "text/csv", b"a,b\n\x00\x01\x02"),  # a NUL byte: binary, not text
        ("a.html", "text/html", b"<p>\xff\xfe broken</p>"),  # not UTF-8
        ("a.md", "", b"\xc3"),  # a cut multi-byte character at the end of the body
    ],
)
@pytest.mark.parametrize("route", [legacy, batch], ids=["legacy", "batch"])
async def test_a_body_that_does_not_fit_its_extension_is_415_and_leaves_nothing(harness, route, name, header, body):
    pid = await project(harness)
    response = await route(harness, pid, body, name, header)
    assert response.status_code == 415 and code(response) == "unsupported_format"
    assert nothing_kept(harness)


@pytest.mark.parametrize("route", [legacy, batch], ids=["legacy", "batch"])
async def test_an_empty_body_is_400_empty_file(harness, route):
    pid = await project(harness)
    response = await route(harness, pid, b"", "a.md", "")
    assert response.status_code == 400 and code(response) == "empty_file"
    assert nothing_kept(harness)


async def test_a_one_byte_text_file_is_accepted(harness):
    pid = await project(harness)
    assert (await legacy(harness, pid, b"#", "a.md", "")).status_code == 201


async def test_utf8_with_a_bom_and_a_multibyte_character_at_the_chunk_edge_is_accepted(
    harness,
):
    pid = await project(harness)
    text = b"\xef\xbb\xbf" + ("ż" * (uploads.TEXT_HEAD_BYTES * 2)).encode()
    pieces = chunks(text, uploads.TEXT_HEAD_BYTES + 1)  # the first piece ends inside a character
    response = await legacy(harness, pid, pieces, "a.md", "")
    assert response.status_code == 201
    assert next(iter(harness.store.objects.values())) == text


async def test_a_missing_filename_is_400_before_the_type_is_looked_at(harness):
    pid = await project(harness)
    response = await harness.client.post(
        f"/api/v1/projects/{pid}/documents",
        content=PDF,
        headers={**bearer(ALICE), "Content-Type": "application/pdf"},
    )
    assert response.status_code == 400 and code(response) == "missing_filename"


# ---- 413: byte caps counted while streaming ---------------------------------------------------


def test_the_byte_caps_are_the_n2_values():
    caps = {format_: size // MiB for format_, size in uploads.MAX_BYTES.items()}
    assert caps == {
        DocumentFormat.PDF: 100,
        DocumentFormat.DOCX: 50,
        DocumentFormat.PPTX: 50,
        DocumentFormat.XLSX: 50,
        DocumentFormat.HTML: 10,
        DocumentFormat.MARKDOWN: 10,
        DocumentFormat.CSV: 10,
    }
    assert set(uploads.MAX_BYTES) == {spec.format for spec in FORMATS}


@pytest.mark.parametrize("spec", CANONICAL)
@pytest.mark.parametrize("route", [legacy, batch], ids=["legacy", "batch"])
async def test_a_body_over_the_cap_is_413_counted_while_streaming_and_leaves_nothing(harness, monkeypatch, route, spec):
    monkeypatch.setitem(uploads.MAX_BYTES, spec.format, 5000)
    pid = await project(harness)
    body = BODIES[spec.format]
    body = body + b"a" * (5001 - len(body))  # one byte over the cap
    response = await route(
        harness, pid, chunks(body, 700), f"file{spec.extension}", spec.media_type
    )  # no length header
    assert response.status_code == 413 and code(response) == "file_too_large"
    assert response.json()["error"]["details"] == {"max_bytes": 5000}
    assert nothing_kept(harness)


@pytest.mark.parametrize("spec", CANONICAL)
async def test_a_body_exactly_at_the_cap_is_accepted(harness, monkeypatch, spec):
    monkeypatch.setitem(uploads.MAX_BYTES, spec.format, 5000)
    pid = await project(harness)
    body = BODIES[spec.format]
    body = body + b"a" * (5000 - len(body))
    response = await legacy(harness, pid, chunks(body, 700), f"file{spec.extension}", spec.media_type)
    assert response.status_code == 201


async def test_an_oversized_declared_length_is_413_before_any_byte_is_read(harness, monkeypatch):
    monkeypatch.setitem(uploads.MAX_BYTES, DocumentFormat.CSV, 100)
    pid = await project(harness)
    opened: list[str] = []
    original = harness.store.open_writer

    async def spy(key, **kw):
        opened.append(key)
        return await original(key, **kw)

    monkeypatch.setattr(harness.store, "open_writer", spy)
    response = await legacy(harness, pid, b"a,b\n" * 100, "t.csv", "text/csv")  # httpx sends Content-Length: 400
    assert response.status_code == 413 and code(response) == "file_too_large"
    assert opened == [] and nothing_kept(harness)


async def test_an_over_cap_stream_aborts_its_writer_and_never_buffers_the_body(harness, monkeypatch):
    cap = 3 * CHUNK_SIZE
    monkeypatch.setitem(uploads.MAX_BYTES, DocumentFormat.PDF, cap)
    pid = await project(harness)
    writes: list[int] = []
    aborted: list[bool] = []
    original = harness.store.open_writer

    async def spy(key, **kw):
        writer = await original(key, **kw)
        write, abort = writer.write, writer.abort

        async def counting_write(data):
            writes.append(len(data))
            await write(data)

        async def counting_abort():
            aborted.append(True)
            await abort()

        writer.write, writer.abort = counting_write, counting_abort
        return writer

    monkeypatch.setattr(harness.store, "open_writer", spy)
    piece = b"x" * (1024 * 1024)

    async def body():
        yield PDF
        for _ in range(cap // len(piece) + 4):  # about 3 MiB past the cap
            yield piece

    response = await legacy(harness, pid, body(), "big.pdf", "application/pdf")
    assert response.status_code == 413
    assert aborted == [True] and nothing_kept(harness)
    assert writes and max(writes) <= CHUNK_SIZE + len(piece)  # one buffer, not the whole body
    assert sum(writes) <= cap


async def test_the_413_message_names_the_limit_in_megabytes(harness, monkeypatch):
    monkeypatch.setitem(uploads.MAX_BYTES, DocumentFormat.HTML, 2 * MiB)
    pid = await project(harness)
    response = await legacy(harness, pid, chunks(b"<p>" + b"a" * (2 * MiB), 600_000), "a.html", "text/html")
    assert response.status_code == 413
    assert "2 MB" in response.json()["error"]["message"]


# ---- batch item category (pure rule behind the SQL) -----------------------------------------


@pytest.mark.parametrize("spec", CANONICAL)
def test_the_batch_item_category_follows_the_format(spec):
    assert batches.item_category(spec.media_type) == CATEGORY[spec.format]


def test_an_unknown_media_type_falls_into_other_files():
    assert batches.item_category("application/octet-stream") == "other_files"
