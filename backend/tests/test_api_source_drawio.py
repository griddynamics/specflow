"""``POST /projects/{pid}/sources/drawio[/check]`` over the REST app: any member, the closed codes of a link, a fetch
and a decode, the source created for the caller, and one document per page from the importer.

The decoder is the converter's (``file_parsers.diagrams``); here it is replaced by ``fake_diagram`` so the route and the
importer are tested alone. Standings as in test_api_source_jira_epic: alice owns ``Alpha``, ben is a collaborator,
carol is no member.

Sequence::

    web -> project_member -> parse link -> (#U: safe fetch) -> decode -> 200 check | 201 source card
    add: -> runtime.imports.add_source(connected_by = caller, link without fragment) -> start(job as the caller)
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import uuid
import zlib
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
import pytest_asyncio

from api.imports import ImportFailedError, ImportJob, SourceGoneError
from api.imports.drawio import MXFILE_MEDIA_TYPE, DrawioImport, DrawioImporter
from api.routers import source_drawio
from atlassian_import_support import SOURCE_ID, FakeAtlassian, RecordingWriter, as_writer, wire_import_rig
from db import source_files
from file_parsers import diagrams
from file_parsers.diagrams import MAX_INPUT_BYTES, MAX_LINK_BYTES, DiagramDecodeError, decode_link_payload
from file_parsers.diagrams.fake import fake_diagram
from integrations.safe_fetch import SafeFetchError
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
CANARY = "link-canary-5e2a"
RLINK = f"https://app.diagrams.net/?lightbox=1#R{CANARY}"
ULINK = f"https://app.diagrams.net/#Uhttps%3A%2F%2Ffiles.example.com%2F{CANARY}.drawio"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        made = await wire_import_rig(served, monkeypatch, FakeAtlassian(), alpha=uuid.UUID(ALPHA), kind="drawio")
        made.payloads, made.files, made.fetched = [], [], []

        def payload(text: str) -> Any:
            made.payloads.append(text)
            return fake_diagram()

        def decode(data: bytes, *, hint: str) -> Any:
            made.files.append((data, hint))
            return fake_diagram()

        async def fetch(url: str, *, max_bytes: int, timeout_s: float) -> bytes:
            made.fetched.append((url, max_bytes, timeout_s))
            return b"<mxfile/>"

        monkeypatch.setattr(diagrams, "decode_link_payload", payload)
        monkeypatch.setattr(diagrams, "decode", decode)
        monkeypatch.setattr(source_drawio, "fetch_public", fetch)
        yield made


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/drawio{tail}"


def body(link: str) -> dict[str, str]:
    return {"link": link}


async def test_the_check_answers_the_name_and_pages_with_their_counts_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, body(f"  {RLINK}  "))

        assert response.status_code == 200, response.text
        assert response.json() == {
            "name": "Flow",
            "pages": [{"name": "Main", "shapes": 3, "arrows": 2}, {"name": "Second", "shapes": 1, "arrows": 0}],
            "capped": False,
        }
    assert rig.payloads == [CANARY, CANARY] and rig.imports.added == []


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, body(RLINK))

    assert error_of(response) == (404, "project_not_found") and rig.payloads == [] and rig.fetched == []


async def test_a_url_link_is_fetched_with_the_cap_and_a_timeout_then_decoded(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, body(ULINK))

    assert response.status_code == 200
    ((url, cap, timeout),) = rig.fetched
    assert url == f"https://files.example.com/{CANARY}.drawio" and cap == MAX_INPUT_BYTES and 0 < timeout <= 15
    assert rig.files == [(b"<mxfile/>", "diagram.drawio")]


@pytest.mark.parametrize(
    ("data", "hint"),
    [
        (b"\x89PNG\r\n\x1a\nrest", "diagram.drawio.png"),
        (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>', "diagram.drawio.svg"),
        (b"  <svg/>", "diagram.drawio.svg"),
        (b"\xef\xbb\xbf<svg/>", "diagram.drawio.svg"),
        (b"\xef\xbb\xbf<?xml version='1.0'?><svg/>", "diagram.drawio.svg"),
        (b"<mxfile/>", "diagram.drawio"),
    ],
)
async def test_the_fetched_bytes_are_decoded_as_the_kind_they_are(rig, monkeypatch, data, hint) -> None:
    async def fetch(url: str, **_: Any) -> bytes:
        return data

    monkeypatch.setattr(source_drawio, "fetch_public", fetch)

    assert (await call(rig, "POST", path(rig, "/check"), ALICE, body(ULINK))).status_code == 200
    assert rig.files == [(data, hint)]


@pytest.mark.parametrize(
    ("link", "status", "code"),
    [
        ("not a link", 422, "drawio_link_invalid"),
        ("http://app.diagrams.net/#Rabc", 422, "drawio_link_invalid"),
        ("https://u:p@app.diagrams.net/#Rabc", 422, "drawio_link_invalid"),
        ("https://app.diagrams.net.evil.com/#Rabc", 422, "drawio_link_invalid"),
        ("https://app.diagrams.net/", 422, "drawio_link_invalid"),
        ("https://app.diagrams.net/#G1abc", 422, "drawio_link_unsupported"),
        ("https://app.diagrams.net/#Hme/repo/main/a.drawio", 422, "drawio_link_unsupported"),
        ("https://app.diagrams.net/#R" + "a" * MAX_LINK_BYTES, 413, "drawio_too_large"),
    ],
)
async def test_a_link_that_cannot_be_used_is_refused_before_any_decode_or_fetch(rig, link, status, code) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, body(link))

        assert error_of(response) == (status, code)
    assert rig.payloads == [] and rig.fetched == [] and rig.imports.added == []


async def test_an_unsupported_storage_is_named_in_the_message(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, body("https://app.diagrams.net/#G1abc"))

    assert "Google Drive" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "case",
    [
        ("blocked_address", 409, "drawio_fetch_failed"),
        ("unreachable", 409, "drawio_fetch_failed"),
        ("timeout", 409, "drawio_fetch_failed"),
        ("bad_status", 409, "drawio_fetch_failed"),
        ("bad_type", 409, "drawio_fetch_failed"),
        ("too_large", 413, "drawio_too_large"),
        ("invalid_url", 422, "drawio_link_invalid"),
    ],
)
async def test_a_failed_fetch_is_a_closed_code_without_the_link(rig, monkeypatch, caplog, case) -> None:
    fetch_code, status, code = case

    async def fetch(*_a: Any, **_k: Any) -> bytes:
        raise SafeFetchError(cast(Any, fetch_code))

    monkeypatch.setattr(source_drawio, "fetch_public", fetch)

    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, body(ULINK))

        assert error_of(response) == (status, code) and CANARY not in response.text
    assert rig.imports.added == [] and CANARY not in log_haystack(caplog)


@pytest.mark.parametrize(
    ("decode_code", "status", "code"),
    [("not_a_diagram", 422, "drawio_not_a_diagram"), ("too_large", 413, "drawio_too_large")],
)
async def test_a_failed_decode_is_a_closed_code(rig, monkeypatch, decode_code, status, code) -> None:
    def failing(*_a: Any, **_k: Any) -> Any:
        raise DiagramDecodeError(cast(Any, decode_code))

    monkeypatch.setattr(diagrams, "decode_link_payload", failing)

    for tail in ("/check", ""):
        assert error_of(await call(rig, "POST", path(rig, tail), ALICE, body(RLINK))) == (status, code)
    assert rig.imports.added == []


async def test_a_diagram_without_pages_is_not_a_diagram(rig, monkeypatch) -> None:
    monkeypatch.setattr(diagrams, "decode_link_payload", lambda _text: diagrams.Diagram("Empty", ()))

    assert error_of(await call(rig, "POST", path(rig), ALICE, body(RLINK))) == (422, "drawio_not_a_diagram")
    assert rig.imports.added == []


async def test_adding_creates_the_source_for_the_caller_with_the_link_stored_without_its_fragment(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, body(RLINK))

    assert response.status_code == 201, response.text
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "drawio",
        "Flow",
        "https://app.diagrams.net/?lightbox=1",
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"]) == (rig.alpha, rig.ben.id, "drawio")
    assert CANARY not in added["link"] and "#" not in added["link"]
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, rig.ben.id, "drawio")
    assert isinstance(importer, DrawioImporter)


async def test_a_url_link_is_stored_as_scheme_host_path_only(rig) -> None:
    link = ULINK + quote("?token=s3cret#frag", safe="")

    assert (await call(rig, "POST", path(rig), ALICE, body(link))).status_code == 201

    assert rig.imports.added[0]["link"] == f"https://files.example.com/{CANARY}.drawio"


@pytest.mark.parametrize(
    ("name", "page", "expected"),
    [("", "First", "First"), ("  ", "  ", "diagrams.net diagram"), ("Named", "First", "Named")],
)
async def test_the_source_is_named_by_the_diagram_then_its_only_page_then_a_default(
    rig, monkeypatch, name, page, expected
) -> None:
    base = fake_diagram()
    first = base.pages[0]
    shaped = diagrams.Diagram(
        name, (diagrams.Page(first.id, page, first.shapes, first.arrows, original=first.original),)
    )
    monkeypatch.setattr(diagrams, "decode_link_payload", lambda _text: shaped)

    assert (await call(rig, "POST", path(rig), ALICE, body(RLINK))).status_code == 201
    assert rig.imports.added[0]["name"] == expected


async def test_the_importer_stores_one_document_per_page_each_with_its_own_original(rig, monkeypatch) -> None:
    monkeypatch.setattr("api.imports.drawio.to_markdown", lambda page, diagram: f"# {diagram.name} / {page.name}")
    assert (await call(rig, "POST", path(rig), ALICE, body(RLINK))).status_code == 201
    (job, importer) = rig.imports.started[0]
    media: list[str] = []

    class Recording(RecordingWriter):
        async def add(self, external_id: str, *, original_media_type: str = "", **values: Any) -> str:
            media.append(original_media_type)
            return await super().add(external_id, **values)

    writer = Recording()
    await importer(job, as_writer(writer))

    assert writer.declared == [("page:p1", "Flow · Main"), ("page:p2", "Flow · Second")]
    assert list(writer.added) == ["page:p1", "page:p2"] and writer.failed == []
    assert writer.added["page:p1"] == ("Flow · Main", "# Flow / Main", fake_diagram().pages[0].original)
    assert writer.added["page:p2"][2] == fake_diagram().pages[1].original
    assert writer.added["page:p1"][2] != writer.added["page:p2"][2]
    assert media == [MXFILE_MEDIA_TYPE, MXFILE_MEDIA_TYPE] == ["application/vnd.jgraph.mxfile"] * 2


async def test_two_pages_with_the_same_id_are_two_items() -> None:
    page = fake_diagram().pages[0]
    twin = diagrams.Page(page.id, "Twin", page.shapes, page.arrows, original=b"other")
    importer = DrawioImporter(DrawioImport(diagrams.Diagram("D", (page, twin))))
    writer = RecordingWriter()
    job = ImportJob(uuid.UUID(int=1), SOURCE_ID, uuid.UUID(int=3), "drawio")

    await importer(job, as_writer(writer))

    assert len(writer.added) == 2 and len({item for item, _ in writer.declared}) == 2


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    assert error_of(await call(rig, "POST", path(rig), ALICE, body(RLINK))) == (404, "source_not_found")


async def test_the_body_is_closed_and_neither_the_link_nor_its_host_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": RLINK, "x": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422

    await call(rig, "POST", path(rig, "/check"), ALICE, body(ULINK))
    await call(rig, "POST", path(rig), ALICE, body(RLINK))
    await call(rig, "POST", path(rig), ALICE, body(ULINK))
    await call(rig, "POST", path(rig), ALICE, body("https://app.diagrams.net/#G" + CANARY))

    haystack = log_haystack(caplog)
    assert CANARY not in haystack and "files.example.com" not in haystack and "Second" not in haystack
    assert "api.imports.added" in haystack


async def test_the_added_event_carries_ids_the_kind_and_counts(rig, caplog) -> None:
    caplog.set_level(logging.INFO)

    await call(rig, "POST", path(rig), ALICE, body(RLINK))

    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "api.imports.added"]
    fields = record.__dict__
    assert (fields["kind"], fields["pages"], fields["capped"]) == ("drawio", 2, False)


def _two_page_link() -> str:
    """A real ``#R`` link: the flow fixture as page one and a copy of it as page two, raw-deflated and base64."""

    xml = (Path(__file__).parent / "fixtures" / "drawio" / "flow.drawio").read_text(encoding="utf-8")
    page = re.search(r"<diagram .*?</diagram>", xml, re.DOTALL)
    assert page is not None
    second = page.group(0).replace('id="d1"', 'id="d2"').replace('name="Flow"', 'name="Copy"')
    second = second.replace('value="', 'value="Copy ')
    packed = zlib.compressobj(wbits=-15)
    raw = packed.compress(quote(xml.replace(page.group(0), page.group(0) + second), safe="").encode()) + packed.flush()
    return f"https://app.diagrams.net/#R{quote(base64.b64encode(raw).decode(), safe='')}"


async def test_a_real_link_is_checked_and_added_with_the_real_decoder(rig, monkeypatch) -> None:
    monkeypatch.setattr(diagrams, "decode_link_payload", decode_link_payload)
    link = _two_page_link()

    checked = await call(rig, "POST", path(rig, "/check"), ALICE, body(link))
    assert checked.status_code == 200, checked.text
    assert [page["name"] for page in checked.json()["pages"]] == ["Flow", "Copy"]
    assert (await call(rig, "POST", path(rig), ALICE, body(link))).status_code == 201

    (job, importer) = rig.imports.started[0]
    writer = RecordingWriter()
    await importer(job, as_writer(writer))

    assert len(writer.added) == 2 and writer.failed == []
    originals = [values[2] for values in writer.added.values()]
    assert originals[0] != originals[1] and all(originals)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (f"https://app.diagrams.net/?title=Order%20flow.drawio#R{CANARY}", "Order flow"),
        ("https://app.diagrams.net/#Uhttps%3A%2F%2Ffiles.example.com%2Fmy-plan.drawio", "my-plan"),
    ],
)
async def test_the_check_the_source_and_the_items_agree_on_the_link_name(rig, link, expected) -> None:
    checked = await call(rig, "POST", path(rig, "/check"), ALICE, body(link))
    assert checked.json()["name"] == expected
    assert (await call(rig, "POST", path(rig), ALICE, body(link))).status_code == 201
    assert rig.imports.added[0]["name"] == expected
    (job, importer) = rig.imports.started[0]
    writer = RecordingWriter()

    await importer(job, as_writer(writer))

    assert writer.declared == [("page:p1", f"{expected} · Main"), ("page:p2", f"{expected} · Second")]


async def test_a_body_far_over_the_link_limit_is_refused_by_validation_before_it_is_read(rig) -> None:
    link = "https://app.diagrams.net/#R" + "a" * (2 * MAX_LINK_BYTES)

    response = await call(rig, "POST", path(rig, "/check"), ALICE, body(link))

    assert error_of(response)[0] == 422 and error_of(response)[1] == "validation_failed" and rig.payloads == []


async def test_a_link_between_the_limit_and_twice_it_is_drawio_too_large(rig) -> None:
    link = "https://app.diagrams.net/#R" + "a" * (MAX_LINK_BYTES + 10)

    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body(link))) == (413, "drawio_too_large")


async def test_decoding_and_rendering_run_off_the_event_loop(rig, monkeypatch) -> None:
    main = threading.get_ident()
    seen: dict[str, int] = {}

    def payload(text: str) -> Any:
        seen["decode"] = threading.get_ident()
        return fake_diagram()

    def render(page: Any, diagram: Any) -> str:
        seen["render"] = threading.get_ident()
        return "# x"

    monkeypatch.setattr(diagrams, "decode_link_payload", payload)
    monkeypatch.setattr("api.imports.drawio.to_markdown", render)
    assert (await call(rig, "POST", path(rig), ALICE, body(RLINK))).status_code == 201
    (job, importer) = rig.imports.started[0]
    await importer(job, as_writer(RecordingWriter()))

    assert seen["decode"] != main and seen["render"] != main


async def test_a_page_that_renders_over_its_cap_ends_the_import_unknown_error_before_anything_is_stored(
    rig, monkeypatch
) -> None:
    def too_big(page: Any, diagram: Any) -> str:
        raise DiagramDecodeError("too_large")

    monkeypatch.setattr("api.imports.drawio.to_markdown", too_big)
    assert (await call(rig, "POST", path(rig), ALICE, body(RLINK))).status_code == 201
    (job, importer) = rig.imports.started[0]
    writer = RecordingWriter()

    with pytest.raises(ImportFailedError) as caught:
        await importer(job, as_writer(writer))

    assert caught.value.reason == "unknown_error" and writer.declared == [] and writer.added == {}


async def test_a_source_removed_during_the_import_stops_it(rig) -> None:
    assert (await call(rig, "POST", path(rig), ALICE, body(RLINK))).status_code == 201
    (job, importer) = rig.imports.started[0]

    class Removed(RecordingWriter):
        async def add(self, external_id: str, **values: Any) -> str:
            if self.added:
                raise SourceGoneError
            return await super().add(external_id, **values)

    writer = Removed()
    with pytest.raises(SourceGoneError):
        await importer(job, as_writer(writer))

    assert list(writer.added) == ["page:p1"]
