"""G12 on the Drive path: the hostile files of S05 arrive through Drive and fail with fixed codes.

Scenario: the fake Drive serves a hostile file under a Drive MIME type; ``drive_import_item`` streams it to the
store unchecked (Drive is trusted for the type only); the parse activity of the child workflow then reads the
stored object through the same guard as an upload (``file_to_markdown``) and ends with a fixed code.

    fake Drive --bytes--> drive_import_item --> store original.<ext> + document row (media type from the MIME)
    parse_document <-- same row and key --> guard --> ApplicationError(code, type)   (history: code only)

The guard itself is tested in ``test_office_guard.py``; here only the Drive entry is new.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from drive_import_offline_support import CID, PID, Repo, Served, build, data, kind_of, mixed_drive, row_of, run_activity
from file_parsers import limits
from office_support import (
    DOCX,
    PPTX,
    XLSX,
    build_csv,
    build_docx,
    build_pptx,
    build_xlsx,
    lie_about_sizes,
    minimal_deck,
    rewrite_member,
    single_entry_zip,
    with_dtd,
)
from specflow_parse_support import FakeRepository, FakeSessions, install_fake_repository
from storage.memory import InMemoryObjectStore
from temporal import ids
from temporal.documents.activities import ParseActivities
from temporal.documents.models import ParseDocumentInput

TOO_LARGE = ("DocumentTooLargeError", "document_too_large")
PARSE_FAILED = ("DocumentParseFailedError", "document_parse_failed")
#: Drive file name: it must never appear in an error that reaches the workflow history.
PRIVATE_NAME = "confidential-merger-plan"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access while importing a Drive file")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(2)
    yield executor
    executor.shutdown(wait=False)


@pytest.fixture
def store():
    return InMemoryObjectStore(app_env="local")


async def through_drive(tmp_path, monkeypatch, store, pool, *, mime: str, content: bytes):
    """Import ``content`` as a Drive file of type ``mime`` and run the parse activity on the stored row.

    Returns ``(parse repository, error or None)``; the import itself must end in ``parse``.
    """

    drive = mixed_drive(tmp_path, [mime], Served)
    drive.content = content
    activities, _ = build(store, pool, drive)
    item = row_of(0, mime)
    item.name = PRIVATE_NAME
    repo = Repo(monkeypatch, item)
    kind = kind_of(item)
    assert kind == "document"
    result = await run_activity(activities, activities.drive_import_item, data(item, kind))
    assert result.action == "parse"  # the import does not look inside the file
    [inserted] = repo.inserted

    parse_repo = FakeRepository()
    parse_repo.media_type, parse_repo.original_key = inserted["media_type"], inserted["original_key"]
    install_fake_repository(monkeypatch, parse_repo)
    parse = ParseActivities(FakeSessions(), store, pool)
    arg = ParseDocumentInput(project_id=PID, document_id=ids.drive_document_id(CID, item.external_id))
    try:
        await ActivityEnvironment().run(parse.parse_document, arg)
    except ApplicationError as error:
        return parse_repo, error
    return parse_repo, None


def assert_fixed(error: ApplicationError | None, expected: tuple[str, str]) -> None:
    assert error is not None
    assert (error.type, error.message) == expected
    assert error.non_retryable  # never retried
    assert PRIVATE_NAME not in repr(error) and "fake-mixed" not in repr(error)


def bomb(tmp_path: Path, name: str, member: str) -> bytes:
    path = single_entry_zip(tmp_path / name, member, b"<a>" + b"x" * (4 * 1024 * 1024) + b"</a>")
    lie_about_sizes(path, declared=100)  # the headers claim a 100 byte entry
    return path.read_bytes()


def dtd(tmp_path: Path, mime: str) -> bytes:
    canary = tmp_path / "canary.txt"
    canary.write_text("TOPSECRET", encoding="utf-8")
    sources = {
        DOCX: (build_docx(tmp_path / "s.docx"), "word/document.xml"),
        PPTX: (build_pptx(tmp_path / "s.pptx", [("T", ["x"])]), "ppt/slides/slide1.xml"),
        XLSX: (build_xlsx(tmp_path / "s.xlsx", [("S", [["a"]])]), "xl/worksheets/sheet1.xml"),
    }
    source, member = sources[mime]
    return with_dtd(source, tmp_path / "hostile", member, canary).read_bytes()


def cells(tmp_path: Path, count: int) -> bytes:
    """A workbook whose dimension tag says one cell and whose sheet holds ``count`` cells."""

    rows = "".join(
        f'<row r="{n}">' + "".join(f'<c r="{chr(65 + i)}{n}"><v>1</v></c>' for i in range(10)) + "</row>"
        for n in range(1, count // 10 + 1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:A1"/><sheetData>{rows}</sheetData></worksheet>'
    ).encode()
    base = build_xlsx(tmp_path / "b.xlsx", [("S", [["a"]])])
    return rewrite_member(base, tmp_path / "cells.xlsx", "xl/worksheets/sheet1.xml", lambda _: sheet).read_bytes()


# --- refused with a fixed code ----------------------------------------------------------------------------------
async def test_a_zip_bomb_with_false_declared_sizes_is_too_large(tmp_path, monkeypatch, store, pool):
    content = bomb(tmp_path, "bomb.docx", "word/document.xml")
    repo, error = await through_drive(tmp_path, monkeypatch, store, pool, mime=DOCX, content=content)
    assert_fixed(error, TOO_LARGE)
    assert repo.parsed == []


@pytest.mark.parametrize("mime", [DOCX, PPTX, XLSX])
async def test_an_external_entity_in_an_office_file_is_refused_and_never_read(tmp_path, monkeypatch, store, pool, mime):
    repo, error = await through_drive(tmp_path, monkeypatch, store, pool, mime=mime, content=dtd(tmp_path, mime))
    assert_fixed(error, PARSE_FAILED)
    assert repo.parsed == []


async def test_a_false_dimension_tag_does_not_hide_the_cells_of_a_drive_xlsx(tmp_path, monkeypatch, store, pool):
    content = cells(tmp_path, limits.MAX_CELLS + 10)
    _, error = await through_drive(tmp_path, monkeypatch, store, pool, mime=XLSX, content=content)
    assert_fixed(error, TOO_LARGE)


async def test_five_hundred_and_one_slides_through_drive_are_too_large(tmp_path, monkeypatch, store, pool):
    content = minimal_deck(tmp_path / "deck.pptx", limits.MAX_SLIDES + 1).read_bytes()
    _, error = await through_drive(tmp_path, monkeypatch, store, pool, mime=PPTX, content=content)
    assert_fixed(error, TOO_LARGE)


async def test_a_csv_over_the_cell_cap_is_too_large(tmp_path, monkeypatch, store, pool):
    text = "\n".join(["1,2,3,4"] * (limits.MAX_CELLS // 4 + 1))
    content = build_csv(tmp_path / "wide.csv", text).read_bytes()
    _, error = await through_drive(tmp_path, monkeypatch, store, pool, mime="text/csv", content=content)
    assert_fixed(error, TOO_LARGE)


async def test_a_text_file_over_the_byte_cap_is_too_large(tmp_path, monkeypatch, store, pool):
    content = b"<p>" + b"x" * limits.MAX_TEXT_FILE_BYTES + b"</p>"
    _, error = await through_drive(tmp_path, monkeypatch, store, pool, mime="text/html", content=content)
    assert_fixed(error, TOO_LARGE)


@pytest.mark.parametrize("mime", [DOCX, PPTX, XLSX])
async def test_bytes_that_are_not_a_package_are_malformed_whatever_drive_says(tmp_path, monkeypatch, store, pool, mime):
    _, error = await through_drive(tmp_path, monkeypatch, store, pool, mime=mime, content=b"%PDF-1.4 not a zip at all")
    assert_fixed(error, PARSE_FAILED)


# --- parsed without danger ----------------------------------------------------------------------------------------
async def test_html_with_scripts_and_remote_images_is_parsed_without_either(tmp_path, monkeypatch, store, pool):
    page = (
        b"<html><body><h1>Quarterly plan</h1><script>STEAL_COOKIES()</script>"
        b'<img src="http://169.254.169.254/latest/meta-data/"><img src="file:///etc/passwd">'
        b'<iframe src="http://evil.example/"></iframe><p>Visible text.</p></body></html>'
    )
    repo, error = await through_drive(tmp_path, monkeypatch, store, pool, mime="text/html", content=page)

    assert error is None  # the socket guard of this module fails the test on any connection
    ((markdown, _),) = repo.parsed
    assert "Quarterly plan" in markdown and "Visible text." in markdown
    assert "STEAL_COOKIES" not in markdown and "169.254" not in markdown and "evil.example" not in markdown
