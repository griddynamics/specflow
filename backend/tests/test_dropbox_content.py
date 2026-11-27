"""``dropbox.content.DropboxContent``: each read is one documented Dropbox call made through the allowlisted client,
with the member's Path-Root, over ``FakeDropbox``: the account root, a shared link, a path, the pages of a recursive
listing, a download stream and a Paper export. Nothing here stores, logs or raises a name, link or Dropbox text.

The export tests use only the documented shape (plans/DROPBOX/research/dropbox-docs.md section 6); they are marked
``DOCUMENTED SHAPE`` and are to be run again against the live recording when task L0 lands (``PAPER_EXPORT``)."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from dropbox import content as content_module
from dropbox.client import WaitBudget
from dropbox.codes import DropboxCode, DropboxError
from dropbox.content import (
    LIST_LIMIT,
    DropboxContent,
    ExportTooLargeError,
)
from dropbox_import_support import BEARER, dropbox
from dropbox_support import (
    ACCOUNT_PERSONAL,
    ACCOUNT_TEAM,
    FOLDER,
    HTML_ENTRY,
    PAPER_ENTRY,
    PATH_CANARY,
    PDF_ENTRY,
    ROOT_NAMESPACE,
    SHARED_FOREIGN_META,
    TEAM_ROOT_NAMESPACE,
    FakeDropbox,
)

TOKEN = SecretStr(BEARER)
ROOT = json.dumps({".tag": "root", "root": ROOT_NAMESPACE}, separators=(",", ":"))
# DOCUMENTED SHAPE (files/export): the Dropbox-API-Result is {"export_metadata": {...}, "file_metadata": {...}}.
PAPER_RESULT = {"export_metadata": {"name": "Plan.md", "size": 21, "export_hash": "h"}, "file_metadata": {}}
PAPER_EXPORTS = {"result": PAPER_RESULT, "content_type": "text/plain; charset=utf-8", "body": b"# Plan\n\nSome text.\n"}


def rig() -> tuple[FakeDropbox, DropboxContent]:
    fake = dropbox()
    fake.access[BEARER] = ACCOUNT_PERSONAL
    return fake, DropboxContent(fake.client())


async def test_the_account_root_is_the_root_namespace_of_the_account() -> None:
    fake, content = rig()
    assert (await content.account_root(TOKEN)).root_namespace_id == ROOT_NAMESPACE
    fake.access[BEARER] = ACCOUNT_TEAM
    assert (await content.account_root(TOKEN)).root_namespace_id == TEAM_ROOT_NAMESPACE
    assert fake.violations == []


async def test_a_shared_link_is_read_with_its_url_and_the_path_root() -> None:
    fake, content = rig()
    link = next(url for url in fake.shared if url.endswith("rlkeycanary"))

    entry = await content.shared_link(TOKEN, link, path_root=ROOT_NAMESPACE)

    assert (entry.kind, entry.id, entry.path_lower) == ("folder", FOLDER["id"], PATH_CANARY)
    assert fake.bodies[-1] == ("/2/sharing/get_shared_link_metadata", {"url": link})
    assert fake.path_roots == [ROOT]


async def test_a_shared_link_of_someone_else_has_no_path_and_an_unknown_one_is_not_found() -> None:
    fake, content = rig()
    foreign = next(url for url in fake.shared if "rlkeyforeign" in url)

    entry = await content.shared_link(TOKEN, foreign, path_root=None)
    assert (entry.id, entry.path_lower) == (SHARED_FOREIGN_META["id"], None)
    with pytest.raises(DropboxError) as error:
        await content.shared_link(TOKEN, "https://www.dropbox.com/scl/fo/none/AAA?rlkey=x", path_root=None)

    assert error.value.code is DropboxCode.NOT_FOUND
    assert fake.path_roots == []  # no Path-Root without a root namespace


async def test_a_path_and_an_id_are_read_by_get_metadata() -> None:
    fake, content = rig()

    by_path = await content.metadata(TOKEN, "/Graphico Lab", path_root=ROOT_NAMESPACE)
    by_id = await content.metadata(TOKEN, PDF_ENTRY["id"], path_root=ROOT_NAMESPACE)

    assert (by_path.kind, by_path.path_display) == ("folder", "/Graphico Lab")
    assert (by_id.kind, by_id.name, by_id.size) == ("file", "Spec.pdf", 592_000)
    assert [body["path"] for _, body in fake.bodies] == ["/Graphico Lab", PDF_ENTRY["id"]]
    assert fake.bodies[0][1]["include_deleted"] is False


async def test_a_missing_or_restricted_target_is_not_found_or_restricted() -> None:
    fake, content = rig()
    fake.restricted.add(PDF_ENTRY["id"])
    for path, code in (("/missing", DropboxCode.NOT_FOUND), (PDF_ENTRY["id"], DropboxCode.RESTRICTED)):
        with pytest.raises(DropboxError) as error:
            await content.metadata(TOKEN, path, path_root=None)
        assert error.value.code is code


async def test_an_answer_of_an_unknown_shape_is_refused() -> None:
    fake, content = rig()
    fake.metadata["/odd"] = {".tag": "deleted", "name": "x", "path_lower": "/odd"}

    with pytest.raises(DropboxError) as error:
        await content.metadata(TOKEN, "/odd", path_root=None)

    assert error.value.code is DropboxCode.REFUSED


async def test_the_listing_is_recursive_with_non_downloadable_files_and_the_page_limit() -> None:
    fake, content = rig()

    page = await content.list_folder(TOKEN, PATH_CANARY, path_root=ROOT_NAMESPACE)

    path, body = fake.bodies[-1]
    assert path == "/2/files/list_folder"
    assert body == {
        "path": PATH_CANARY,
        "recursive": True,
        "include_deleted": False,
        "include_non_downloadable_files": True,
        "limit": LIST_LIMIT,
    }
    assert LIST_LIMIT == 2000 and not page.has_more
    assert [e.name for e in page.entries] == ["Graphico Lab", "Sub", "Spec.pdf", "Page.html", "logo.png", "Notes.md"]
    assert {e.kind for e in page.entries} == {"file", "folder"}


async def test_the_listing_continues_by_cursor_until_the_last_page() -> None:
    fake, content = rig()
    fake.page_size = 4

    first = await content.list_folder(TOKEN, PATH_CANARY, path_root=None)
    assert first.has_more and first.cursor is not None and len(first.entries) == 4
    last = await content.list_folder_continue(TOKEN, first.cursor, path_root=None)

    assert not last.has_more and [e.name for e in last.entries] == ["logo.png", "Notes.md"]
    assert fake.bodies[-1][0] == "/2/files/list_folder/continue" and set(fake.bodies[-1][1]) == {"cursor"}


async def test_a_listing_of_a_missing_folder_is_not_found_and_a_page_without_a_cursor_is_refused(monkeypatch) -> None:
    _fake, content = rig()
    with pytest.raises(DropboxError) as error:
        await content.list_folder(TOKEN, "/missing", path_root=None)
    assert error.value.code is DropboxCode.NOT_FOUND

    async def odd(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"entries": []}

    monkeypatch.setattr(content._client, "rpc", odd)
    with pytest.raises(DropboxError) as refused:
        await content.list_folder(TOKEN, PATH_CANARY, path_root=None)
    assert refused.value.code is DropboxCode.REFUSED


async def test_a_download_streams_the_bytes_by_id_with_the_path_root() -> None:
    fake, content = rig()

    async with content.download(TOKEN, HTML_ENTRY["id"], path_root=ROOT_NAMESPACE) as chunks:
        data = b"".join([chunk async for chunk in chunks])

    assert data == b"<html><body>fake</body></html>"
    assert json.loads(fake.api_args[-1]) == {"path": HTML_ENTRY["id"]}
    assert fake.path_roots == [ROOT] and fake.violations == []


async def test_a_download_of_a_missing_file_is_not_found_and_streams_in_chunks_of_the_configured_size(
    monkeypatch,
) -> None:
    fake, content = rig()
    fake.content[PDF_ENTRY["id"]] = b"x" * 25
    monkeypatch.setattr(content_module, "DOWNLOAD_CHUNK", 10)

    async with content.download(TOKEN, PDF_ENTRY["id"], path_root=None) as chunks:
        sizes = [len(chunk) async for chunk in chunks]
    with pytest.raises(DropboxError) as error:
        async with content.download(TOKEN, "id:nothing", path_root=None):
            pass

    assert sum(sizes) == 25 and max(sizes) <= 10
    assert error.value.code is DropboxCode.NOT_FOUND


async def test_a_throttled_download_waits_by_the_budget_then_gives_up() -> None:
    fake, content = rig()
    fake.throttle = 1
    fake.retry_after = "1"
    async with content.download(TOKEN, HTML_ENTRY["id"], path_root=None, budget=WaitBudget()) as chunks:
        assert b"".join([c async for c in chunks])
    fake.throttle = 5
    with pytest.raises(DropboxError) as error:
        async with content.download(TOKEN, HTML_ENTRY["id"], path_root=None, budget=WaitBudget(0.5)):
            pass

    assert error.value.code is DropboxCode.UNREACHABLE


async def test_an_invalid_root_is_raised_for_the_caller_to_read_the_root_again() -> None:
    fake, content = rig()
    fake.invalid_root = 1

    with pytest.raises(DropboxError) as error:
        await content.list_folder(TOKEN, PATH_CANARY, path_root="1")

    assert error.value.code is DropboxCode.INVALID_ROOT


# Paper export: DOCUMENTED SHAPE, run again against the live recording when task L0 lands ---------------------------


async def test_a_paper_export_asks_for_markdown_and_returns_its_text() -> None:  # DOCUMENTED SHAPE (L0 re-run)
    fake, content = rig()
    fake.exports[PAPER_ENTRY["id"]] = PAPER_EXPORTS

    text = await content.export(TOKEN, PAPER_ENTRY["id"], path_root=ROOT_NAMESPACE)

    assert text == "# Plan\n\nSome text.\n"
    assert json.loads(fake.api_args[-1]) == {"path": PAPER_ENTRY["id"], "export_format": "markdown"}
    assert fake.path_roots == [ROOT] and fake.violations == []


async def test_a_paper_export_over_its_cap_is_too_large() -> None:  # DOCUMENTED SHAPE (L0 re-run)
    fake, content = rig()
    fake.exports[PAPER_ENTRY["id"]] = {**PAPER_EXPORTS, "body": b"x" * 11}

    with pytest.raises(ExportTooLargeError):
        await content.export(TOKEN, PAPER_ENTRY["id"], max_bytes=10, path_root=None)
    assert await content.export(TOKEN, PAPER_ENTRY["id"], max_bytes=11, path_root=None) == "x" * 11


async def test_a_paper_export_that_is_not_text_is_refused() -> None:  # DOCUMENTED SHAPE (L0 re-run)
    fake, content = rig()
    fake.exports[PAPER_ENTRY["id"]] = {**PAPER_EXPORTS, "body": b"\xff\xfe\x00"}

    with pytest.raises(DropboxError) as error:
        await content.export(TOKEN, PAPER_ENTRY["id"], path_root=None)

    assert error.value.code is DropboxCode.REFUSED


async def test_a_paper_doc_without_an_export_is_unsupported() -> None:
    _fake, content = rig()  # no recorded export: the fake answers 409 non_exportable

    with pytest.raises(DropboxError) as error:
        await content.export(TOKEN, PAPER_ENTRY["id"], path_root=None)

    assert error.value.code is DropboxCode.UNSUPPORTED
