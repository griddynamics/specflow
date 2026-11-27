"""The Box importer (``api.imports.box``) over a scripted Box (``box_import_support``) and a writer that records
what it stores: the check (``inspect``) and the import by lane, the two caps, the size skip, the cap while streaming,
duplicates, Box Notes, pacing, the failure mapping of items and of the source, and what never reaches a log.

Sequence of one import::

    resolve the link as the bound member -> walk the tree (folder items pages, breadth first, to the caps)
    -> declare every file -> skipped types end ``skipped`` -> unreadable folders end ``no_access``
    -> at most 4 at a time: download -> writer.add_file | Box Note text -> writer.add
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from api.imports import ImportFailedError, SourceGoneError
from api.imports import box as box_module
from api.imports import files as files_module
from api.imports.box import BoxImport, BoxImporter, CheckedFile, Inspection, inspect
from api.uploads import MAX_BYTES
from box import content as content_module
from box.codes import BoxCode, BoxError
from box.links import parse_link
from box_import_support import (
    MEMBER,
    BoxWriter,
    FakeConnections,
    NoteBox,
    add_file,
    add_folder,
    as_connections,
    as_writer,
    file_entry,
    folder_entry,
    job,
)
from box_support import SHARED_ITEM_FOLDER, SHARED_LINK_CANARY
from file_parsers.formats import DocumentFormat

NAME_CANARY = "secret-name-canary-77.pdf"
PDF = b"%PDF-1.7 body"


@pytest.fixture(autouse=True)
def no_poll_wait(monkeypatch) -> None:
    monkeypatch.setattr(content_module, "POLL_SECONDS", 0)


def rig(entries: list[dict] | None = None) -> tuple[NoteBox, FakeConnections]:
    box = NoteBox()
    box.items["2001"] = entries or []
    return box, FakeConnections(box)


def folder_source(folder_id: str = "2001") -> BoxImport:
    return BoxImport(parse_link(f"https://app.box.com/folder/{folder_id}"))


async def run(box: NoteBox, connections: FakeConnections, source: BoxImport | None = None) -> BoxWriter:
    writer = BoxWriter()
    await BoxImporter(as_connections(connections), source or folder_source())(job(), as_writer(writer))
    return writer


async def check(connections: FakeConnections, source: BoxImport | None = None) -> Inspection:
    return await inspect(as_connections(connections), MEMBER, source or folder_source())


# The check and the lanes ---------------------------------------------------------------------------------------


async def test_the_check_counts_files_by_lane_folders_and_skipped_types() -> None:
    box, connections = rig(
        [
            folder_entry("2002", "Sub"),
            file_entry("1", "a.pdf"),
            file_entry("2", "b.docx"),
            file_entry("3", "c.md"),
            file_entry("4", "Note.boxnote"),
            file_entry("5", "t.txt"),
            file_entry("6", "old.doc"),
            file_entry("7", "pic.png"),
            file_entry("8", "sheet.gsheet"),
            {"type": "web_link", "id": "9", "name": "Wiki"},
        ]
    )
    add_folder(box, "2002", [file_entry("10", "deep.xlsx"), file_entry("11", "clip.mp4")], "Sub")

    found = await check(connections)

    assert found == Inspection(
        "folder",
        "Project",
        "2001",
        files=[
            CheckedFile("1", "a.pdf", "", "pdf"),
            CheckedFile("2", "b.docx", "", "word"),
            CheckedFile("3", "c.md", "", "markdown"),
            CheckedFile("4", "Note.boxnote", "", "note"),
            CheckedFile("5", "t.txt", "", "text"),
            CheckedFile("10", "deep.xlsx", "Sub", "excel"),
        ],
        files_capped=False,
        skipped=5,
        folders=1,
        bytes=60,
        capped=False,
    )


async def test_the_check_lists_the_nested_path_and_every_category() -> None:
    box, connections = rig(
        [folder_entry("2002", "A"), file_entry("1", "x.pptx"), file_entry("2", "y.html"), file_entry("3", "z.csv")]
    )
    add_folder(box, "2002", [folder_entry("2003", "B")], "A")
    add_folder(box, "2003", [file_entry("4", "deep.pdf", 7)], "B")

    found = await check(connections)

    assert [(f.id, f.path, f.category) for f in found.files] == [
        ("1", "", "powerpoint"),
        ("2", "", "html"),
        ("3", "", "csv"),
        ("4", "A/B", "pdf"),
    ]
    assert (found.bytes, found.folders) == (37, 2)


async def test_a_file_link_is_one_file_and_an_unsupported_one_is_skipped() -> None:
    box, connections = rig()
    add_file(box, file_entry("3001", "Spec.pdf", 5))
    add_file(box, file_entry("3003", "logo.png", 5))

    assert await check(connections, BoxImport(parse_link("https://app.box.com/file/3001"))) == Inspection(
        "file",
        "Spec.pdf",
        "3001",
        files=[CheckedFile("3001", "Spec.pdf", "", "pdf")],
        files_capped=False,
        skipped=0,
        folders=0,
        bytes=5,
        capped=False,
    )
    assert await check(connections, BoxImport(parse_link("https://app.box.com/file/3003"))) == Inspection(
        "file", "logo.png", "3003", files=[], files_capped=False, skipped=1, folders=0, bytes=0, capped=False
    )


async def test_a_trashed_file_and_a_file_over_its_cap_are_skipped_not_counted_as_files(monkeypatch) -> None:
    big = file_entry("2", "big.pdf", box_module.MAX_PDF_BYTES + 1)
    _box, connections = rig([file_entry("1", "ok.pdf"), big, file_entry("3", "gone.pdf", item_status="trashed")])

    found = await check(connections)

    assert (len(found.files), found.skipped) == (1, 2)


async def test_the_size_cap_is_by_format() -> None:
    ooxml, text = box_module.MAX_OOXML_BYTES, box_module.MAX_TEXT_BYTES
    _box, connections = rig(
        [
            file_entry("1", "ok.docx", ooxml),
            file_entry("2", "big.docx", ooxml + 1),
            file_entry("3", "ok.md", text),
            file_entry("4", "big.csv", text + 1),
            file_entry("5", "pdf-ok.pdf", ooxml + 1),
        ]
    )

    found = await check(connections)

    assert (len(found.files), found.skipped) == (3, 2)


async def test_the_file_cap_stops_the_walk_and_is_logged_once_with_counts_only(monkeypatch, caplog) -> None:
    monkeypatch.setattr(box_module, "MAX_FILES", 3)
    _box, connections = rig([file_entry(str(n), f"{n}-{NAME_CANARY}") for n in range(10)])

    with caplog.at_level(logging.INFO):
        found = await check(connections)

    assert (len(found.files), found.files_capped, found.capped) == (3, True, True)
    records = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.box_cap_reached"]
    assert len(records) == 1
    assert (records[0].cap, records[0].files, records[0].entries) == ("files", 3, 3)
    assert NAME_CANARY not in caplog.text


async def test_the_check_ignores_exclude() -> None:
    _box, connections = rig([file_entry("1", "a.pdf"), file_entry("2", "b.pdf")])
    source = BoxImport(parse_link("https://app.box.com/folder/2001"), frozenset({"1"}))

    assert [f.id for f in (await check(connections, source)).files] == ["1", "2"]


async def test_the_entry_cap_counts_files_folders_and_skipped_items(monkeypatch, caplog) -> None:
    monkeypatch.setattr(box_module, "MAX_ENTRIES", 5)
    box, connections = rig(
        [folder_entry("2002", "Sub")]
        + [file_entry(str(n), f"{n}.png") for n in range(10)]
        + [file_entry("99", "a.pdf")]
    )
    add_folder(box, "2002", [file_entry("100", "never.pdf")], "Sub")

    with caplog.at_level(logging.INFO):
        found = await check(connections)

    assert (len(found.files), found.files_capped, found.skipped, found.folders, found.capped) == (0, False, 4, 1, True)
    records = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.box_cap_reached"]
    assert len(records) == 1 and records[0].cap == "entries" and records[0].entries == 5


async def test_the_walk_follows_the_pages_of_a_folder() -> None:
    box, connections = rig([file_entry(str(n), f"{n}.pdf") for n in range(7)])
    box.page_size = 3

    assert len((await check(connections)).files) == 7


async def test_a_web_link_all_files_and_an_unreadable_target_are_refused() -> None:
    box, connections = rig()
    box.shared["https://app.box.com/s/bookmark"] = {"type": "web_link", "id": "4", "name": "Wiki"}
    cases = {
        "https://app.box.com/s/bookmark": BoxCode.LINK_INVALID,
        "https://app.box.com/folder/9999": BoxCode.NO_ACCESS,
        "https://app.box.com/file/9999": BoxCode.NO_ACCESS,
    }
    for link, code in cases.items():
        with pytest.raises(BoxError) as error:
            await check(connections, BoxImport(parse_link(link)))
        assert error.value.code is code


async def test_a_shared_link_resolves_to_its_folder_and_is_sent_only_as_the_boxapi_header() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.shared["https://app.box.com/s/abc123"] = SHARED_ITEM_FOLDER
    source = BoxImport(parse_link("https://app.box.com/s/abc123"))

    found = await check(connections, source)

    assert (found.kind, found.resolved_id, len(found.files)) == ("folder", "2001", 1)
    assert set(box.shared_headers) == {"shared_link=https://app.box.com/s/abc123"}
    assert len(box.shared_headers) == 2  # the resolution and the listing, as the member (not an anonymous read)
    assert all("abc123" not in path for _, path in box.calls)


# The import ----------------------------------------------------------------------------------------------------


async def test_every_file_is_declared_by_its_path_then_stored_in_its_lane() -> None:
    box, connections = rig(
        [
            folder_entry("2002", "Sub"),
            file_entry("1", "a.pdf"),
            file_entry("4", "Note.boxnote"),
            file_entry("5", "t.txt"),
            file_entry("7", "pic.png"),
        ]
    )
    add_folder(box, "2002", [file_entry("10", "deep.md")], "Sub")
    for file_id, data in (("1", PDF), ("5", b"plain"), ("10", b"# md")):
        box.content[file_id] = data
    box.representations["4"] = "note text"

    writer = await run(box, connections)

    assert sorted(writer.declared) == sorted(
        [
            ("file:1", "a.pdf"),
            ("file:4", "Note.boxnote"),
            ("file:5", "t.txt"),
            ("file:7", "pic.png"),
            ("file:10", "Sub/deep.md"),
        ]
    )
    assert writer.skipped == [("file:7", "pic.png")] and writer.failed == []
    assert writer.files == {
        "file:1": ("a.pdf", PDF, "application/pdf"),
        "file:5": ("t.txt", b"plain", "text/markdown"),  # a .txt is stored as Markdown, as an upload of it is
        "file:10": ("Sub/deep.md", b"# md", "text/markdown"),
    }
    name, markdown, original = writer.notes["file:4"]
    assert (name, markdown) == ("Note.boxnote", "note text")
    assert json.loads(original) == {"file": {"id": "4", "name": "Note.boxnote"}, "text": "note text"}
    assert sorted(writer.parsed) == ["file:1", "file:10", "file:5"]  # the notes are converted here, not parsed


async def test_a_file_link_imports_that_one_file() -> None:
    box, connections = rig()
    add_file(box, file_entry("3001", "Spec.pdf", 5), PDF)

    writer = await run(box, connections, BoxImport(parse_link("https://app.box.com/file/3001")))

    assert writer.files == {"file:3001": ("Spec.pdf", PDF, "application/pdf")}


async def test_a_shared_folder_is_imported_through_the_shared_link_header() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.content["1"] = PDF
    box.shared["https://app.box.com/s/abc123"] = SHARED_ITEM_FOLDER

    writer = await run(box, connections, BoxImport(parse_link("https://app.box.com/s/abc123")))

    assert list(writer.files) == ["file:1"]
    assert len(box.shared_headers) == 3 and set(box.shared_headers) == {"shared_link=https://app.box.com/s/abc123"}


async def test_the_same_bytes_in_two_files_are_stored_once_and_the_other_is_a_duplicate() -> None:
    box, connections = rig([file_entry("1", "a.pdf"), file_entry("2", "copy of a.pdf")])
    box.content["1"] = box.content["2"] = PDF

    writer = await run(box, connections)

    assert len(writer.files) == 1 and len(writer.duplicates) == 1 and len(writer.parsed) == 1


async def test_a_file_over_its_cap_while_streaming_is_convert_failed_in_the_download_step(monkeypatch) -> None:
    monkeypatch.setattr(files_module, "MAX_TEXT_BYTES", 5)
    box, connections = rig([file_entry("1", "notes.txt", 3), file_entry("2", "ok.txt", 3)])
    box.content["1"], box.content["2"] = b"twelve bytes", b"ok"

    writer = await run(box, connections)

    assert writer.failed == [("file:1", "convert_failed", "download")]
    assert list(writer.files) == ["file:2"]


async def test_a_file_the_member_cannot_read_is_a_no_access_item_and_the_import_goes_on() -> None:
    box, connections = rig([file_entry("1", "locked.pdf"), file_entry("2", "ok.pdf")])
    box.content["2"] = PDF  # nothing for "1": Box answers 404

    writer = await run(box, connections)

    assert writer.failed == [("file:1", "no_access", "read")]
    assert list(writer.files) == ["file:2"]


async def test_a_folder_the_member_cannot_read_is_a_no_access_item_and_the_rest_is_imported() -> None:
    box, connections = rig([folder_entry("2002", "Locked"), file_entry("2", "ok.pdf")])  # no listing for 2002
    box.content["2"] = PDF

    writer = await run(box, connections)

    assert writer.failed == [("folder:2002", "no_access", "read")]
    assert writer.declared == [("file:2", "ok.pdf")] and list(writer.files) == ["file:2"]


async def test_a_download_that_never_gets_ready_is_an_unknown_error_item_in_the_download_step() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.content["1"] = PDF
    box.not_ready = 99

    writer = await run(box, connections)

    assert writer.failed == [("file:1", "unknown_error", "download")] and writer.files == {}


async def test_a_throttled_box_is_waited_for_and_the_import_completes() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.content["1"] = PDF
    box.throttle = 3

    writer = await run(box, connections)

    assert list(writer.files) == ["file:1"] and writer.failed == []


async def test_a_box_note_that_becomes_ready_is_stored_and_one_that_never_does_is_an_item_in_error() -> None:
    box, connections = rig([file_entry("1", "Slow.boxnote"), file_entry("2", "Late.boxnote")])
    box.representations["1"] = "ready text"
    box.states["1"] = ["none", "pending", "success"]
    box.states["2"] = ["pending"]
    box.representations["2"] = "never read"

    writer = await run(box, connections)

    assert list(writer.notes) == ["file:1"]
    assert writer.failed == [("file:2", "unknown_error", "read")]


async def test_at_most_four_downloads_run_at_once() -> None:
    box, connections = rig([file_entry(str(n), f"{n}.pdf") for n in range(12)])
    for n in range(12):
        box.content[str(n)] = PDF + str(n).encode()

    writer = await run(box, connections)

    assert len(writer.files) == 12 and writer.peak == box_module.DOWNLOAD_SLOTS == 4


# The failure of the source ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "reason"),
    [("https://app.box.com/folder/9999", "no_access"), ("https://app.box.com/file/9999", "no_access")],
)
async def test_a_target_that_cannot_be_read_ends_the_source_no_access(link, reason) -> None:
    box, connections = rig()

    with pytest.raises(ImportFailedError) as error:
        await run(box, connections, BoxImport(parse_link(link)))

    assert error.value.reason == reason


async def test_a_connection_that_expired_ends_the_source_no_access() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    connections.expired = True

    with pytest.raises(ImportFailedError) as error:
        await run(box, connections)

    assert error.value.reason == "no_access"


async def test_box_down_after_the_retries_ends_the_source_unknown_error() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.status = 503

    with pytest.raises(ImportFailedError) as error:
        await run(box, connections)

    assert error.value.reason == "unknown_error"


async def test_a_connection_lost_in_the_middle_stops_the_other_downloads_and_ends_the_source(monkeypatch) -> None:
    box, connections = rig([file_entry(str(n), f"{n}.pdf") for n in range(6)])
    for n in range(6):
        box.content[str(n)] = PDF + str(n).encode()
    original = connections.call
    calls = 0

    async def flaky(user_id, request):
        nonlocal calls
        calls += 1
        if calls > 3:  # the resolution, the listing, then the first download goes; the next are refused
            connections.expired = True
        return await original(user_id, request)

    monkeypatch.setattr(connections, "call", flaky)

    with pytest.raises(ImportFailedError) as error:
        await run(box, connections)

    assert error.value.reason == "no_access"


async def test_a_removed_source_stops_the_import() -> None:
    box, connections = rig([file_entry("1", "a.pdf")])
    box.content["1"] = PDF

    class Gone(BoxWriter):
        async def declare(self, *_: object, **__: object) -> None:
            raise SourceGoneError

    with pytest.raises(SourceGoneError):
        await BoxImporter(as_connections(connections), folder_source())(job(), as_writer(Gone()))


# What never reaches a log ----------------------------------------------------------------------------------------


async def test_a_run_logs_no_name_no_link_and_no_token(caplog) -> None:
    box, connections = rig([file_entry("1", NAME_CANARY), file_entry("2", "n.boxnote"), file_entry("3", "x.png")])
    box.content["1"] = PDF
    box.shared["https://app.box.com/s/abc123"] = SHARED_ITEM_FOLDER
    source = BoxImport(parse_link("https://app.box.com/s/abc123"))

    with caplog.at_level(logging.DEBUG):
        await run(box, connections, source)

    haystack = "\n".join(f"{r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in caplog.records)
    for secret in (NAME_CANARY, "abc123", SHARED_LINK_CANARY, "access-test", "text of the note", "n.boxnote"):
        assert secret not in haystack
    assert any(getattr(r, "event", None) == "api.imports.box_finished" for r in caplog.records)


def test_the_caps_are_the_ones_of_the_decision_and_of_an_upload() -> None:
    assert (box_module.MAX_FILES, box_module.MAX_ENTRIES, box_module.DOWNLOAD_SLOTS) == (1000, 5000, 4)
    assert MAX_BYTES[DocumentFormat.PDF] == box_module.MAX_PDF_BYTES
    assert MAX_BYTES[DocumentFormat.DOCX] == box_module.MAX_OOXML_BYTES
    assert MAX_BYTES[DocumentFormat.MARKDOWN] == box_module.MAX_TEXT_BYTES


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_a_note_content_fetch_that_fails_is_an_item_failure_and_the_import_goes_on(status: int) -> None:
    box, connections = rig([file_entry("1", "n.boxnote"), file_entry("2", "ok.txt", 3)])
    box.content_status = status
    box.content["2"] = b"abc"

    writer = await run(box, connections)

    assert writer.failed == [("file:1", "unknown_error", "read")]
    assert list(writer.files) == ["file:2"] and box.refreshes == 0


async def test_a_note_whose_content_url_is_outside_dl_boxcloud_com_is_an_item_failure(monkeypatch) -> None:
    box, connections = rig([file_entry("1", "n.boxnote")])
    original = box.handle

    async def rewrite(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if "x-rep-hints" not in request.headers:
            return response
        body = json.loads(response.content)
        body["representations"]["entries"][0]["content"]["url_template"] = "https://api.box.com/2.0/x/{+asset_path}"
        return httpx.Response(200, json=body)

    monkeypatch.setattr(box, "handle", rewrite)
    connections.client = box.client()

    writer = await run(box, connections)

    assert writer.failed == [("file:1", "unknown_error", "read")] and box.content_auth == []


# The member unchecks files ---------------------------------------------------------------------------------------


async def test_an_excluded_file_is_neither_declared_nor_downloaded_nor_parsed_and_only_the_count_is_logged(
    caplog,
) -> None:
    box, connections = rig(
        [
            folder_entry("2002", "Sub"),
            file_entry("1", f"keep-{NAME_CANARY}"),
            file_entry("2", f"drop-{NAME_CANARY}"),
            file_entry("4", "Drop.boxnote"),
            file_entry("7", "pic.png"),
        ]
    )
    add_folder(box, "2002", [file_entry("10", "deep.md"), file_entry("11", "drop.md")], "Sub")
    box.content["1"], box.content["10"] = PDF, b"# md"
    source = BoxImport(parse_link("https://app.box.com/folder/2001"), frozenset({"2", "4", "11", "999"}))

    with caplog.at_level(logging.INFO):
        writer = await run(box, connections, source)

    assert sorted(writer.declared) == [
        ("file:1", f"keep-{NAME_CANARY}"),
        ("file:10", "Sub/deep.md"),
        ("file:7", "pic.png"),
    ]
    assert sorted(writer.files) == ["file:1", "file:10"] and writer.notes == {}
    assert sorted(writer.parsed) == ["file:1", "file:10"]
    assert not any(call_path.endswith(("/2/content", "/11/content")) for _, call_path in box.calls)
    finished = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.box_finished"]
    assert len(finished) == 1 and finished[0].excluded == 3 and finished[0].skipped == 1
    assert NAME_CANARY not in caplog.text and "999" not in caplog.text


async def test_an_excluded_file_still_counts_toward_the_file_cap(monkeypatch) -> None:
    monkeypatch.setattr(box_module, "MAX_FILES", 3)
    box, connections = rig([file_entry(str(n), f"{n}.pdf") for n in range(1, 8)])
    for n in range(1, 8):
        box.content[str(n)] = PDF + str(n).encode()
    source = BoxImport(parse_link("https://app.box.com/folder/2001"), frozenset({"1"}))

    writer = await run(box, connections, source)

    assert sorted(writer.declared) == [
        ("file:2", "2.pdf"),
        ("file:3", "3.pdf"),
    ]  # never file 4: the check did not show it
