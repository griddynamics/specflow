"""``box.content.BoxContent``: the content reads over a scripted Box (``box_support``, ``box_import_support``): folders,
files, shared items and the password answer, items pages, the download stream, Box Notes through the
``[extracted_text]`` representation, the pacing of 429 and 202, and that the client stays read-only.

Sequence of a Box Note::

    GET /files/{id}?fields=representations (x-rep-hints [extracted_text]) -> state none | pending | success
    none -> GET info.url (starts it); pending -> wait POLL_SECONDS, ask again (3 polls at most); success -> GET content
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from box import content as content_module
from box.client import BoxClient, WaitBudget
from box.codes import BoxCode, BoxError
from box.content import DOWNLOAD_CHUNK, BoxContent
from box_import_support import BEARER, NoteBox, add_file, add_folder, file_entry, folder_entry
from box_support import FILE_INFO, FOLDER_INFO, SHARED_LINK_CANARY, FakeBox

T = SecretStr(BEARER)


def content_of(box: FakeBox) -> BoxContent:
    box.access[BEARER] = {}
    return BoxContent(box.client())


async def code_of(awaitable) -> BoxCode:
    with pytest.raises(BoxError) as error:
        await awaitable
    return error.value.code


async def test_a_folder_and_a_file_are_read_by_id() -> None:
    box = FakeBox()
    content = content_of(box)

    folder, file = await content.folder(T, "2001"), await content.file(T, "3001")

    assert (folder.kind, folder.id, folder.name, folder.active) == ("folder", "2001", "Project", True)
    assert (file.kind, file.size, file.extension) == ("file", 4096, "pdf")
    assert "Spec" not in repr(file)  # a name never reaches a repr


async def test_a_folder_or_file_that_cannot_be_read_is_no_access() -> None:
    box = FakeBox()
    box.folders.pop("2001")
    content = content_of(box)

    assert await code_of(content.folder(T, "2001")) is BoxCode.NO_ACCESS
    assert await code_of(content.file(T, "nope")) is BoxCode.NO_ACCESS


async def test_an_item_in_the_trash_is_not_active_and_a_name_without_extension_has_none() -> None:
    box = FakeBox()
    box.files["5"] = {"type": "file", "id": "5", "name": "README", "size": 3, "item_status": "trashed"}
    item = await content_of(box).file(T, "5")

    assert (item.active, item.extension) == (False, None)


async def test_a_shared_link_is_resolved_with_the_boxapi_header_and_only_there() -> None:
    box = FakeBox()
    item = await content_of(box).shared_item(T, SHARED_LINK_CANARY)

    assert (item.kind, item.id) == ("folder", "2001")
    assert box.shared_headers == [f"shared_link={SHARED_LINK_CANARY}"]
    assert all(SHARED_LINK_CANARY.rsplit("/", 1)[1] not in path for _, path in box.calls)  # not in any URL


async def test_an_unknown_shared_link_is_no_access() -> None:
    assert await code_of(content_of(FakeBox()).shared_item(T, "https://app.box.com/s/unknown")) is BoxCode.NO_ACCESS


async def test_a_password_protected_shared_link_is_the_password_code_whichever_way_box_says_it() -> None:
    box = FakeBox()
    box.shared["https://app.box.com/s/locked"] = {**FOLDER_INFO, "shared_link": {"is_password_enabled": True}}
    content = content_of(box)
    assert await code_of(content.shared_item(T, "https://app.box.com/s/locked")) is BoxCode.LINK_PROTECTED

    class Refusing(FakeBox):
        def _rest(self, _request, _user):  # Box answers 403 with a message about the password
            return httpx.Response(403, json={"code": "access_denied", "message": "A password is required"})

    assert await code_of(content_of(Refusing()).shared_item(T, "https://app.box.com/s/x")) is BoxCode.LINK_PROTECTED


async def test_the_items_of_a_folder_come_in_pages_by_marker() -> None:
    box = FakeBox()
    box.page_size = 2
    content = content_of(box)

    first = await content.folder_items(T, "2001")
    second = await content.folder_items(T, "2001", marker=first.next_marker)
    third = await content.folder_items(T, "2001", marker=second.next_marker)

    assert [len(page.entries) for page in (first, second, third)] == [2, 2, 1]
    assert third.next_marker is None
    kinds = [item.kind for page in (first, second, third) for item in page.entries]
    assert kinds == ["folder", "file", "file", "file", "web_link"]


async def test_the_items_request_asks_for_marker_paging_and_only_the_fields_it_needs(monkeypatch) -> None:
    box = FakeBox()
    seen = []
    handle = box.handle

    async def spy(request):
        seen.append(dict(request.url.params))
        return await handle(request)

    monkeypatch.setattr(box, "handle", spy)
    await content_of(box).folder_items(T, "2001")

    assert seen[-1]["usemarker"] == "true" and seen[-1]["limit"] == "1000"
    assert seen[-1]["fields"] == content_module.ITEM_FIELDS


async def test_a_download_streams_the_bytes_in_chunks_without_the_token_on_the_download_host() -> None:
    box = FakeBox()
    box.content["3001"] = b"x" * (DOWNLOAD_CHUNK + 5)
    content = content_of(box)

    async with content.download(T, "3001") as chunks:
        sizes = [len(chunk) async for chunk in chunks]

    assert sizes == [DOWNLOAD_CHUNK, 5]
    assert box.violations == []  # no token to boxcloud, no write


async def test_a_download_of_a_file_that_cannot_be_read_is_no_access() -> None:
    content = content_of(FakeBox())

    with pytest.raises(BoxError) as error:
        async with content.download(T, "9999"):
            pass

    assert error.value.code is BoxCode.NO_ACCESS


async def test_a_download_that_is_not_ready_waits_and_then_streams() -> None:
    box = FakeBox()
    box.not_ready = 2

    async with content_of(box).download(T, "3001") as chunks:
        data = b"".join([chunk async for chunk in chunks])

    assert data == box.content["3001"]


async def test_a_download_that_stays_not_ready_ends_when_the_retries_run_out() -> None:
    box = FakeBox()
    box.not_ready = 99

    with pytest.raises(BoxError) as error:
        async with content_of(box).download(T, "3001"):
            pass

    assert error.value.code is BoxCode.NOT_READY


async def test_a_throttled_read_waits_for_retry_after_and_goes_on() -> None:
    box = FakeBox()
    box.throttle = 2

    assert (await content_of(box).file(T, "3001")).id == "3001"
    assert [path for _, path in box.calls].count("/2.0/files/3001") == 3


async def test_a_wait_budget_that_is_spent_ends_a_throttled_read() -> None:
    box = FakeBox()
    box.throttle = 99

    assert await code_of(content_of(box).file(T, "3001", budget=WaitBudget(1.5))) is BoxCode.UNREACHABLE


async def test_box_down_is_unreachable() -> None:
    box = FakeBox()
    box.status = 503

    assert await code_of(content_of(box).folder(T, "2001")) is BoxCode.UNREACHABLE


# Box Notes -------------------------------------------------------------------------------------------------------


def note_box(*states: str) -> NoteBox:
    box = NoteBox()
    box.states["3002"] = list(states)
    return box


async def test_a_ready_box_note_is_read_as_text_with_the_extracted_text_hint() -> None:
    box = note_box("success")

    text = await content_of(box).extracted_text(T, "3002", max_bytes=1000)

    assert text == "text of the note" and box.hints == ["[extracted_text]"] and box.started == []


async def test_a_note_without_a_representation_is_asked_to_start_then_polled_until_ready() -> None:
    box = note_box("none", "pending", "success")

    text = await content_of(box).extracted_text(T, "3002", max_bytes=1000)

    assert text == "text of the note"
    assert box.started == ["3002"] and box.asks == 3


async def test_a_note_that_stays_pending_ends_after_three_polls() -> None:
    box = note_box("pending")

    assert await code_of(content_of(box).extracted_text(T, "3002", max_bytes=1000)) is BoxCode.UNREACHABLE
    assert box.asks == 4  # the first ask and three polls


async def test_a_note_whose_representation_failed_ends_at_once() -> None:
    box = note_box("error")

    assert await code_of(content_of(box).extracted_text(T, "3002", max_bytes=1000)) is BoxCode.UNREACHABLE
    assert box.asks == 1


async def test_the_text_of_a_note_is_cut_to_the_cap() -> None:
    box = note_box("success")
    box.representations["3002"] = "é" * 100

    text = await content_of(box).extracted_text(T, "3002", max_bytes=11)

    assert len(text.encode()) <= 11 + 2 and text.startswith("éééé")


async def test_a_representation_url_outside_box_is_refused(monkeypatch) -> None:
    box = note_box("success")
    original = box.handle

    async def rewrite(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if "x-rep-hints" not in request.headers:
            return response
        body = json.loads(response.content)
        body["representations"]["entries"][0]["content"]["url_template"] = "https://evil.example/{+asset_path}"
        return httpx.Response(200, json=body)

    monkeypatch.setattr(box, "handle", rewrite)

    assert await code_of(content_of(box).extracted_text(T, "3002", max_bytes=10)) is BoxCode.REFUSED


async def test_the_content_reads_are_get_only() -> None:
    box = note_box("none", "success")
    content = content_of(box)
    add_folder(box, "7", [folder_entry("8", "x"), file_entry("9", "a.pdf")])
    add_file(box, FILE_INFO, b"%PDF")

    await content.folder(T, "7")
    await content.folder_items(T, "7")
    await content.extracted_text(T, "3002", max_bytes=100)
    async with content.download(T, "3001") as chunks:
        [chunk async for chunk in chunks]

    assert box.violations == [] and {method for method, _ in box.calls} == {"GET"}


# The Note content host ---------------------------------------------------------------------------------------------


def note_with_template(monkeypatch, template: str) -> tuple[NoteBox, list[str]]:
    """A ready Note whose ``url_template`` is ``template``; the second value lists every host the client called."""

    box = note_box("success")
    hosts: list[str] = []
    original = box.handle

    async def rewrite(request: httpx.Request) -> httpx.Response:
        hosts.append(f"{request.url.scheme}://{request.url.netloc.decode()}{request.url.path}")
        response = await original(request)
        if "x-rep-hints" not in request.headers:
            return response
        body = json.loads(response.content)
        body["representations"]["entries"][0]["content"]["url_template"] = template
        return httpx.Response(200, json=body)

    monkeypatch.setattr(box, "handle", rewrite)
    return box, hosts


async def test_the_note_text_is_fetched_from_dl_boxcloud_com_with_the_token() -> None:
    box = note_box("success")

    assert await content_of(box).extracted_text(T, "3002", max_bytes=1000) == "text of the note"

    assert box.content_auth == [f"Bearer {BEARER}"]
    assert ("GET", "/api/2.0/internal_files/3002/representations/extracted_text/content/") in box.calls
    assert not any(path.startswith("/2.0/internal_files/") and path.endswith("/content/") for _, path in box.calls)


@pytest.mark.parametrize(
    "template",
    [
        "https://dl.boxcloud.com.evil.com/api/2.0/internal_files/3002/content/{+asset_path}",
        "https://evil.com/dl.boxcloud.com/api/2.0/internal_files/3002/content/{+asset_path}",
        "http://dl.boxcloud.com/api/2.0/internal_files/3002/content/{+asset_path}",
        "https://dl.boxcloud.com:8443/api/2.0/internal_files/3002/content/{+asset_path}",
        "https://user@dl.boxcloud.com/api/2.0/internal_files/3002/content/{+asset_path}",
        "https://dl.boxcloud.com@evil.com/api/2.0/internal_files/3002/content/{+asset_path}",
        "https://dl.boxcloud.com/d/1/signed/3002",
        "https://dl.boxcloud.com/api/2.0/files/3002/content",
        "https://dl.boxcloud.com/api/2.0/internal_files/../../files/3002/content",
        "https://api.box.com/2.0/internal_files/3002/representations/extracted_text/content/{+asset_path}",
        "https://app.box.com/api/2.0/internal_files/3002/content/{+asset_path}",
    ],
)
async def test_the_token_never_goes_to_another_host_or_path_for_the_note_text(monkeypatch, template: str) -> None:
    box, hosts = note_with_template(monkeypatch, template)

    assert await code_of(content_of(box).extracted_text(T, "3002", max_bytes=10)) is BoxCode.REFUSED

    assert box.content_auth == [] and len(hosts) == 1  # only the representation list was asked
    assert not any("evil" in seen for seen in hosts)


async def test_a_401_from_the_content_host_is_refused_and_asks_for_no_refresh() -> None:
    box = note_box("success")
    box.content_status = 401

    assert await code_of(content_of(box).extracted_text(T, "3002", max_bytes=10)) is BoxCode.REFUSED
    assert box.refreshes == 0 and len(box.content_auth) == 1


async def test_a_note_content_fetch_that_fails_is_never_an_access_code() -> None:
    for status in (403, 404, 500):
        box = note_box("success")
        box.content_status = status

        code = await code_of(content_of(box).extracted_text(T, "3002", max_bytes=10))

        assert code in (BoxCode.REFUSED, BoxCode.UNREACHABLE)


async def test_the_note_polls_sleep_through_the_client_and_are_charged_to_the_budget() -> None:
    box = note_box("pending", "pending", "success")
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    box.access[BEARER] = {}
    client = BoxClient("id", SecretStr("secret"), "http://localhost/cb", transport=box.transport(), sleep=record)
    budget = WaitBudget()

    assert await BoxContent(client).extracted_text(T, "3002", max_bytes=1000, budget=budget) == "text of the note"

    assert waits == [content_module.POLL_SECONDS] * 2
    assert budget.left == WaitBudget().left - 2 * content_module.POLL_SECONDS


async def test_a_poll_that_does_not_fit_the_budget_ends_the_note_unreachable() -> None:
    box = note_box("pending")
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    box.access[BEARER] = {}
    client = BoxClient("id", SecretStr("secret"), "http://localhost/cb", transport=box.transport(), sleep=record)

    code = await code_of(BoxContent(client).extracted_text(T, "3002", max_bytes=10, budget=WaitBudget(15)))

    assert code is BoxCode.UNREACHABLE and waits == [content_module.POLL_SECONDS]
