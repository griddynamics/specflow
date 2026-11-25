"""``POST /projects/{pid}/sources/linear[/check]`` over the REST app: any member (D6), the acting account is the member
the project's Linear is bound to (R1), the closed codes of a link and a refused read, and the import handed to
``runtime.imports`` with the bound member as the reader and the caller as the one who added it.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> bound Linear member -> link -> Linear (check: kind, name, issues, documents)
    add: -> runtime.imports.add_source(connected_by = caller) -> start(job as the bound member) -> 201 source card
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports.linear_project import MAX_LINEAR_ISSUES, LinearProjectImporter
from api.routers.source_linear import EXCLUDE_MAX
from atlassian_import_support import SOURCE_ID, FakeImports, RecordingWriter, as_writer, source_card
from db import source_files
from integrations_db_fakes import FakeIntegrationsDb
from linear.errors import (
    LinearForbiddenError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear_import_support import ACTING, FakeLinearConnections, seed_workspace
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        space = await seed_workspace()
        alpha = uuid.UUID(ALPHA)
        connections, integrations, imports = FakeLinearConnections(space.fake), FakeIntegrationsDb(), FakeImports()
        integrations.install(monkeypatch)
        integrations.put(alpha, "linear", ACTING)
        served.runtime.linear = connections
        served.runtime.imports = imports

        async def source_summary(_session: Any, _project: uuid.UUID, source_id: uuid.UUID) -> Any:
            added = imports.added[-1] if imports.added else {"name": "", "link": ""}
            card = source_card("linear_project", served.users["alice"].id, added["name"], added["link"])
            return card if source_id == SOURCE_ID else None

        monkeypatch.setattr(source_files, "source_summary", source_summary)
        ben = await served.repo.upsert_user(
            None, SimpleNamespace(subject="user:ben@example.com", email="ben@example.com", name=None)
        )
        carol = SimpleNamespace(subject="user:carol@example.com", email="carol@example.com", name=None)
        await served.repo.upsert_user(None, carol)
        served.repo.members.add((alpha, ben.id))
        served.repo.roles[(alpha, ben.id)] = "collaborator"
        yield SimpleNamespace(
            http=served.client,
            runtime=served.runtime,
            space=space,
            fake=space.fake,
            connections=connections,
            integrations=integrations,
            imports=imports,
            alpha=alpha,
            ben=ben,
        )


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/linear{tail}"


async def test_the_check_answers_a_project_link_with_its_issues_and_documents_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, {"link": rig.space.project.url})

        assert response.status_code == 200
        body = response.json()
        assert (body["kind"], body["name"], body["issues"], body["capped"], body["documents"]) == (
            "project",
            "Checkout",
            5,
            False,
            2,
        )
        assert body["items"][0] == {"identifier": "GRA-1", "title": "Card flow", "state": "Backlog", "parent": None}
        assert body["items"][2]["parent"] == "GRA-1"


async def test_the_check_answers_an_issue_by_url_or_identifier_with_its_sub_issues(rig) -> None:
    for value in (rig.space.issues[0].url, "GRA-1", f"  {rig.space.issues[0].url}  "):
        response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": value})

        assert response.status_code == 200
        body = response.json()
        assert (body["kind"], body["name"], body["issues"], body["documents"]) == ("issue", "GRA-1 Card flow", 3, 0)
        assert [item["identifier"] for item in body["items"]] == ["GRA-3", "GRA-4", "GRA-5"]


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"link": "GRA-1"})

    assert error_of(response) == (404, "project_not_found") and rig.fake.calls == []


@pytest.mark.parametrize("value", ["not a link", "https://linear.app/graphico-app/team/GRA/active", "GRA", "12"])
async def test_a_value_that_is_no_link_or_identifier_is_refused_before_anything_is_stored(rig, value) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": value})

        assert error_of(response) == (422, "linear_link_invalid")
    assert rig.imports.added == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LinearNotFoundError(), 409, "linear_not_found"),
        (LinearForbiddenError(), 409, "linear_forbidden"),
        (LinearUnauthorizedError(), 409, "linear_reconnect_required"),
        (LinearRateLimitedError(2.0), 429, "linear_rate_limited"),
        (LinearUnavailableError(), 502, "linear_unreachable"),
    ],
)
async def test_a_refused_read_is_a_closed_code(rig, error, status, code) -> None:
    rig.fake.fail_next(error, method="resolve_link")

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": "GRA-1"})

    assert error_of(response) == (status, code) and "Card flow" not in response.text


async def test_an_issue_or_project_that_does_not_exist_is_not_found(rig) -> None:
    for value in ("GRA-99", "https://linear.app/graphico-app/project/nothing-aaaaaaaaaaaa"):
        response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": value})

        assert error_of(response) == (409, "linear_not_found")


async def test_a_token_refused_after_the_refresh_asks_the_member_to_connect_again_on_every_read(rig) -> None:
    rig.fake.fail_next(LinearUnauthorizedError(), method="issues_of_project")

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": rig.space.project.url})

    assert error_of(response) == (409, "linear_reconnect_required")


async def test_without_a_bound_member_or_with_an_expired_connection_or_with_linear_off_nothing_is_read(rig) -> None:
    body = {"link": "GRA-1"}
    rig.connections.status = "expired"
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (409, "linear_reconnect_required")
    rig.connections.status = "active"
    rig.integrations.release(rig.alpha, ACTING)
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "linear_not_connected")
    rig.integrations.put(rig.alpha, "linear", uuid.UUID(int=5))
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "linear_not_connected")
    rig.runtime.linear = None
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (503, "linear_unavailable")
    assert rig.fake.calls == [] and rig.imports.added == []


async def test_adding_a_project_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"link": rig.space.project.url})

    assert response.status_code == 201, response.text
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "linear_project",
        "Checkout",
        rig.space.project.url,
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"]) == (rig.alpha, rig.ben.id, "linear_project")
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, ACTING, "linear_project")
    assert isinstance(importer, LinearProjectImporter)
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert (
        len(writer.added) == 7 and rig.fake.writes == []
    )  # five issues and two documents; nothing is written to Linear


async def test_adding_an_issue_by_identifier_names_the_source_by_the_issue_and_imports_it_with_its_sub_issues(
    rig,
) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": "gra-1"})

    assert response.status_code == 201, response.text
    assert response.json()["source"]["name"] == "GRA-1 Card flow"
    (job, importer) = rig.imports.started[0]
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert list(writer.added) == ["issue:GRA-1", "issue:GRA-3", "issue:GRA-4", "issue:GRA-5"]


async def test_adding_the_same_link_again_is_a_new_source_that_reads_the_same_bytes(rig) -> None:
    for _ in range(2):
        assert (await call(rig, "POST", path(rig), ALICE, {"link": rig.space.project.url})).status_code == 201

    assert len(rig.imports.added) == len(rig.imports.started) == 2
    first, second = RecordingWriter(), RecordingWriter()
    await rig.imports.started[0][1](rig.imports.started[0][0], as_writer(first))
    await rig.imports.started[1][1](rig.imports.started[1][0], as_writer(second))
    assert first.added == second.added  # the writer's content hash drops the copies


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"link": "GRA-1"})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_no_title_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": "GRA-1", "x": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {"link": "x" * 3000})).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"link": rig.space.project.url})

    assert "Card flow" not in log_haystack(caplog) and "Checkout" not in log_haystack(caplog)


async def test_the_exclude_list_skips_those_issues_and_is_counted_not_named_in_the_log(rig, caplog) -> None:
    body = {"link": rig.space.project.url, "exclude": ["GRA-2", "GRA-2", "GRA-77"]}

    response = await call(rig, "POST", path(rig), ALICE, body)

    assert response.status_code == 201, response.text
    (job, importer) = rig.imports.started[0]
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert [key for key in writer.added if key.startswith("issue:")] == [
        "issue:GRA-1",
        "issue:GRA-3",
        "issue:GRA-4",
        "issue:GRA-5",
    ]
    assert "GRA-77" not in log_haystack(caplog)


@pytest.mark.parametrize(
    "exclude", [["gra-2"], ["GRA"], ["GRA-2 "], [""], [5], "GRA-2", [f"GRA-{n}" for n in range(201)]]
)
async def test_a_bad_exclude_is_a_validation_failure(rig, exclude) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": "GRA-1", "exclude": exclude})

    assert error_of(response) == (422, "validation_failed")
    assert not rig.imports.started


async def test_the_limit_of_exclude_is_the_issue_cap(rig) -> None:
    assert EXCLUDE_MAX == MAX_LINEAR_ISSUES
    exclude = [f"GRA-{n}" for n in range(1, MAX_LINEAR_ISSUES + 1)]

    assert (await call(rig, "POST", path(rig), ALICE, {"link": "GRA-1", "exclude": exclude})).status_code == 201
