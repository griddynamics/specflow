"""``POST /projects/{pid}/sources/dropbox[/check]`` over the REST app: any member (D6), the acting account is the member
the project's Dropbox is bound to (R1), the closed ``dropbox_*`` codes of a link and of a refused read, the shared
link that never becomes ``connections.link``, and the import handed to ``runtime.imports`` with the bound member as
the reader and the caller as the one who added it.

Standings as in test_api_source_box: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> Dropbox service on? -> bound member (active?) -> link -> Dropbox (check: name, files, caps)
    add: -> runtime.imports.add_source(connected_by = caller, link = /home address) -> start(bound member's job) -> 201
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports import dropbox as dropbox_module
from api.imports.dropbox import DropboxImporter
from atlassian_import_support import SOURCE_ID, FakeImports
from db import source_files
from dropbox.client import DropboxApiError
from dropbox.codes import DropboxCode
from dropbox.content import DropboxContent
from dropbox_import_support import (
    FOLDER_URL,
    MEMBER,
    DropboxWriter,
    FakeConnections,
    as_writer,
    dropbox,
)
from dropbox_support import (
    FOLDER,
    FOREIGN_LINK_CANARY,
    NAME_CANARY,
    PATH_CANARY,
    SHARED_LINK_CANARY,
    SUBFOLDER,
    file_entry,
)
from integrations_db_fakes import FakeIntegrationsDb
from source_rig_support import wire_source_rig
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
CANARY = "folder-name-canary-5e2"
SUB = "/Graphico Lab/Sub"
SHARED_PART = "rlkeycanary"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        fake = dropbox()
        named = {**FOLDER, "name": CANARY}
        fake.metadata[PATH_CANARY] = fake.metadata[FOLDER["id"]] = named
        fake.trees[PATH_CANARY] = [
            FOLDER,
            SUBFOLDER,
            file_entry("id:1", "a.pdf", 10),
            file_entry("id:2", "x.png", 10),
            file_entry("id:3", "b.docx", 10, folder=SUB),
        ]
        fake.content = {"id:1": b"%PDF-1.7"}
        runtime = served.runtime
        connections, integrations, imports = FakeConnections(fake), FakeIntegrationsDb(), FakeImports()
        integrations.install(monkeypatch)
        integrations.put(uuid.UUID(ALPHA), "dropbox", MEMBER, workspace_id=connections.workspace_id)
        runtime.dropbox = connections
        runtime.imports = imports

        alpha, ben = await wire_source_rig(monkeypatch, served, "dropbox", imports)
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            dropbox=fake,
            connections=connections,
            integrations=integrations,
            imports=imports,
            alpha=alpha,
            ben=ben,
        )


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/dropbox{tail}"


async def test_the_check_of_a_folder_answers_its_name_the_files_to_uncheck_and_the_counts_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, {"link": FOLDER_URL})

        assert response.status_code == 200
        assert response.json() == {
            "kind": "folder",
            "name": CANARY,
            "files": [
                {"id": "id:1", "name": "a.pdf", "path": "", "category": "pdf"},
                {"id": "id:3", "name": "b.docx", "path": "Sub", "category": "word"},
            ],
            "files_capped": False,
            "skipped": 1,
            "folders": 1,
            "bytes": 20,
            "capped": False,
        }


async def test_the_check_ignores_exclude_and_lists_every_file(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL, "exclude": ["id:1"]})

    assert [f["id"] for f in response.json()["files"]] == ["id:1", "id:3"]


async def test_the_check_of_a_shared_link_names_the_folder_it_resolves_to(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": f"  {SHARED_LINK_CANARY}  "})

    assert response.status_code == 200 and response.json()["name"] == CANARY and response.json()["kind"] == "folder"


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"link": FOLDER_URL})

    assert error_of(response) == (404, "project_not_found") and rig.dropbox.calls == []


@pytest.mark.parametrize(
    "link",
    [
        "https://www.dropbox.com.evil.io/home/Graphico%20Lab",
        "http://www.dropbox.com/home/Graphico%20Lab",
        "https://www.dropbox.com/home",
        "https://user:pw@www.dropbox.com/home/Graphico%20Lab",
        "https://www.dropbox.com/paper/d/abc",
        "nonsense",
    ],
)
async def test_a_link_that_cannot_be_used_is_refused_before_any_read(rig, link) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": link})

        assert error_of(response) == (422, "dropbox_link_invalid")
    assert rig.dropbox.calls == [] and rig.imports.added == []


async def test_a_link_that_is_not_in_the_members_dropbox_or_is_unreadable_is_no_access(rig) -> None:
    rig.dropbox.restricted.add("/restricted")
    rig.dropbox.metadata["/restricted"] = FOLDER
    for link in (
        FOREIGN_LINK_CANARY,
        "https://www.dropbox.com/scl/fo/unknown/AAA?rlkey=x",
        "https://www.dropbox.com/home/Missing",
        "https://www.dropbox.com/home/Restricted",
    ):
        for tail in ("/check", ""):
            response = await call(rig, "POST", path(rig, tail), ALICE, {"link": link})

            assert error_of(response) == (409, "dropbox_no_access")
    assert rig.imports.added == []


async def test_a_link_type_dropbox_does_not_resolve_is_link_invalid_and_other_refusals_no_access(
    rig, monkeypatch
) -> None:
    refusal = DropboxApiError(DropboxCode.UNSUPPORTED, ("unsupported_link_type",))

    async def refused(*_args: object, **_kwargs: object) -> object:
        raise refusal

    monkeypatch.setattr(DropboxContent, "shared_link", refused)
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": SHARED_LINK_CANARY})
        assert error_of(response) == (422, "dropbox_link_invalid")
    refusal = DropboxApiError(DropboxCode.REFUSED, ("something_else",))
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": SHARED_LINK_CANARY})
    assert error_of(response) == (409, "dropbox_no_access")
    assert rig.imports.added == []


async def test_dropbox_down_is_unreachable_and_an_expired_connection_asks_to_connect_again(rig) -> None:
    rig.dropbox.status = 503
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL})) == (
        502,
        "dropbox_unreachable",
    )
    rig.dropbox.status = None
    rig.connections.expired = True
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL})) == (
        409,
        "dropbox_reconnect_required",
    )


async def test_without_a_bound_member_or_with_an_expired_connection_or_without_the_service_nothing_is_read(rig) -> None:
    body = {"link": FOLDER_URL}
    rig.connections.status = "expired"
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (409, "dropbox_reconnect_required")
    rig.connections.status = "active"
    rig.integrations.put(rig.alpha, "dropbox", uuid.UUID(int=5))  # bound to a member with no connection
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "dropbox_not_connected")
    rig.integrations.release(rig.alpha, uuid.UUID(int=5))
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "dropbox_not_connected")
    rig.runtime.dropbox = None
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (503, "dropbox_unavailable")
    assert rig.dropbox.calls == [] and rig.imports.added == []


async def test_adding_a_folder_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"link": FOLDER_URL, "exclude": ["id:3"]})

    assert response.status_code == 201
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "dropbox",
        CANARY,
        "https://www.dropbox.com/home/Graphico%20Lab",
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"], added["name"]) == (
        rig.alpha,
        rig.ben.id,
        "dropbox",
        CANARY,
    )
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, MEMBER, "dropbox")
    assert isinstance(importer, DropboxImporter)
    writer = DropboxWriter()
    await importer(job, as_writer(writer))
    assert list(writer.files) == ["id:1"] and writer.skipped == [("id:2", "x.png")]  # id:3 was unchecked
    assert "id:3" not in [d[0] for d in writer.declared]


async def test_a_shared_link_is_never_stored_as_the_link_of_the_source(rig) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": SHARED_LINK_CANARY})

    assert response.status_code == 201
    (added,) = rig.imports.added
    assert added["link"] == "https://www.dropbox.com/home/Graphico%20Lab" and added["name"] == CANARY
    for part in (SHARED_PART, "sharedcanary7b4", "AAAkeycanary"):
        assert part not in str(added) and part not in response.text
    # the importer still holds it, for this one run
    (job, importer) = rig.imports.started[0]
    writer = DropboxWriter()
    await importer(job, as_writer(writer))
    assert "id:1" in writer.files
    sent = [body.get("url", "") for _, body in rig.dropbox.bodies if isinstance(body, dict)]
    assert any(SHARED_PART in url for url in sent)


async def test_a_file_link_is_stored_as_the_home_path_of_the_file(rig) -> None:
    rig.dropbox.metadata["/graphico lab/a.pdf"] = rig.dropbox.trees[PATH_CANARY][2]

    await call(rig, "POST", path(rig), ALICE, {"link": "https://www.dropbox.com/home/Graphico%20Lab/a.pdf?x=1"})

    assert rig.imports.added[0]["link"] == "https://www.dropbox.com/home/Graphico%20Lab/a.pdf"


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_URL})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_neither_name_nor_link_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL, "extra": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422
    assert (
        await call(rig, "POST", path(rig), ALICE, {"link": "https://www.dropbox.com/s/" + "a" * 3000})
    ).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_URL, "exclude": ["bad"]})).status_code == 422
    assert (
        await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_URL, "exclude": [f"id:{n}" for n in range(1001)]})
    ).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"link": SHARED_LINK_CANARY})

    haystack = log_haystack(caplog)
    assert CANARY not in haystack and NAME_CANARY not in haystack and SHARED_PART not in haystack
    assert SHARED_LINK_CANARY not in haystack


async def test_adding_resolves_the_link_but_never_lists_the_files_of_a_folder(rig) -> None:
    check = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL})
    assert any(p == "/2/files/list_folder" for _, p in rig.dropbox.calls) and check.status_code == 200
    rig.dropbox.calls.clear()

    response = await call(rig, "POST", path(rig), ALICE, {"link": FOLDER_URL})

    assert response.status_code == 201
    assert [p for _, p in rig.dropbox.calls] == ["/2/users/get_current_account", "/2/files/get_metadata"]


async def test_a_member_who_reconnected_another_dropbox_account_is_refused_by_check_and_add(rig) -> None:
    rig.connections.workspace_id = "user:another-account"

    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": FOLDER_URL})

        assert error_of(response) == (409, "integration_workspace_mismatch")
    assert rig.dropbox.calls == [] and rig.imports.added == []


async def test_the_check_answers_the_caps(rig, monkeypatch) -> None:
    monkeypatch.setattr(dropbox_module, "MAX_FILES", 1)

    body = (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": FOLDER_URL})).json()

    assert (body["files_capped"], body["capped"], len(body["files"])) == (True, True, 1)
    assert json.dumps(body).count("id:1") == 1
