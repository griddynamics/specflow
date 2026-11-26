"""``POST /projects/{pid}/sources/box[/check]`` over the REST app: any member (D6), the acting account is the member the
project's Box is bound to (R1), the closed ``box_*`` codes of a link and of a refused read, the shared link that never
becomes ``connections.link``, and the import handed to ``runtime.imports`` with the bound member as the reader and the
caller as the one who added it.

Standings as in test_api_source_confluence: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> Box service on? -> bound Box member (active?) -> link -> Box (check: name, files, skipped)
    add: -> runtime.imports.add_source(connected_by = caller, link = canonical) -> start(job as the bound member) -> 201
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports import box as box_module
from api.imports.box import BoxImporter
from api.schemas import BOX_EXCLUDE_MAX
from atlassian_import_support import SOURCE_ID, FakeImports
from box_import_support import MEMBER, BoxWriter, FakeConnections, NoteBox, as_writer, file_entry, folder_entry
from box_support import ENTERPRISE_ID, SHARED_ITEM_FOLDER, SHARED_LINK_CANARY
from db import source_files
from integrations_db_fakes import FakeIntegrationsDb
from source_rig_support import wire_source_rig
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
FOLDER_LINK = "https://app.box.com/folder/2001"
SHARED_LINK = "https://app.box.com/s/abc123shared"
CANARY = "folder-name-canary-5e2"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        box = NoteBox()
        box.folders["2001"] = {"type": "folder", "id": "2001", "name": CANARY, "item_status": "active"}
        box.items["2001"] = [folder_entry("2002", "Sub"), file_entry("1", "a.pdf"), file_entry("2", "x.png")]
        box.items["2002"] = [file_entry("3", "b.docx")]
        box.content["1"] = b"%PDF-1.7"
        box.shared[SHARED_LINK] = {**SHARED_ITEM_FOLDER, "name": CANARY}
        runtime = served.runtime
        connections, integrations, imports = FakeConnections(box), FakeIntegrationsDb(), FakeImports()
        integrations.install(monkeypatch)
        integrations.put(uuid.UUID(ALPHA), "box", MEMBER, workspace_id=ENTERPRISE_ID)
        runtime.box = connections
        runtime.imports = imports

        alpha, ben = await wire_source_rig(monkeypatch, served, "box", imports)
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            box=box,
            connections=connections,
            integrations=integrations,
            imports=imports,
            alpha=alpha,
            ben=ben,
        )


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/box{tail}"


async def test_the_check_of_a_folder_answers_its_name_and_the_counts_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, {"link": FOLDER_LINK})

        assert response.status_code == 200
        assert response.json() == {
            "kind": "folder",
            "name": CANARY,
            "files": [
                {"id": "1", "name": "a.pdf", "path": "", "category": "pdf"},
                {"id": "3", "name": "b.docx", "path": "Sub", "category": "word"},
            ],
            "files_capped": False,
            "skipped": 1,
            "folders": 1,
            "bytes": 20,
            "capped": False,
        }


async def test_the_check_of_a_shared_link_names_the_folder_it_resolves_to(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": f"  {SHARED_LINK}  "})

    assert response.status_code == 200 and response.json()["name"] == CANARY and response.json()["kind"] == "folder"
    assert set(rig.box.shared_headers) == {f"shared_link={SHARED_LINK}"}


async def test_the_check_of_a_file_link_is_one_file(rig) -> None:
    rig.box.files["1"] = file_entry("1", "Spec.pdf", 5)

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": "https://app.box.com/file/1"})

    assert response.json() == {
        "kind": "file",
        "name": "Spec.pdf",
        "files": [{"id": "1", "name": "Spec.pdf", "path": "", "category": "pdf"}],
        "files_capped": False,
        "skipped": 0,
        "folders": 0,
        "bytes": 5,
        "capped": False,
    }


async def test_the_check_lists_at_most_the_file_cap_and_says_when_there_are_more(rig, monkeypatch) -> None:
    monkeypatch.setattr(box_module, "MAX_FILES", 1)

    body = (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK})).json()

    assert [f["id"] for f in body["files"]] == ["1"]
    assert (body["files_capped"], body["capped"]) == (True, True)


async def test_the_check_ignores_exclude(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK, "exclude": ["1", "3"]})

    assert response.status_code == 200 and [f["id"] for f in response.json()["files"]] == ["1", "3"]


@pytest.mark.parametrize("exclude", [["id:1"], ["1a"], [""], [1], ["1"] * (BOX_EXCLUDE_MAX + 1), "1"])
async def test_a_bad_exclude_is_refused_before_any_read(rig, exclude) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_LINK, "exclude": exclude})

    assert error_of(response) == (422, "validation_failed")
    assert rig.box.calls == [] and rig.imports.added == []


async def test_the_exclude_cap_is_the_file_cap() -> None:
    assert BOX_EXCLUDE_MAX == box_module.MAX_FILES


async def test_adding_without_the_files_the_member_unchecked_skips_them_entirely(rig) -> None:
    rig.box.content["3"] = b"docx bytes"

    response = await call(rig, "POST", path(rig), BEN, {"link": FOLDER_LINK, "exclude": ["1", "42"]})

    assert response.status_code == 201
    (job, importer) = rig.imports.started[0]
    writer = BoxWriter()
    await importer(job, as_writer(writer))
    assert [external for external, _ in writer.declared] == ["file:3", "file:2"]
    assert list(writer.files) == ["file:3"] and writer.parsed == ["file:3"]
    assert not any(call_path.endswith("/1/content") for _, call_path in rig.box.calls)


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"link": FOLDER_LINK})

    assert error_of(response) == (404, "project_not_found") and rig.box.calls == []


@pytest.mark.parametrize(
    "link",
    [
        "https://app.box.com.evil.io/folder/2001",
        "http://app.box.com/folder/2001",
        "https://app.box.com/folder/0",
        "https://user:pw@app.box.com/folder/2001",
        "https://app.box.com/notes/2001",
        "nonsense",
    ],
)
async def test_a_link_that_cannot_be_used_is_refused_before_any_read(rig, link) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": link})

        assert error_of(response) == (422, "box_link_invalid")
    assert rig.box.calls == [] and rig.imports.added == []


async def test_a_web_link_a_protected_link_and_an_unreadable_target_are_closed_codes(rig) -> None:
    rig.box.shared["https://app.box.com/s/bookmark"] = {"type": "web_link", "id": "4", "name": "Wiki"}
    rig.box.shared["https://app.box.com/s/locked"] = {
        **SHARED_ITEM_FOLDER,
        "shared_link": {"is_password_enabled": True},
    }
    cases = [
        ("https://app.box.com/s/bookmark", (422, "box_link_invalid")),
        ("https://app.box.com/s/locked", (422, "box_link_password")),
        ("https://app.box.com/s/unknown", (409, "box_no_access")),
        ("https://app.box.com/folder/9999", (409, "box_no_access")),
        ("https://app.box.com/file/9999", (409, "box_no_access")),
    ]
    for link, expected in cases:
        assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, {"link": link})) == expected
        assert error_of(await call(rig, "POST", path(rig), ALICE, {"link": link})) == expected
    assert rig.imports.added == []


async def test_box_down_is_unreachable_and_an_expired_token_asks_to_connect_again(rig) -> None:
    rig.box.status = 503
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK})) == (
        502,
        "box_unreachable",
    )
    rig.box.status = None
    rig.connections.expired = True
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK})) == (
        409,
        "box_reconnect_required",
    )


async def test_without_a_bound_member_or_with_an_expired_connection_or_without_the_service_nothing_is_read(rig) -> None:
    body = {"link": FOLDER_LINK}
    rig.connections.status = "expired"
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (409, "box_reconnect_required")
    rig.connections.status = "active"
    rig.integrations.put(rig.alpha, "box", uuid.UUID(int=5))  # bound to a member with no connection
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "box_not_connected")
    rig.integrations.release(rig.alpha, uuid.UUID(int=5))
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "box_not_connected")
    rig.runtime.box = None
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (503, "box_unavailable")
    assert rig.box.calls == [] and rig.imports.added == []


async def test_adding_a_folder_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"link": FOLDER_LINK})

    assert response.status_code == 201
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "box",
        CANARY,
        FOLDER_LINK,
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"], added["name"]) == (
        rig.alpha,
        rig.ben.id,
        "box",
        CANARY,
    )
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, MEMBER, "box")
    assert isinstance(importer, BoxImporter)
    writer = BoxWriter()
    await importer(job, as_writer(writer))
    assert list(writer.files) == ["file:1"] and writer.failed == [("file:3", "no_access", "read")]
    assert writer.skipped == [("file:2", "x.png")]


async def test_a_shared_link_is_never_stored_as_the_link_of_the_source(rig) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": SHARED_LINK})

    assert response.status_code == 201
    (added,) = rig.imports.added
    assert added["link"] == "https://app.box.com/folder/2001" and added["name"] == CANARY
    assert "abc123shared" not in str(added) and "abc123shared" not in response.text
    # the importer still holds it, for this one run
    (job, importer) = rig.imports.started[0]
    writer = BoxWriter()
    await importer(job, as_writer(writer))
    assert "file:1" in writer.files
    assert f"shared_link={SHARED_LINK}" in rig.box.shared_headers


async def test_a_file_link_is_stored_as_its_canonical_file_address(rig) -> None:
    rig.box.files["1"] = file_entry("1", "Spec.pdf", 5)

    await call(rig, "POST", path(rig), ALICE, {"link": "https://app.box.com/file/1?s=secret"})

    assert rig.imports.added[0]["link"] == "https://app.box.com/file/1"


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_LINK})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_neither_name_nor_link_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK, "extra": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422
    assert (
        await call(rig, "POST", path(rig), ALICE, {"link": "https://app.box.com/s/" + "a" * 3000})
    ).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"link": SHARED_LINK})

    haystack = log_haystack(caplog)
    assert CANARY not in haystack and "abc123shared" not in haystack and SHARED_LINK_CANARY not in haystack


async def test_adding_resolves_the_link_but_never_lists_the_items_of_a_folder(rig) -> None:
    check = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_LINK})
    assert any(call_path.endswith("/items") for _, call_path in rig.box.calls) and check.status_code == 200
    rig.box.calls.clear()

    response = await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_LINK})

    assert response.status_code == 201
    assert rig.box.calls == [("GET", "/2.0/folders/2001")]  # the one read of the folder, no listing


async def test_a_member_who_reconnected_another_box_account_is_refused_by_check_and_add(rig) -> None:
    rig.connections.workspace_id = "another-enterprise"

    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": FOLDER_LINK})

        assert error_of(response) == (409, "integration_workspace_mismatch")
    assert rig.box.calls == [] and rig.imports.added == []
