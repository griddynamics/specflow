"""The Dropbox importer (``api.imports.dropbox``, docs/dropbox/source.md) over ``FakeDropbox`` and a writer that records
what it stores: the check (``inspect``), the lanes, the caps, ``exclude``, the re-add skip, Path-Root, the Paper gate
and the failure mapping (DB14).

Sequence of one import::

    link -> account root (Path-Root) -> metadata of the target (path_lower) -> one recursive listing (+ continue pages)
    -> declare every file (size, modified) -> skip the skipped -> per file: re-add skip | download -> add_file | export

The Paper export tests use the DOCUMENTED shape only and set the gate by hand; re-run them when task L0 records a live
export and ``PAPER_EXPORT_RECORDED`` is switched on."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from api.imports import ImportFailedError, SourceGoneError
from api.imports import dropbox as dropbox_module
from api.imports import files as files_module
from api.imports.dropbox import DropboxImport, DropboxImporter, Inspection, inspect
from dropbox.client import DropboxApiError
from dropbox.codes import DropboxCode, DropboxError
from dropbox.content import DropboxContent
from dropbox.links import parse_link
from dropbox_import_support import (
    FOLDER_URL,
    MEMBER,
    DropboxWriter,
    FakeConnections,
    as_connections,
    as_writer,
    dropbox,
    job,
)
from dropbox_support import (
    ACCOUNT_TEAM,
    FILE_LINK_CANARY,
    FOLDER,
    FOREIGN_LINK_CANARY,
    NAME_CANARY,
    PATH_CANARY,
    PDF_ENTRY,
    ROOT_NAMESPACE,
    SHARED_LINK_CANARY,
    SUBFOLDER,
    TEAM_ROOT_NAMESPACE,
    FakeDropbox,
    file_entry,
)

PDF = b"%PDF-1.7 body"
SUB = "/Graphico Lab/Sub"
MODIFIED = datetime(2026, 11, 20, 10, 0, 1, tzinfo=UTC)
PAPER_OPTIONS = {"export_as": "markdown", "export_options": ["html", "markdown"]}  # DOCUMENTED SHAPE (export_info)
PAPER_BODY = {"result": {"export_metadata": {"name": "Plan.md"}}, "content_type": "text/plain", "body": b"# Plan\n"}


def rig(*entries: dict[str, Any], contents: dict[str, bytes] | None = None) -> tuple[FakeDropbox, FakeConnections]:
    """A Dropbox whose ``/Graphico Lab`` holds ``entries`` (its own folder entry first, as the fixture lists it)."""

    fake = dropbox()
    fake.trees[PATH_CANARY] = [FOLDER, *entries]
    fake.content = {e["id"]: PDF + e["id"].encode() for e in entries if e.get(".tag") == "file"} | (contents or {})
    return fake, FakeConnections(fake)


def home() -> DropboxImport:
    return DropboxImport(parse_link(FOLDER_URL))


def shared(link: str = SHARED_LINK_CANARY, exclude: frozenset[str] = frozenset()) -> DropboxImport:
    return DropboxImport(parse_link(link), exclude)


async def run(connections: FakeConnections, source: DropboxImport | None = None, writer: DropboxWriter | None = None):
    writer = writer or DropboxWriter()
    await DropboxImporter(as_connections(connections), source or home())(job(), as_writer(writer))
    return writer


async def check(connections: FakeConnections, source: DropboxImport | None = None) -> Inspection:
    return await inspect(as_connections(connections), MEMBER, source or home())


def three() -> tuple[dict[str, Any], ...]:
    return (
        SUBFOLDER,
        file_entry("id:1", "b.pdf", 100),
        file_entry("id:2", "a.html", 50),
        file_entry("id:3", "n.md", 20, folder=SUB),
    )


# The check ----------------------------------------------------------------------------------------------------------


async def test_the_check_lists_the_importable_files_by_path_then_name_with_categories_and_counts() -> None:
    _fake, connections = rig(
        SUBFOLDER,
        file_entry("id:1", "b.pdf", 100),
        file_entry("id:2", "a.docx", 50),
        file_entry("id:3", "n.md", 20, folder=SUB),
        file_entry("id:4", "t.txt", 5, folder=SUB),
        file_entry("id:5", "s.xlsx", 7),
        file_entry("id:6", "p.pptx", 9),
        file_entry("id:7", "d.csv", 11),
        file_entry("id:8", "h.html", 13),
        file_entry("id:9", "pic.png", 1),
        file_entry("id:10", "old.doc", 1),
        file_entry("id:11", "x.gdoc", 1, is_downloadable=False),
    )

    found = await check(connections)

    assert (found.kind, found.name, found.path_display) == ("folder", "Graphico Lab", "/Graphico Lab")
    assert [(f.id, f.name, f.path, f.category) for f in found.files] == [
        ("id:2", "a.docx", "", "word"),
        ("id:1", "b.pdf", "", "pdf"),
        ("id:7", "d.csv", "", "csv"),
        ("id:8", "h.html", "", "html"),
        ("id:6", "p.pptx", "", "powerpoint"),
        ("id:5", "s.xlsx", "", "excel"),
        ("id:3", "n.md", "Sub", "markdown"),
        ("id:4", "t.txt", "Sub", "text"),
    ]
    assert (found.skipped, found.folders, found.bytes) == (3, 1, 100 + 50 + 20 + 5 + 7 + 9 + 11 + 13)
    assert (found.files_capped, found.capped) == (False, False)


async def test_the_folder_of_the_link_itself_is_not_counted_and_deleted_entries_are_ignored() -> None:
    deleted = {".tag": "deleted", "name": "gone.pdf", "path_lower": "/graphico lab/gone.pdf"}
    _fake, connections = rig(deleted, {"no": "tag"}, file_entry("id:1", "a.pdf", 10))

    found = await check(connections)

    assert (found.folders, found.skipped, [f.id for f in found.files]) == (0, 0, ["id:1"])


async def test_a_shared_folder_link_resolves_to_its_folder_through_the_link_then_its_id() -> None:
    fake, connections = rig(*three())

    found = await check(connections, shared())

    assert (found.kind, found.name, len(found.files)) == ("folder", "Graphico Lab", 3)
    assert [path for path, _ in fake.bodies][:3] == [
        "/2/users/get_current_account",
        "/2/sharing/get_shared_link_metadata",
        "/2/files/get_metadata",
    ]
    assert fake.bodies[2][1]["path"] == FOLDER["id"] and fake.violations == []


async def test_a_file_link_is_one_file_and_an_unsupported_one_is_skipped() -> None:
    fake, connections = rig(file_entry("id:9", "logo.png", 5))
    fake.metadata["/graphico lab/logo.png"] = fake.trees[PATH_CANARY][1]

    pdf = await check(connections, shared(FILE_LINK_CANARY))
    png = await check(connections, DropboxImport(parse_link("https://www.dropbox.com/home/Graphico%20Lab/logo.png")))

    assert (pdf.kind, pdf.name, [(f.id, f.path, f.category) for f in pdf.files]) == (
        "file",
        "Spec.pdf",
        [(PDF_ENTRY["id"], "", "pdf")],
    )
    assert (pdf.skipped, pdf.folders, pdf.bytes, pdf.capped) == (0, 0, 592_000, False)
    assert (png.kind, png.files, png.skipped) == ("file", [], 1)


@pytest.mark.parametrize(
    ("link", "code"),
    [
        (FOREIGN_LINK_CANARY, DropboxCode.NO_ACCESS),  # a folder of someone else: no path_lower
        ("https://www.dropbox.com/scl/fo/unknown/AAA?rlkey=x", DropboxCode.NO_ACCESS),
        ("https://www.dropbox.com/home/Missing", DropboxCode.NO_ACCESS),
    ],
)
async def test_a_target_that_is_not_in_the_members_dropbox_is_no_access(link, code) -> None:
    _fake, connections = rig(*three())

    with pytest.raises(DropboxError) as error:
        await check(connections, DropboxImport(parse_link(link)))

    assert error.value.code is code


async def test_a_restricted_target_is_no_access_and_a_password_link_is_link_invalid(monkeypatch) -> None:
    fake, connections = rig(*three())
    fake.restricted.add(PATH_CANARY)
    with pytest.raises(DropboxError) as restricted:
        await check(connections)
    assert restricted.value.code is DropboxCode.NO_ACCESS

    async def locked(*_args: object, **_kwargs: object) -> object:
        raise DropboxApiError(DropboxCode.RESTRICTED, ("shared_link_access_denied",))

    monkeypatch.setattr(DropboxContent, "shared_link", locked)
    with pytest.raises(DropboxError) as password:
        await check(connections, shared())
    assert password.value.code is DropboxCode.LINK_INVALID


async def test_the_dropbox_root_is_link_invalid_and_a_malformed_path_too(monkeypatch) -> None:
    fake, connections = rig(*three())
    fake.metadata["/graphico lab"] = {**FOLDER, "path_lower": "/"}
    with pytest.raises(DropboxError) as root:
        await check(connections)
    assert root.value.code is DropboxCode.LINK_INVALID

    async def malformed(*_args: object, **_kwargs: object) -> object:
        raise DropboxApiError(DropboxCode.REFUSED, ("path", "malformed_path"))

    monkeypatch.setattr(DropboxContent, "metadata", malformed)
    with pytest.raises(DropboxError) as bad:
        await check(connections)
    assert bad.value.code is DropboxCode.LINK_INVALID


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        (DropboxApiError(DropboxCode.UNSUPPORTED, ("unsupported_link_type",)), DropboxCode.LINK_INVALID),
        (DropboxApiError(DropboxCode.UNSUPPORTED, ("unsupported_file",)), DropboxCode.NO_ACCESS),
        (DropboxApiError(DropboxCode.REFUSED, ("other",)), DropboxCode.NO_ACCESS),
        (DropboxError(DropboxCode.REFUSED), DropboxCode.NO_ACCESS),  # an answer this code cannot read
        (DropboxApiError(DropboxCode.REFUSED, ("reset",)), DropboxCode.UNREACHABLE),
        (DropboxError(DropboxCode.UNREACHABLE), DropboxCode.UNREACHABLE),
        (DropboxError(DropboxCode.RECONNECT_REQUIRED), DropboxCode.RECONNECT_REQUIRED),
    ],
)
async def test_a_refusal_about_the_target_is_never_auth_failed(monkeypatch, refusal, code) -> None:
    _fake, connections = rig(*three())

    async def refused(*_args: object, **_kwargs: object) -> object:
        raise refusal

    monkeypatch.setattr(DropboxContent, "shared_link", refused)
    with pytest.raises(DropboxError) as error:
        await check(connections, shared())
    assert error.value.code is code


async def test_an_expired_listing_cursor_is_unreachable_for_the_check_and_unknown_error_for_the_import(
    monkeypatch,
) -> None:
    fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 10) for n in range(3)))
    fake.page_size = 1

    async def reset(*_args: object, **_kwargs: object) -> object:
        raise DropboxApiError(DropboxCode.REFUSED, ("reset",))

    monkeypatch.setattr(DropboxContent, "list_folder_continue", reset)
    with pytest.raises(DropboxError) as error:
        await check(connections)
    assert error.value.code is DropboxCode.UNREACHABLE
    with pytest.raises(ImportFailedError) as failed:
        await run(connections)
    assert failed.value.reason == "unknown_error"


# Path-Root -----------------------------------------------------------------------------------------------------------


async def test_every_call_of_a_team_member_names_the_team_root_namespace() -> None:
    fake, connections = rig(*three())
    fake.access["sl.access-import"] = ACCOUNT_TEAM
    root = json.dumps({".tag": "root", "root": TEAM_ROOT_NAMESPACE}, separators=(",", ":"))

    await run(connections, shared())

    calls = [path for path, _ in fake.bodies]
    assert calls.count("/2/users/get_current_account") == 1  # the root is read once per import
    assert fake.path_roots and set(fake.path_roots) == {root}
    assert len(fake.path_roots) == len(fake.calls) - 1  # every call but the account read sent the header
    assert fake.violations == [] and ROOT_NAMESPACE not in "".join(fake.path_roots)


async def test_an_invalid_root_reads_the_root_once_more_and_asks_again() -> None:
    fake, connections = rig(*three())
    fake.invalid_root = 1

    found = await check(connections)

    assert len(found.files) == 3
    assert [path for path, _ in fake.bodies].count("/2/users/get_current_account") == 2


async def test_a_second_invalid_root_ends_the_read(monkeypatch) -> None:
    fake, connections = rig(*three())
    fake.invalid_root = 2

    with pytest.raises(DropboxError) as error:
        await check(connections)
    assert error.value.code is DropboxCode.INVALID_ROOT

    fake.invalid_root = 2
    with pytest.raises(ImportFailedError) as failed:
        await run(connections)
    assert failed.value.reason == "unknown_error"


# Caps ----------------------------------------------------------------------------------------------------------------


def cap_events(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "api.imports.dropbox_cap_reached"]


async def test_the_file_cap_stops_the_listing_and_is_logged_once_with_counts_only(monkeypatch, caplog) -> None:
    fake, connections = rig(*(file_entry(f"id:{n}", f"{NAME_CANARY}{n}.pdf", 10) for n in range(5)))
    fake.page_size = 2
    monkeypatch.setattr(dropbox_module, "MAX_FILES", 3)

    with caplog.at_level(logging.INFO):
        found = await check(connections)

    assert (len(found.files), found.files_capped, found.capped) == (3, True, True)
    (record,) = cap_events(caplog)
    assert (record.cap, record.files, record.bytes) == ("files", 3, 30)
    assert NAME_CANARY not in caplog.text and PATH_CANARY not in caplog.text
    pages = [path for path, _ in fake.bodies if path.startswith("/2/files/list_folder")]
    assert pages == ["/2/files/list_folder", *["/2/files/list_folder/continue"] * 2]  # the last page was not read


async def test_the_entry_cap_counts_files_folders_and_skipped_files(monkeypatch, caplog) -> None:
    _fake, connections = rig(
        SUBFOLDER, file_entry("id:1", "a.pdf"), file_entry("id:2", "x.png"), file_entry("id:3", "b.pdf")
    )
    monkeypatch.setattr(dropbox_module, "MAX_ENTRIES", 3)

    with caplog.at_level(logging.INFO):
        found = await check(connections)

    assert (found.folders, found.skipped, len(found.files)) == (1, 1, 1)
    assert (found.capped, found.files_capped) == (True, False)
    assert [r.cap for r in cap_events(caplog)] == ["entries"]


async def test_the_byte_cap_is_by_listed_size_of_the_importable_files(monkeypatch, caplog) -> None:
    _fake, connections = rig(
        file_entry("id:1", "a.pdf", 600), file_entry("id:2", "b.pdf", 500), file_entry("id:3", "c.png", 9_999)
    )
    monkeypatch.setattr(dropbox_module, "MAX_BYTES", 1000)

    with caplog.at_level(logging.INFO):
        found = await check(connections)

    assert ([f.id for f in found.files], found.bytes, found.capped) == (["id:1"], 600, True)
    assert [r.cap for r in cap_events(caplog)] == ["bytes"]


async def test_the_import_applies_the_same_caps_and_declares_only_what_it_takes(monkeypatch) -> None:
    _fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 10) for n in range(5)))
    monkeypatch.setattr(dropbox_module, "MAX_FILES", 2)

    writer = await run(connections)

    assert [d[0] for d in writer.declared] == ["id:0", "id:1"] and sorted(writer.files) == ["id:0", "id:1"]


async def test_a_listing_of_several_pages_is_read_to_the_end() -> None:
    fake, connections = rig(*(file_entry(f"id:{n}", f"{n:02}.pdf", 10) for n in range(7)))
    fake.page_size = 2

    found = await check(connections)

    assert [f.name for f in found.files] == [f"{n:02}.pdf" for n in range(7)] and not found.capped
    assert [path for path, _ in fake.bodies].count("/2/files/list_folder/continue") == 3


async def test_the_unchecked_files_count_toward_the_file_cap_so_the_import_takes_no_file_the_check_hid(
    monkeypatch,
) -> None:
    _fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 10) for n in range(4)))
    monkeypatch.setattr(dropbox_module, "MAX_FILES", 2)
    shown = [f.id for f in (await check(connections)).files]

    writer = await run(connections, DropboxImport(home().target, frozenset({"id:0"})))

    assert shown == ["id:0", "id:1"]
    assert sorted(writer.files) == ["id:1"] and [d[0] for d in writer.declared] == ["id:1"]


async def test_the_unchecked_files_count_toward_the_byte_cap(monkeypatch) -> None:
    _fake, connections = rig(
        file_entry("id:1", "a.pdf", 600), file_entry("id:2", "b.pdf", 300), file_entry("id:3", "c.pdf", 300)
    )
    monkeypatch.setattr(dropbox_module, "MAX_BYTES", 1000)
    shown = [f.id for f in (await check(connections)).files]

    writer = await run(connections, DropboxImport(home().target, frozenset({"id:1"})))

    assert shown == ["id:1", "id:2"] and sorted(writer.files) == ["id:2"]


async def test_a_file_over_its_cap_by_listed_size_is_skipped_by_format(monkeypatch) -> None:
    ooxml, text = files_module.MAX_OOXML_BYTES, files_module.MAX_TEXT_BYTES
    _fake, connections = rig(
        file_entry("id:1", "ok.docx", ooxml),
        file_entry("id:2", "big.docx", ooxml + 1),
        file_entry("id:3", "ok.md", text),
        file_entry("id:4", "big.csv", text + 1),
        file_entry("id:5", "big-ok.pdf", ooxml + 1),
        file_entry("id:6", "big.pdf", files_module.MAX_PDF_BYTES + 1),
    )

    found = await check(connections)

    assert sorted(f.id for f in found.files) == ["id:1", "id:3", "id:5"] and found.skipped == 3


# The import: lanes ---------------------------------------------------------------------------------------------------


async def test_each_file_goes_to_its_lane_declared_with_size_and_modified_time_named_by_its_path() -> None:
    _fake, connections = rig(*three(), file_entry("id:4", "pic.png", 5), file_entry("id:5", "t.txt", 5))

    writer = await run(connections)

    assert sorted(writer.files) == ["id:1", "id:2", "id:3", "id:5"]
    assert writer.files["id:3"][0] == "Sub/n.md" and writer.files["id:1"][0] == "b.pdf"
    assert writer.files["id:1"][1] == PDF + b"id:1"
    assert writer.files["id:1"][2] == files_module.lane_of("pdf").media_type
    assert writer.files["id:5"][2] == "text/markdown"  # a .txt is stored as Markdown, as an upload of it is
    assert writer.skipped == [("id:4", "pic.png")] and writer.failed == [] and writer.parsed == sorted(writer.files)
    assert {d[0] for d in writer.declared} == {"id:1", "id:2", "id:3", "id:4", "id:5"}
    assert writer.listed["id:1"] == (100, MODIFIED) and writer.listed["id:4"] == (5, MODIFIED)


async def test_a_shared_link_is_read_through_its_target_path_and_a_file_link_imports_one_file() -> None:
    fake, connections = rig(*three(), contents={PDF_ENTRY["id"]: PDF})

    folder = await run(connections, shared())
    file = await run(connections, shared(FILE_LINK_CANARY))

    assert sorted(folder.files) == ["id:1", "id:2", "id:3"]
    assert list(file.files) == [PDF_ENTRY["id"]] and file.files[PDF_ENTRY["id"]][0] == "Spec.pdf"
    assert fake.violations == []


async def test_the_unchecked_files_are_not_declared_and_not_downloaded() -> None:
    fake, connections = rig(*three())
    source = DropboxImport(home().target, frozenset({"id:2", "id:unknown"}))  # an unknown id is ignored

    writer = await run(connections, source)

    assert sorted(writer.files) == ["id:1", "id:3"] and "id:2" not in [d[0] for d in writer.declared]
    assert [json.loads(raw)["path"] for raw in fake.api_args] == ["id:1", "id:3"]


async def test_the_same_bytes_twice_are_a_duplicate_by_hash() -> None:
    _fake, connections = rig(
        file_entry("id:1", "a.pdf", 5), file_entry("id:2", "b.pdf", 5), contents={"id:1": PDF, "id:2": PDF}
    )

    writer = await run(connections)

    assert list(writer.files) == ["id:1"] and writer.duplicates == ["id:2"]


async def test_an_unchanged_file_of_an_earlier_import_is_linked_without_a_download() -> None:
    fake, connections = rig(*three())
    writer = DropboxWriter()
    writer.stored = {"id:1": (100, MODIFIED), "id:2": (49, MODIFIED), "id:3": (20, datetime(2020, 1, 1, tzinfo=UTC))}

    await run(connections, writer=writer)

    assert writer.linked == ["id:1"] and "id:1" not in writer.files  # same id, size and modified time
    assert sorted(writer.files) == ["id:2", "id:3"]  # another size, another modified time: downloaded
    assert [json.loads(raw)["path"] for raw in fake.api_args] == ["id:2", "id:3"]
    assert "id:1" in [d[0] for d in writer.declared]


async def test_at_most_four_downloads_run_at_once() -> None:
    _fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 5) for n in range(12)))

    writer = await run(connections)

    assert len(writer.files) == 12 and writer.peak == dropbox_module.DOWNLOAD_SLOTS == 4


async def test_every_call_of_a_personal_import_sends_the_path_root_and_none_is_off_the_allowlist() -> None:
    fake, connections = rig(*three())

    await run(connections)

    assert fake.violations == [] and len(fake.path_roots) == len(fake.calls) - 1


# Paper: gated until a live export is recorded (DB10, task L0) --------------------------------------------------------


def paper(**extra: Any) -> dict[str, Any]:
    return file_entry("id:p", "Plan.paper", 1024, is_downloadable=False, **extra)


async def test_a_paper_doc_is_skipped_while_no_live_export_is_recorded() -> None:
    fake, connections = rig(paper(export_info=PAPER_OPTIONS), file_entry("id:1", "a.pdf", 5))
    fake.exports["id:p"] = PAPER_BODY

    found = await check(connections)
    writer = await run(connections)

    assert dropbox_module.PAPER_EXPORT_RECORDED is False
    assert [f.id for f in found.files] == ["id:1"] and found.skipped == 1
    assert writer.skipped == [("id:p", "Plan.paper")] and writer.notes == {} and list(writer.files) == ["id:1"]
    assert all(json.loads(raw)["path"] != "id:p" for raw in fake.api_args)  # never exported, never downloaded


async def test_a_paper_doc_is_exported_as_markdown_when_the_gate_is_open(monkeypatch) -> None:  # DOCUMENTED SHAPE (L0)
    monkeypatch.setattr(dropbox_module, "PAPER_EXPORT_RECORDED", True)
    fake, connections = rig(paper(export_info=PAPER_OPTIONS), file_entry("id:1", "a.pdf", 5))
    fake.exports["id:p"] = PAPER_BODY

    found = await check(connections)
    writer = await run(connections)

    assert [(f.id, f.category) for f in found.files] == [("id:1", "pdf"), ("id:p", "paper")]
    assert writer.notes == {"id:p": ("Plan.paper", "# Plan\n", b"# Plan\n", "text/markdown")}
    exported = [json.loads(raw) for raw in fake.api_args if "export_format" in raw]
    assert exported == [{"path": "id:p", "export_format": "markdown"}]
    assert writer.skipped == [] and writer.failed == []


async def test_a_paper_doc_without_a_markdown_export_option_is_skipped_even_with_the_gate_open(monkeypatch) -> None:
    monkeypatch.setattr(dropbox_module, "PAPER_EXPORT_RECORDED", True)
    _fake, connections = rig(paper(export_info={"export_as": "html", "export_options": ["html"]}))

    writer = await run(connections)

    assert writer.skipped == [("id:p", "Plan.paper")] and writer.notes == {}


async def test_a_paper_export_that_fails_ends_the_item_convert_failed_in_the_convert_step(monkeypatch) -> None:
    monkeypatch.setattr(dropbox_module, "PAPER_EXPORT_RECORDED", True)
    fake, connections = rig(paper(export_info=PAPER_OPTIONS))  # no export in the fake: 409 non_exportable

    writer = await run(connections)
    assert writer.failed == [("id:p", "convert_failed", "convert")] and writer.notes == {}

    fake.exports["id:p"] = PAPER_BODY
    monkeypatch.setattr(dropbox_module, "MAX_EXPORT_BYTES", 3)  # the export is larger than its cap
    writer = await run(connections)
    assert writer.failed == [("id:p", "convert_failed", "convert")]


# The failures of an item ---------------------------------------------------------------------------------------------


async def test_a_file_that_is_not_found_or_restricted_ends_its_item_no_access_and_the_import_goes_on() -> None:
    fake, connections = rig(*three())
    fake.not_found.add("id:1")
    fake.restricted.add("id:2")

    writer = await run(connections)

    assert sorted(writer.failed) == [("id:1", "no_access", "read"), ("id:2", "no_access", "read")]
    assert list(writer.files) == ["id:3"]


async def test_a_download_that_fails_after_its_retries_ends_only_its_item_unknown_error_download(monkeypatch) -> None:
    _fake, connections = rig(*three())
    original = DropboxContent.download

    def flaky(self, token, file_id, **kwargs):
        if file_id == "id:2":
            raise DropboxError(DropboxCode.UNREACHABLE)
        return original(self, token, file_id, **kwargs)

    monkeypatch.setattr(DropboxContent, "download", flaky)

    writer = await run(connections)

    assert writer.failed == [("id:2", "unknown_error", "download")] and sorted(writer.files) == ["id:1", "id:3"]


async def test_a_file_that_cannot_be_downloaded_is_skipped_not_failed(monkeypatch) -> None:
    _fake, connections = rig(*three())
    original = DropboxContent.download

    def refused(self, token, file_id, **kwargs):
        if file_id == "id:2":
            raise DropboxApiError(DropboxCode.UNSUPPORTED, ("unsupported_file",))
        return original(self, token, file_id, **kwargs)

    monkeypatch.setattr(DropboxContent, "download", refused)

    writer = await run(connections)

    assert writer.skipped == [("id:2", "a.html")] and writer.failed == [] and sorted(writer.files) == ["id:1", "id:3"]


async def test_a_file_that_grows_past_its_cap_while_streaming_is_convert_failed_download(monkeypatch) -> None:
    _fake, connections = rig(
        file_entry("id:1", "a.html", 5), file_entry("id:2", "b.pdf", 5), contents={"id:1": b"x" * 50}
    )
    monkeypatch.setattr(files_module, "MAX_TEXT_BYTES", 10)

    writer = await run(connections)

    assert writer.failed == [("id:1", "convert_failed", "download")] and list(writer.files) == ["id:2"]


async def test_a_throttled_download_waits_by_retry_after_inside_the_budget_and_imports_the_file() -> None:
    fake, connections = rig(file_entry("id:1", "a.pdf", 5))
    fake.retry_after = "1"

    class Throttled(DropboxWriter):
        async def declare(self, *args: Any, **kwargs: Any) -> None:
            await super().declare(*args, **kwargs)
            fake.throttle = 2  # the downloads are throttled twice

    writer = await run(connections, writer=Throttled())

    assert list(writer.files) == ["id:1"] and writer.failed == []


async def test_throttling_past_the_one_wait_budget_of_the_import_ends_the_source_unknown_error() -> None:
    fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 5) for n in range(6)))
    fake.retry_after = "30"

    class Throttled(DropboxWriter):
        async def declare(self, *args: Any, **kwargs: Any) -> None:
            await super().declare(*args, **kwargs)
            fake.throttle = 1000

    with pytest.raises(ImportFailedError) as error:
        await run(connections, writer=Throttled())

    assert error.value.reason == "unknown_error"


# The failures of the source ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("link", [FOREIGN_LINK_CANARY, "https://www.dropbox.com/home/Missing"])
async def test_a_target_that_cannot_be_read_ends_the_source_no_access(link) -> None:
    _fake, connections = rig(*three())

    with pytest.raises(ImportFailedError) as error:
        await run(connections, DropboxImport(parse_link(link)))

    assert error.value.reason == "no_access"


async def test_a_connection_that_expired_ends_the_source_no_access() -> None:
    _fake, connections = rig(*three())
    connections.expired = True

    with pytest.raises(ImportFailedError) as error:
        await run(connections)

    assert error.value.reason == "no_access"


async def test_dropbox_down_after_the_retries_ends_the_source_unknown_error() -> None:
    fake, connections = rig(*three())
    fake.status = 503

    with pytest.raises(ImportFailedError) as error:
        await run(connections)

    assert error.value.reason == "unknown_error"


async def test_a_connection_lost_in_the_middle_stops_the_other_downloads_and_ends_the_source(monkeypatch) -> None:
    _fake, connections = rig(*(file_entry(f"id:{n}", f"{n}.pdf", 5) for n in range(8)))
    original = connections.call
    calls = 0

    async def flaky(user_id, request):
        nonlocal calls
        calls += 1
        if calls > 4:  # the root, the target, the listing and the first download go; the next are refused
            connections.expired = True
        return await original(user_id, request)

    monkeypatch.setattr(connections, "call", flaky)

    with pytest.raises(ImportFailedError) as error:
        await run(connections)

    assert error.value.reason == "no_access"


async def test_a_removed_source_stops_the_import() -> None:
    _fake, connections = rig(*three())

    class Gone(DropboxWriter):
        async def declare(self, *_: object, **__: object) -> None:
            raise SourceGoneError

    with pytest.raises(SourceGoneError):
        await run(connections, writer=Gone())


# What never reaches a log or a repr ----------------------------------------------------------------------------------


async def test_a_run_logs_no_name_no_path_no_link_and_no_token(caplog) -> None:
    _fake, connections = rig(file_entry("id:1", f"{NAME_CANARY}.pdf", 5), file_entry("id:2", "x.png", 5))

    with caplog.at_level(logging.DEBUG):
        await run(connections, shared())
        await check(connections, shared())

    haystack = "\n".join(f"{r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in caplog.records)
    for secret in (NAME_CANARY, "Graphico", "rlkeycanary", "sharedcanary7b4", "sl.access", PATH_CANARY):
        assert secret not in haystack
    (finished,) = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.dropbox_finished"]
    assert (finished.added, finished.duplicates, finished.failed, finished.skipped) == (1, 0, 0, 1)


def test_the_shared_link_of_an_import_never_shows_in_its_repr() -> None:
    source = shared()

    assert "rlkeycanary" not in repr(source) and "sharedcanary" not in repr(source)
