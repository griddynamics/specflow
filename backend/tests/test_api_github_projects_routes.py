"""The GitHub Projects routes over the REST app (docs/github/projects.md "Routes"): the target, the projects of the
repository's organization and the access check, as the member the project's GitHub is connected by. GitHub is the fake
gateway of ``github_projects_support``; the rows are in memory.

Standings as in test_api_github_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.github_projects.runner import GitHubProjectsRunner
from api.github_projects.sync import PushCounts, PushDeps, PushResult
from db import github_projects as projects_db
from db import integrations as integrations_module
from db.github_projects import ProjectChoice, ProjectTargetView, TargetSaved
from github.client import Repository
from github.codes import GitHubCode, GitHubError
from github.connections import GitHubConnections
from github.projects import RepositoryInfo
from github_projects_support import PROJECT_NODE, REPOSITORY, FakeGateway, target_view
from github_support import LOGIN, USER_ID, FakeGitHub, MemoryStore, github_settings, repo
from integrations_db_fakes import FakeIntegrationsDb
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of
from test_api_github_routes import ALICE, BEN, CAROL, FakeRepositoriesDb, bind, connect

PID = uuid.UUID(ALPHA)
PATH = f"projects/{ALPHA}/github-projects"
_ = integrations_module


class FakeProjectsDb:
    """The target functions of ``db.github_projects`` over one dict."""

    def __init__(self, repositories: FakeRepositoriesDb) -> None:
        self.repositories = repositories
        self.targets: dict[uuid.UUID, ProjectTargetView] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("get_target", "save_target", "record_start", "record_end", "running_targets"):
            monkeypatch.setattr(projects_db, name, getattr(self, name))

    async def get_target(self, _session: Any, project_id: uuid.UUID) -> ProjectTargetView | None:
        return self.targets.get(project_id)

    async def save_target(
        self,
        _session: Any,
        project_id: uuid.UUID,
        *,
        repository_id: uuid.UUID,
        project: ProjectChoice,
        enabled: bool,
        now: datetime,
    ) -> TargetSaved:
        before = self.targets.get(project_id)
        name = self.repositories.rows[repository_id].full_name
        base = target_view(project_id)
        self.targets[project_id] = replace(
            base,
            repository_id=repository_id,
            repository_name=name,
            project_node_id=project.node_id,
            project_title=project.title,
            project_url=project.url,
            enabled=enabled,
            updated_at=now,
        )
        changed = before is None or (before.repository_id, before.project_node_id) != (repository_id, project.node_id)
        return TargetSaved(
            destination_changed=before is not None and changed,
            turned_on=enabled and (before is None or not before.enabled),
            keys_changed=changed,
        )

    async def record_start(self, _session: Any, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
        self.targets[project_id] = replace(
            self.targets[project_id], last_status="running", last_trigger=trigger, last_started_at=queued_at
        )

    async def record_end(self, _session: Any, project_id: uuid.UUID, **values: Any) -> None:
        self.targets[project_id] = replace(
            self.targets[project_id],
            last_status=values["status"],
            last_error_code=values["error_code"],
            last_counts=values["counts"],
        )

    async def running_targets(self, _session: Any) -> list[uuid.UUID]:
        return []


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        github, store, repositories = FakeGitHub(), MemoryStore(), FakeRepositoriesDb()
        repositories.install(monkeypatch)
        FakeIntegrationsDb().install(monkeypatch)
        db = FakeProjectsDb(repositories)
        db.install(monkeypatch)
        gateway, passes = FakeGateway(), []

        async def push(project_id: str, _deps: PushDeps) -> PushResult:
            passes.append(project_id)
            return PushResult("succeeded", None, PushCounts(created=1))

        runtime, repo_ = served.runtime, served.repo
        connections = GitHubConnections(github_settings(), store=store.transaction, client=github.client())
        runtime.github = connections
        runner = GitHubProjectsRunner(connections, transaction=runtime.transaction, graph_store=object(), push=push)

        async def gateway_of(_user_id: uuid.UUID) -> FakeGateway:
            return gateway

        monkeypatch.setattr(runner, "gateway_of", gateway_of)
        runtime.github_projects = runner
        ben = await repo_.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        repo_.members.add((PID, ben.id))
        repo_.roles[(PID, ben.id)] = "collaborator"
        await repo_.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        github.installations[USER_ID] = {11: [repo(7, "acme/shop")]}
        alice = served.users["alice"]
        repository = Repository(
            id=7,
            full_name=REPOSITORY,
            html_url="https://github.com/acme/shop",
            private=True,
            default_branch="main",
            size_kb=10,
        )
        repository_id = await repositories.add_repository(None, PID, repository, added_by=alice.id)
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            runner=runner,
            gateway=gateway,
            db=db,
            passes=passes,
            repository_id=str(repository_id),
        )
        runtime.github = None
        runtime.github_projects = None
        await runner.aclose()
        await connections.aclose()


async def ready(rig: Any) -> None:
    """Alice connects GitHub and binds it to the project."""

    assert (await connect(rig)).status_code == 200
    assert (await bind(rig)).status_code == 200


def body(rig: Any, **values: Any) -> dict[str, Any]:
    return {"repository_id": rig.repository_id, "project_node_id": PROJECT_NODE, "enabled": True} | values


async def settle(rig: Any) -> None:
    tasks = [p.task for p in rig.runner._projects.values() if p.task is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


# GET and PUT ------------------------------------------------------------------------------------------------------


async def test_a_project_with_no_destination_reads_as_available_and_empty(rig) -> None:
    answer = (await call(rig, "GET", PATH)).json()

    assert answer["available"] is True and answer["enabled"] is False
    assert (answer["repository"], answer["project_title"], answer["sync"]) == (None, None, None)


async def test_saving_an_existing_project_stores_it_and_starts_a_pass_as_the_bound_member(rig) -> None:
    await ready(rig)

    answer = await call(rig, "PUT", PATH, ALICE, body(rig))
    await settle(rig)

    assert answer.status_code == 200
    saved = answer.json()
    assert (saved["repository"], saved["project_node_id"], saved["project_title"]) == (REPOSITORY, PROJECT_NODE, "Plan")
    assert (saved["enabled"], saved["connected_by_name"], saved["connected_by_me"]) == (True, LOGIN, True)
    assert saved["sync"]["status"] == "running" and saved["sync"]["trigger"] == "settings"
    assert rig.passes == [ALPHA]
    later = (await call(rig, "GET", PATH)).json()
    assert (later["sync"]["status"], later["sync"]["counts"]["created"]) == ("succeeded", 1)
    assert [name for name in rig.gateway.names() if name != "organization_projects"] == ["repository", "project"]


async def test_a_new_project_is_created_in_the_organization_with_the_typed_title(rig) -> None:
    await ready(rig)

    answer = await call(rig, "PUT", PATH, ALICE, body(rig, project_node_id=None, new_project_title="  Shop plan "))

    assert answer.status_code == 200
    assert (answer.json()["project_node_id"], answer.json()["project_title"]) == ("PVT_new", "Shop plan")
    assert ("create_project", ("acme", "Shop plan")) in rig.gateway.calls


async def test_saving_without_the_projects_github_binding_is_refused(rig) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, body(rig))) == (409, "github_not_connected")
    assert rig.passes == []


async def test_a_destination_names_one_project_or_one_new_title(rig) -> None:
    await ready(rig)

    both = await call(rig, "PUT", PATH, ALICE, body(rig, new_project_title="X"))
    neither = await call(rig, "PUT", PATH, ALICE, body(rig, project_node_id=None))
    extra = await call(rig, "PUT", PATH, ALICE, body(rig, surprise=1))

    assert [r.status_code for r in (both, neither, extra)] == [422, 422, 422]


async def test_a_repository_that_is_not_the_projects_is_not_found(rig) -> None:
    await ready(rig)

    answer = await call(rig, "PUT", PATH, ALICE, body(rig, repository_id=str(uuid.uuid4())))

    assert error_of(answer) == (404, "repository_not_found")


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (GitHubError(GitHubCode.PERMISSIONS_MISSING), (409, "github_permissions_missing")),
        (GitHubError(GitHubCode.PROJECT_NOT_FOUND), (404, "github_project_not_found")),
        (GitHubError(GitHubCode.UNREACHABLE), (502, "github_unreachable")),
        (GitHubError(GitHubCode.RECONNECT_REQUIRED), (409, "github_reconnect_required")),
    ],
)
async def test_a_github_refusal_on_save_is_one_closed_error_and_saves_nothing(rig, failure, expected) -> None:
    await ready(rig)
    rig.gateway.fail["project"] = failure

    answer = await call(rig, "PUT", PATH, ALICE, body(rig))

    assert error_of(answer) == expected
    assert rig.db.targets == {} and rig.passes == []


async def test_github_off_on_this_server_is_a_503_and_an_unavailable_read(rig) -> None:
    rig.runtime.github_projects = None

    assert (await call(rig, "GET", PATH)).json()["available"] is False
    assert error_of(await call(rig, "PUT", PATH, ALICE, body(rig))) == (503, "github_unavailable")


async def test_a_collaborator_reads_and_saves_and_a_stranger_sees_no_project(rig) -> None:
    await ready(rig)

    assert (await call(rig, "PUT", PATH, BEN, body(rig))).status_code == 200
    assert (await call(rig, "GET", PATH, BEN)).json()["connected_by_me"] is False
    assert (await call(rig, "GET", PATH, CAROL)).status_code == 404


# The projects and the access check --------------------------------------------------------------------------------


async def test_the_projects_of_the_repositorys_organization_are_listed(rig) -> None:
    await ready(rig)

    answer = (await call(rig, "GET", f"{PATH}/projects?repository_id={rig.repository_id}")).json()

    assert answer["organization"] == "acme"
    assert answer["projects"] == [
        {"node_id": PROJECT_NODE, "number": 1, "title": "Plan", "url": "https://github.com/orgs/acme/projects/1"}
    ]


async def test_the_access_check_says_ok_for_an_existing_project_and_for_a_new_one(rig) -> None:
    await ready(rig)

    existing = await call(
        rig,
        "POST",
        f"{PATH}/access-check",
        ALICE,
        {"repository_id": rig.repository_id, "project_node_id": PROJECT_NODE},
    )
    new = await call(
        rig, "POST", f"{PATH}/access-check", ALICE, {"repository_id": rig.repository_id, "new_project_title": "T"}
    )

    assert existing.json() == {"result": "ok", "organization": "acme", "project_title": "Plan"}
    assert new.json() == {"result": "ok", "organization": "acme", "project_title": None}


@pytest.mark.parametrize(
    ("method", "failure", "reason", "field"),
    [
        ("project", GitHubError(GitHubCode.PROJECT_NOT_FOUND), "github_project_not_found", "project"),
        ("repository", GitHubError(GitHubCode.REPO_NOT_ACCESSIBLE), "github_repo_not_accessible", "repository"),
        ("organization_projects", GitHubError(GitHubCode.PERMISSIONS_MISSING), "github_permissions_missing", None),
        ("repository", GitHubError(GitHubCode.RECONNECT_REQUIRED), "github_reconnect_required", None),
        ("repository", GitHubError(GitHubCode.REFUSED), "github_auth_failed", None),
    ],
)
async def test_the_access_check_names_what_failed_as_a_closed_reason(rig, method, failure, reason, field) -> None:
    await ready(rig)
    rig.gateway.fail[method] = failure
    form = {"repository_id": rig.repository_id}
    form |= {"project_node_id": PROJECT_NODE} if method == "project" else {"new_project_title": "T"}

    answer = await call(rig, "POST", f"{PATH}/access-check", ALICE, form)

    assert answer.status_code == 200
    assert answer.json() == {"result": "failed", "reason": reason, "field": field}


async def test_a_users_repository_or_a_read_only_one_cannot_be_a_destination(rig, monkeypatch) -> None:
    await ready(rig)
    form = {"repository_id": rig.repository_id, "new_project_title": "T"}
    infos = iter(
        [RepositoryInfo(REPOSITORY, None, can_write=True), RepositoryInfo(REPOSITORY, "acme", can_write=False)]
    )

    async def repository(full_name: str) -> RepositoryInfo:
        return next(infos)

    monkeypatch.setattr(rig.gateway, "repository", repository)

    user_owned = await call(rig, "POST", f"{PATH}/access-check", ALICE, form)
    read_only = await call(rig, "POST", f"{PATH}/access-check", ALICE, form)

    assert user_owned.json() == {"result": "failed", "reason": "github_not_an_organization", "field": "repository"}
    assert read_only.json() == {"result": "failed", "reason": "github_repo_read_only", "field": "repository"}


async def test_the_access_check_without_a_binding_is_a_409(rig) -> None:
    form = {"repository_id": rig.repository_id, "new_project_title": "T"}

    assert error_of(await call(rig, "POST", f"{PATH}/access-check", ALICE, form)) == (409, "github_not_connected")
