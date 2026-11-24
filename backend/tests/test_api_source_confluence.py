"""``POST /projects/{pid}/sources/confluence[/check]`` over the REST app: any member (D6), the acting account is the
member the project's Atlassian is bound to (R1), the closed codes of a link and of a refused read, and the import
handed to ``runtime.imports`` with the bound member as the reader and the caller as the one who added it.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> bound Atlassian member -> link -> Confluence (check: title, pages, linked issues)
    add: -> runtime.imports.add_source(connected_by = caller) -> start(job as the bound member) -> 201 source card
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports import ImportJob
from api.imports.confluence import ConfluenceImporter
from atlassian_import_support import (
    ACTING,
    SITE,
    FakeAtlassian,
    FakeConnections,
    RecordingWriter,
    as_writer,
    install,
    storage_page,
)
from db import source_files
from integrations_db_fakes import FakeIntegrationsDb
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
PAGE_LINK = f"{SITE}/wiki/spaces/ORD/pages/5/Order+limits"
SOURCE_ID = uuid.UUID(int=900)
CANARY = "page-title-canary-3c9a"


class FakeImports:
    """``runtime.imports``: records the sources it was asked to add and the imports it was asked to start."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.started: list[tuple[ImportJob, Any]] = []

    async def add_source(self, project_id: uuid.UUID, user_id: uuid.UUID, **values: Any) -> uuid.UUID:
        self.added.append({"project_id": project_id, "user_id": user_id, **values})
        return SOURCE_ID

    def start(self, job: ImportJob, importer: Any) -> None:
        self.started.append((job, importer))


def card(user_id: uuid.UUID, name: str) -> source_files.SourceSummary:
    return source_files.SourceSummary(
        id=SOURCE_ID,
        kind="confluence",
        name=name,
        name_suffix=1,
        added_by_id=user_id,
        added_by_name="Alice",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        listing_finished=False,
        open=False,
        total=0,
        counts=dict.fromkeys(source_files.FILE_STATES, 0),
        failure_reason=None,
        folder_id=None,
        link=PAGE_LINK,
    )


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        runtime, repo = served.runtime, served.repo
        fake = FakeAtlassian(
            pages={
                "5": storage_page("5", CANARY, '<p><a href="https://example.atlassian.net/browse/PAY-7">t</a></p>'),
                "6": storage_page("6", "Child", "<p>child</p>"),
            },
            below={"5": [{"id": "6", "title": "Child", "type": "page"}]},
            spaces={"ORD": {"id": "10", "key": "ORD", "name": "Orders"}},
            space_pages={"10": [{"id": "1", "title": "Home"}]},
        )
        connections = FakeConnections()
        integrations = FakeIntegrationsDb()
        integrations.install(monkeypatch)
        alpha = uuid.UUID(ALPHA)
        integrations.put(alpha, "atlassian", ACTING)
        imports = FakeImports()
        runtime.jira = SimpleNamespace(connections=connections)
        runtime.imports = imports
        install(monkeypatch, fake)

        async def source_summary(_session: Any, _project: uuid.UUID, source_id: uuid.UUID) -> Any:
            return card(served.users["alice"].id, "Order limits") if source_id == SOURCE_ID else None

        monkeypatch.setattr(source_files, "source_summary", source_summary)
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        repo.members.add((alpha, ben.id))
        repo.roles[(alpha, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            fake=fake,
            connections=connections,
            integrations=integrations,
            imports=imports,
            alpha=alpha,
            ben=ben,
        )


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/confluence{tail}"


async def test_the_check_of_a_page_answers_its_title_pages_and_linked_issues_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, {"link": PAGE_LINK, "follow": True})

        assert response.status_code == 200
        assert response.json() == {"kind": "page", "title": CANARY, "pages": 2, "capped": False, "linked_issues": 1}


async def test_the_check_of_a_space_link_answers_the_space(rig) -> None:
    link = f"{SITE}/wiki/spaces/ORD/overview"

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": link})

    assert response.json() == {"kind": "space", "title": "Orders", "pages": 1, "capped": False, "linked_issues": 0}


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"link": PAGE_LINK})

    assert error_of(response) == (404, "project_not_found") and rig.fake.requests == []


@pytest.mark.parametrize(
    ("link", "status", "code"),
    [
        (f"{SITE}/wiki/x/AbCd", 422, "confluence_short_link_unsupported"),
        ("https://other.atlassian.net/wiki/spaces/ORD/pages/5/T", 422, "atlassian_site_mismatch"),
        (f"{SITE}/wiki/display/ORD/Old", 422, "confluence_link_invalid"),
        ("nonsense", 422, "confluence_link_invalid"),
    ],
)
async def test_a_link_that_cannot_be_used_is_refused_before_any_read(rig, link, status, code) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": link})

        assert error_of(response) == (status, code)
    assert rig.fake.requests == [] and rig.imports.added == []


@pytest.mark.parametrize(
    ("forced", "status", "code"),
    [
        (404, 409, "confluence_not_found"),
        (403, 409, "confluence_forbidden"),
        (503, 502, "atlassian_unreachable"),
        (400, 502, "atlassian_unreachable"),
    ],
)
async def test_a_refused_read_is_a_closed_code(rig, forced, status, code) -> None:
    rig.fake.status["/wiki/api/v2/pages/5"] = forced

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": PAGE_LINK})

    assert error_of(response) == (status, code)
    assert CANARY not in response.text


async def test_a_space_that_does_not_exist_is_not_found(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": f"{SITE}/wiki/spaces/NOPE/overview"})

    assert error_of(response) == (409, "confluence_not_found")


async def test_a_refused_token_asks_the_member_to_connect_again(rig) -> None:
    rig.fake.status["/wiki/api/v2/pages/5"] = 401
    rig.fake.expire_t1 = True

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": PAGE_LINK})

    assert error_of(response) == (409, "atlassian_reconnect_required")


async def test_without_a_bound_member_or_with_an_expired_connection_or_without_oauth_nothing_is_read(rig) -> None:
    body = {"link": PAGE_LINK}
    rig.connections.status = "expired"
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (409, "atlassian_reconnect_required")
    rig.connections.status = "active"
    rig.integrations.put(rig.alpha, "atlassian", uuid.UUID(int=5))
    assert error_of(await call(rig, "POST", path(rig, ""), ALICE, body)) == (409, "atlassian_not_connected")
    rig.integrations.release(rig.alpha, uuid.UUID(int=5))
    assert error_of(await call(rig, "POST", path(rig, ""), ALICE, body)) == (409, "atlassian_not_connected")
    rig.runtime.jira = SimpleNamespace(connections=None)
    assert error_of(await call(rig, "POST", path(rig, ""), ALICE, body)) == (503, "atlassian_oauth_unavailable")
    rig.runtime.jira = None
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (503, "atlassian_oauth_unavailable")
    assert rig.fake.requests == [] and rig.imports.added == []


async def test_adding_a_link_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"link": f"  {PAGE_LINK}  ", "follow": True})

    assert response.status_code == 201
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "confluence",
        "Order limits",
        PAGE_LINK,
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"], added["name"]) == (
        rig.alpha,
        rig.ben.id,
        "confluence",
        CANARY,
    )
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, ACTING, "confluence")
    assert isinstance(importer, ConfluenceImporter)
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert list(writer.added) == ["page:5", "page:6"] and writer.failed == [("issue:PAY-7", "no_access", "read")]


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"link": PAGE_LINK})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_the_title_never_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": PAGE_LINK, "extra": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"link": PAGE_LINK})

    assert CANARY not in log_haystack(caplog)
