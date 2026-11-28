"""The GitHub routes over the REST app (docs/github/api.md): the caller's connection and the project repositories, on a
scripted GitHub, an in-memory connection store and a fake ``db.github``. The clone workflow start is recorded, not run.

Standings as in test_api_atlassian_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from temporalio.service import RPCError, RPCStatusCode

from api.routers import repositories as repositories_router
from db import github as github_module
from db.github import ConnectionRows, RepositoryExistsError, RepositoryRow
from db.models import ProjectRepository
from github.client import SIZE_LIMIT_KB, Repository
from github.connections import GitHubConnections
from github_support import CLIENT_CANARY, CODE_CANARY, LOGIN, USER_ID, FakeGitHub, MemoryStore, github_settings, repo
from integrations_db_fakes import FakeIntegrationsDb
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from storage.keys import repository_snapshot
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
RETURN = "/projects/p/settings?tab=github"
SHA = "b" * 40
REPOSITORY_FUNCTIONS = (
    "list_repositories",
    "get_repository",
    "add_repository",
    "delete_repository",
    "request_clone",
    "fail_clone",
)


class FakeRepositoriesDb:
    """The project-repository functions of ``db.github`` over a dict."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, ProjectRepository] = {}
        self.names: dict[uuid.UUID, str] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in REPOSITORY_FUNCTIONS:
            monkeypatch.setattr(github_module, name, getattr(self, name))

    def _row(self, row: ProjectRepository) -> RepositoryRow:
        return RepositoryRow(row, self.names.get(row.added_by) if row.added_by else None)

    async def list_repositories(self, _session: Any, project_id: uuid.UUID) -> list[RepositoryRow]:
        rows = [r for r in self.rows.values() if r.project_id == project_id]
        return [self._row(r) for r in sorted(rows, key=lambda r: r.full_name)]

    async def get_repository(
        self, _session: Any, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> RepositoryRow | None:
        row = self.rows.get(repository_id)
        return self._row(row) if row is not None and row.project_id == project_id else None

    async def add_repository(
        self, _session: Any, project_id: uuid.UUID, repository: Repository, *, added_by: uuid.UUID
    ) -> uuid.UUID:
        if any(r.project_id == project_id and r.github_repo_id == repository.id for r in self.rows.values()):
            raise RepositoryExistsError
        repository_id = uuid.uuid4()
        self.rows[repository_id] = ProjectRepository(
            id=repository_id,
            project_id=project_id,
            github_repo_id=repository.id,
            full_name=repository.full_name,
            html_url=repository.html_url,
            private=repository.private,
            default_branch=repository.default_branch,
            added_by=added_by,
            clone_state="pending",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return repository_id

    async def delete_repository(self, _session: Any, project_id: uuid.UUID, repository_id: uuid.UUID) -> bool:
        if (await self.get_repository(None, project_id, repository_id)) is None:
            return False
        del self.rows[repository_id]
        return True

    async def request_clone(self, _session: Any, project_id: uuid.UUID, repository_id: uuid.UUID) -> bool:
        found = await self.get_repository(None, project_id, repository_id)
        if found is not None and found.repository.clone_state != "cloning":
            found.repository.clone_state, found.repository.failure_code = "pending", None
        return found is not None

    async def fail_clone(
        self, _session: Any, project_id: uuid.UUID, repository_id: uuid.UUID, code: str, *, now: Any
    ) -> bool:
        row = self.rows[repository_id]
        assert row.project_id == project_id
        row.clone_state, row.failure_code, row.checked_at = "failed", code, now
        return True


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        github, store, db = FakeGitHub(), MemoryStore(), FakeRepositoriesDb()
        db.install(monkeypatch)
        integrations = FakeIntegrationsDb()
        integrations.install(monkeypatch)
        started: list[tuple[uuid.UUID, uuid.UUID]] = []

        async def start(_runtime: Any, project_id: uuid.UUID, repository_id: uuid.UUID) -> str:
            if rig_state.start_fails:
                raise RPCError("temporal down", RPCStatusCode.UNAVAILABLE, b"")
            started.append((project_id, repository_id))
            return f"github-snapshot-{repository_id}"

        rig_state = SimpleNamespace(start_fails=False)
        monkeypatch.setattr(repositories_router, "start_github_snapshot", start)
        runtime, repo_ = served.runtime, served.repo
        connections = GitHubConnections(github_settings(), store=store.transaction, client=github.client())
        runtime.github = connections
        ben = await repo_.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        alpha = uuid.UUID(ALPHA)
        repo_.members.add((alpha, ben.id))
        repo_.roles[(alpha, ben.id)] = "collaborator"
        await repo_.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        alice = served.users["alice"]
        db.names[alice.id] = "Alice"
        github.installations[USER_ID] = {
            11: [repo(7, "octo/app"), repo(8, "octo/huge", size=SIZE_LIMIT_KB + 1)],
        }
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            github=github,
            store=store,
            db=db,
            integrations=integrations,
            started=started,
            state=rig_state,
            alpha=alpha,
            alice=alice,
            ben=ben,
        )
        runtime.github = None
        await connections.aclose()


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connect(rig: Any, who: str = ALICE) -> httpx.Response:
    url = (await call(rig, "POST", "integrations/github/authorize", who, {"return_to": RETURN})).json()["url"]
    return await call(rig, "POST", "integrations/github/callback", who, {"code": CODE_CANARY, "state": state_of(url)})


async def bind(rig: Any, who: str = ALICE) -> httpx.Response:
    """Connect the project to GitHub as ``who`` (their own active connection)."""

    return await call(rig, "PUT", f"projects/{ALPHA}/integrations/github", who)


# The connection routes -----------------------------------------------------------------------------------------------


async def test_the_connect_flow_keeps_the_connection_and_answers_the_return_path(rig, caplog) -> None:
    before = (await call(rig, "GET", "integrations/github")).json()
    with caplog.at_level(logging.DEBUG):
        answer = await connect(rig)

    assert before == {
        "enabled": True,
        "connected": False,
        "status": None,
        "login": None,
        "connected_at": None,
        "last_used_at": None,
        "manage_url": "https://github.com/settings/installations",
    }
    assert answer.status_code == 200
    body = answer.json()
    assert body["return_to"] == RETURN
    assert (body["connection"]["connected"], body["connection"]["login"]) == (True, LOGIN)
    now = (await call(rig, "GET", "integrations/github")).json()
    assert (now["status"], now["login"]) == ("active", LOGIN)
    assert (await call(rig, "GET", "integrations/github", BEN)).json()["connected"] is False
    for canary in (CODE_CANARY, CLIENT_CANARY, "access-1", "refresh-1"):
        assert canary not in caplog.text


async def test_a_callback_with_a_bad_state_or_code_is_refused_with_a_closed_code(rig, caplog) -> None:
    url = (await call(rig, "POST", "integrations/github/authorize", BEN, {"return_to": RETURN})).json()["url"]
    with caplog.at_level(logging.INFO):
        other = await call(rig, "POST", "integrations/github/callback", body={"code": "c", "state": state_of(url)})
    bad_code = await call(
        rig,
        "POST",
        "integrations/github/callback",
        body={
            "code": "unknown",
            "state": state_of(
                (await call(rig, "POST", "integrations/github/authorize", body={"return_to": RETURN})).json()["url"]
            ),
        },
    )

    assert error_of(other) == (400, "github_state_invalid")
    assert error_of(bad_code) == (400, "github_code_invalid")
    assert "api.github.refused" in caplog.text and "unknown" not in caplog.text


@pytest.mark.parametrize("return_to", ["https://evil.com", "//evil.com", "/a/../b", "/a%2fb", "/a b", "x"])
async def test_a_return_path_off_this_site_is_refused(rig, return_to: str) -> None:
    for path in ("integrations/github/authorize", "integrations/github/install"):
        assert error_of(await call(rig, "POST", path, body={"return_to": return_to})) == (422, "validation_failed")


async def test_the_install_link_and_the_setup_check(rig) -> None:
    url = (await call(rig, "POST", "integrations/github/install", body={"return_to": RETURN})).json()["url"]
    state = state_of(url)

    assert url.startswith("https://github.com/apps/specflow-local/installations/new?state=")
    assert (await call(rig, "POST", "integrations/github/setup", body={"state": state})).json() == {"return_to": RETURN}
    refused = await call(rig, "POST", "integrations/github/setup", BEN, {"state": state})
    assert error_of(refused) == (400, "github_state_invalid")


async def test_the_caller_lists_the_repositories_they_can_read(rig) -> None:
    assert error_of(await call(rig, "GET", "integrations/github/repositories")) == (409, "github_not_connected")
    await connect(rig)

    listed = (await call(rig, "GET", "integrations/github/repositories")).json()

    assert [r["full_name"] for r in listed["repositories"]] == ["octo/app", "octo/huge"]
    assert [r["too_large"] for r in listed["repositories"]] == [False, True] and listed["truncated"] is False


async def test_github_down_is_502(rig) -> None:
    await connect(rig)
    rig.github.status = 503

    assert error_of(await call(rig, "GET", "integrations/github/repositories")) == (502, "github_unreachable")


async def test_with_the_integration_off_the_routes_answer_503_and_the_list_says_so(rig) -> None:
    rig.runtime.github = None

    assert (await call(rig, "GET", "integrations/github")).json()["enabled"] is False
    for method, path, body in (
        ("POST", "integrations/github/authorize", {"return_to": RETURN}),
        ("POST", "integrations/github/install", {"return_to": RETURN}),
        ("POST", "integrations/github/callback", {"code": "c", "state": "s"}),
        ("POST", "integrations/github/setup", {"state": "s"}),
        ("GET", "integrations/github/repositories", None),
        ("POST", f"projects/{ALPHA}/repositories", {"github_repo_id": 7}),
    ):
        assert error_of(await call(rig, method, path, body=body)) == (503, "github_unavailable"), path
    listed = (await call(rig, "GET", f"projects/{ALPHA}/repositories")).json()
    assert listed == {"enabled": False, "can_manage": True, "repositories": []}


# The project repositories --------------------------------------------------------------------------------------------


async def test_a_member_adds_a_repository_the_projects_connection_can_read_and_its_clone_check_starts(rig) -> None:
    await connect(rig)
    await bind(rig)

    added = await call(rig, "POST", f"projects/{ALPHA}/repositories", body={"github_repo_id": 7})

    assert added.status_code == 201
    body = added.json()
    assert (body["full_name"], body["clone_state"], body["added_by_name"], body["added_by_me"]) == (
        "octo/app",
        "pending",
        "Alice",
        True,
    )
    assert rig.started == [(rig.alpha, uuid.UUID(body["id"]))]
    as_ben = (await call(rig, "GET", f"projects/{ALPHA}/repositories", BEN)).json()
    assert as_ben["enabled"] is True and as_ben["can_manage"] is True  # D6: any member manages
    assert [(r["full_name"], r["added_by_me"]) for r in as_ben["repositories"]] == [("octo/app", False)]
    again = await call(rig, "POST", f"projects/{ALPHA}/repositories", body={"github_repo_id": 7})
    assert error_of(again) == (409, "repository_exists")


@pytest.mark.parametrize(
    ("repo_id", "expected"), [(9, (404, "github_repo_not_accessible")), (8, (422, "github_repo_too_large"))]
)
async def test_a_repository_the_projects_connection_cannot_read_or_too_large_is_refused(rig, repo_id, expected) -> None:
    await connect(rig)
    await bind(rig)

    refused = await call(rig, "POST", f"projects/{ALPHA}/repositories", body={"github_repo_id": repo_id})

    assert error_of(refused) == expected and rig.db.rows == {} and rig.started == []


async def test_a_collaborator_adds_a_repository_as_the_bound_member_and_a_non_member_sees_nothing(rig) -> None:
    await connect(rig)  # alice's GitHub connection; the project is bound to her
    await bind(rig)
    body = {"github_repo_id": 7}

    added = await call(rig, "POST", f"projects/{ALPHA}/repositories", BEN, body)  # ben has no connection of his own

    assert added.status_code == 201
    assert (added.json()["added_by_name"], added.json()["added_by_me"]) == ("Alice", False)  # D1: alice's account
    assert rig.db.rows[uuid.UUID(added.json()["id"])].added_by == rig.alice.id
    assert error_of(await call(rig, "GET", f"projects/{ALPHA}/repositories", CAROL)) == (404, "project_not_found")
    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/repositories", CAROL, body)) == (
        404,
        "project_not_found",
    )


async def test_adding_needs_the_project_to_be_connected_to_github_by_a_member(rig) -> None:
    await connect(rig)  # a connection of the caller is not the project's: nobody bound it
    body = {"github_repo_id": 7}

    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/repositories", body=body)) == (
        409,
        "github_not_connected",
    )
    rig.integrations.put(rig.alpha, "github", None)  # its member left
    assert error_of(await call(rig, "POST", f"projects/{ALPHA}/repositories", body=body)) == (
        409,
        "github_not_connected",
    )
    assert rig.db.rows == {} and rig.started == []


async def test_a_start_that_fails_marks_the_repository_failed(rig) -> None:
    await connect(rig)
    await bind(rig)
    rig.state.start_fails = True

    body = (await call(rig, "POST", f"projects/{ALPHA}/repositories", body={"github_repo_id": 7})).json()

    assert (body["clone_state"], body["failure_code"]) == ("failed", "github_clone_failed")


async def test_removing_a_repository_deletes_its_snapshots(rig) -> None:
    await connect(rig)
    await bind(rig)
    rid = (await call(rig, "POST", f"projects/{ALPHA}/repositories", body={"github_repo_id": 7})).json()["id"]
    key = repository_snapshot(ALPHA, rid, SHA)
    writer = await rig.runtime.store.open_writer(key, content_type="application/gzip")
    await writer.write(b"tar")
    await writer.close()

    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/repositories/{rid}", CAROL)) == (
        404,
        "project_not_found",
    )
    assert (await call(rig, "DELETE", f"projects/{ALPHA}/repositories/{rid}", BEN)).status_code == 204  # D6

    assert rig.db.rows == {} and not await rig.runtime.store.exists(key)
    for path in (f"repositories/{rid}", "repositories/not-a-uuid"):
        assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/{path}")) == (404, "repository_not_found")


def test_the_fakes_keep_the_signatures_of_db_github() -> None:
    def shape(function: Any) -> list[tuple[str, Any]]:
        parameters = list(inspect.signature(function).parameters.values())
        return [(p.name.lstrip("_"), p.kind) for p in parameters if p.name != "self"]

    for name in REPOSITORY_FUNCTIONS:
        assert shape(getattr(FakeRepositoriesDb, name)) == shape(getattr(github_module, name)), name
    for name in ("get", "lock", "save", "rotate", "expire", "delete"):
        assert shape(getattr(MemoryStore, name)) == shape(getattr(ConnectionRows, name)), name
