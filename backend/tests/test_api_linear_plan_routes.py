"""The Linear plan routes over the REST app: the target, the teams and projects of the acting member, the access check
and the agent pair, as the member the project's Linear is bound to (rule R1). Linear is ``FakeLinear``; the rows are in
memory.

Standings as in test_api_github_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.linear_plan.runner import LinearPlanRunner
from api.linear_plan.sync import PushCounts, PushDeps, PushResult
from integrations_db_fakes import FakeIntegrationsDb
from linear.errors import LinearForbiddenError, LinearUnauthorizedError, LinearUnavailableError
from linear.fake import FakeLinear
from linear_plan_support import FakePlanDb
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of
from test_api_github_routes import ALICE, BEN, CAROL

PID = uuid.UUID(ALPHA)
PATH = f"projects/{ALPHA}/linear-plan"
WORKSPACE = "Graphico"


@dataclass(frozen=True)
class Connection:
    status: str = "active"
    linear_user_id: str = "u1"
    organization_id: str = "w1"
    organization_name: str = WORKSPACE


class FakeConnections:
    """What ``runtime.linear`` is (stream S1's connections): who is connected, and a gateway per member."""

    def __init__(self, linear: FakeLinear) -> None:
        self.linear = linear
        self.members: set[uuid.UUID] = set()
        self.gateways: list[uuid.UUID] = []

    async def get(self, user_id: uuid.UUID) -> Connection | None:
        return Connection() if user_id in self.members else None

    @asynccontextmanager
    async def gateway(self, user_id: uuid.UUID) -> AsyncIterator[FakeLinear]:
        self.gateways.append(user_id)
        yield self.linear


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        linear = FakeLinear(user_name="Mate")
        team = linear.add_team("ENG", "Engineering")
        other = linear.add_team("OPS", "Operations")
        project = linear.add_project(team.id, "Roadmap")
        linear.add_project(other.id, "Ops board")
        connections = FakeConnections(linear)
        integrations = FakeIntegrationsDb()
        integrations.install(monkeypatch)
        db = FakePlanDb()
        db.install(monkeypatch)
        passes: list[str] = []
        stub = SimpleNamespace(gate=None)  # an event a test sets here holds the passes until it is set

        async def push(project_id: str, _deps: PushDeps) -> PushResult:
            passes.append(project_id)
            if stub.gate is not None:
                await stub.gate.wait()
            return PushResult("succeeded", None, PushCounts(created=1))

        runtime = served.runtime
        runtime.linear = connections
        runner = LinearPlanRunner(connections.gateway, transaction=runtime.transaction, graph_store=object(), push=push)
        runtime.linear_plan = runner
        ben = await served.repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        served.repo.members.add((PID, ben.id))
        served.repo.roles[(PID, ben.id)] = "collaborator"
        await served.repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        alice = served.users["alice"]
        connections.members.add(alice.id)
        yield SimpleNamespace(
            http=served.client,
            app=served.app,
            runtime=runtime,
            runner=runner,
            linear=linear,
            team=team,
            other=other,
            project=project,
            connections=connections,
            integrations=integrations,
            alice=alice,
            db=db,
            passes=passes,
            stub=stub,
        )
        runtime.linear = None
        runtime.linear_plan = None
        await runner.aclose()


def bind(rig: Any, member: uuid.UUID | None = None) -> None:
    """The project's Linear is connected by ``member`` (Alice by default)."""

    rig.integrations.put(PID, "linear", member or rig.alice.id, workspace_id="w1", workspace_name=WORKSPACE)


def body(rig: Any, **values: Any) -> dict[str, Any]:
    return {"team_id": rig.team.id, "project_id": rig.project.id, "enabled": True} | values


async def settle(rig: Any) -> None:
    running = [run.task for run in rig.runner._projects.values()]
    await asyncio.wait_for(asyncio.gather(*filter(None, running)), timeout=2)


# GET and PUT ------------------------------------------------------------------------------------------------------


async def test_a_project_with_no_destination_reads_as_available_and_empty(rig) -> None:
    shown = (await call(rig, "GET", PATH)).json()

    assert (shown["available"], shown["enabled"], shown["workspace"]) == (True, False, None)
    assert [shown[key] for key in ("team_key", "project_name", "sync")] == [None] * 3


async def test_saving_an_existing_project_stores_it_and_starts_a_pass_as_the_bound_member(rig) -> None:
    bind(rig)

    answer = await call(rig, "PUT", PATH, ALICE, body(rig))
    await settle(rig)

    assert answer.status_code == 200
    saved = answer.json()
    assert (saved["team_key"], saved["project_id"], saved["project_name"]) == ("ENG", rig.project.id, "Roadmap")
    assert (saved["enabled"], saved["created_by_graphico"], saved["connected_by_me"]) == (True, False, True)
    assert saved["workspace"] == WORKSPACE
    assert saved["sync"]["status"] == "running" and saved["sync"]["trigger"] == "settings"
    assert rig.passes == [ALPHA] and rig.connections.gateways == [rig.alice.id]
    later = (await call(rig, "GET", PATH)).json()
    assert (later["sync"]["status"], later["sync"]["counts"]["created"]) == ("succeeded", 1)
    assert rig.linear.count("create_project") == 0


async def test_without_a_project_graphico_creates_one_named_after_the_project_and_adopts_it_on_a_retry(rig) -> None:
    bind(rig)

    first = await call(rig, "PUT", PATH, ALICE, body(rig, project_id=None, enabled=False))
    again = await call(rig, "PUT", PATH, ALICE, body(rig, project_id=None, enabled=False))

    assert first.status_code == again.status_code == 200
    made = first.json()
    assert (made["created_by_graphico"], made["project_name"]) == (True, "Alpha")
    assert made["project_url"].startswith("https://linear.app/graphico-app/project/")
    assert again.json()["project_id"] == made["project_id"] and rig.linear.count("create_project") == 2
    assert [p.name for p in await rig.linear.projects(rig.team.id)].count("Alpha") == 1
    assert rig.passes == []  # disabled: no pass


async def test_saving_without_the_projects_linear_binding_is_refused(rig) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, body(rig))) == (409, "linear_not_connected")
    assert rig.passes == [] and rig.db.targets == {}


async def test_a_destination_names_a_team_and_at_most_one_project(rig) -> None:
    bind(rig)

    extra = await call(rig, "PUT", PATH, ALICE, body(rig, surprise=1))
    blank = await call(rig, "PUT", PATH, ALICE, body(rig, team_id=""))
    loose = await call(rig, "PUT", PATH, ALICE, body(rig, enabled="yes"))
    blank_project = await call(rig, "PUT", PATH, ALICE, body(rig, project_id="  ", enabled=False))

    assert [r.status_code for r in (extra, blank, loose)] == [422, 422, 422]
    assert blank_project.status_code == 200 and blank_project.json()["created_by_graphico"] is True


async def test_an_unknown_team_or_a_project_of_another_team_is_not_found(rig) -> None:
    bind(rig)
    other_project = (await rig.linear.projects(rig.other.id))[0]

    team = await call(rig, "PUT", PATH, ALICE, body(rig, team_id=str(uuid.uuid4())))
    elsewhere = await call(rig, "PUT", PATH, ALICE, body(rig, project_id=other_project.id))
    missing = await call(rig, "PUT", PATH, ALICE, body(rig, project_id=str(uuid.uuid4())))

    assert error_of(team) == (404, "linear_team_not_found")
    assert error_of(elsewhere) == (404, "linear_project_not_found")
    assert error_of(missing) == (404, "linear_project_not_found")
    assert rig.db.targets == {} and rig.passes == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (LinearUnauthorizedError(), (409, "linear_reconnect_required")),
        (LinearForbiddenError(), (409, "linear_forbidden")),
        (LinearUnavailableError(), (502, "linear_unreachable")),
    ],
)
async def test_a_linear_refusal_on_save_is_one_closed_error_and_saves_nothing(rig, failure, expected) -> None:
    bind(rig)
    rig.linear.fail_next(failure, method="teams")

    answer = await call(rig, "PUT", PATH, ALICE, body(rig))

    assert error_of(answer) == expected
    assert rig.db.targets == {} and rig.passes == []


async def test_linear_off_on_this_server_is_a_503_and_an_unavailable_read(rig) -> None:
    rig.runtime.linear_plan = None

    assert (await call(rig, "GET", PATH)).json()["available"] is False
    assert error_of(await call(rig, "PUT", PATH, ALICE, body(rig))) == (503, "linear_unavailable")
    assert error_of(await call(rig, "GET", f"{PATH}/teams")) == (503, "linear_unavailable")


async def test_a_collaborator_reads_and_saves_and_a_stranger_sees_no_project(rig) -> None:
    bind(rig)

    assert (await call(rig, "PUT", PATH, BEN, body(rig))).status_code == 200
    assert (await call(rig, "GET", PATH, BEN)).json()["connected_by_me"] is False
    assert (await call(rig, "GET", PATH, CAROL)).status_code == 404


async def test_the_member_who_acts_is_the_one_bound_now_not_the_one_who_saved(rig) -> None:
    bind(rig)
    await call(rig, "PUT", PATH, ALICE, body(rig, enabled=False))
    ben = next(user for (pid, user) in rig.runtime.session_factory.members if pid == PID and user != rig.alice.id)
    rig.connections.members.add(ben)
    bind(rig, ben)

    shown = (await call(rig, "GET", PATH, ALICE)).json()
    await call(rig, "GET", f"{PATH}/teams", ALICE)

    assert shown["connected_by_me"] is False and rig.connections.gateways == [rig.alice.id, ben]


# The teams, the projects and the access check ------------------------------------------------------------------------


async def test_the_teams_of_the_acting_members_workspace_are_listed(rig) -> None:
    bind(rig)

    answer = (await call(rig, "GET", f"{PATH}/teams")).json()

    assert answer["workspace"] == WORKSPACE
    assert answer["teams"] == [
        {"id": rig.team.id, "key": "ENG", "name": "Engineering"},
        {"id": rig.other.id, "key": "OPS", "name": "Operations"},
    ]


async def test_the_projects_of_a_team_are_listed(rig) -> None:
    bind(rig)

    answer = (await call(rig, "GET", f"{PATH}/projects?team_id={rig.team.id}")).json()

    assert answer["projects"] == [{"id": rig.project.id, "name": "Roadmap", "url": rig.project.url}]
    assert error_of(await call(rig, "GET", f"{PATH}/projects?team_id={uuid.uuid4()}")) == (409, "linear_not_found")


async def test_listing_without_the_projects_binding_is_linear_not_connected(rig) -> None:
    assert error_of(await call(rig, "GET", f"{PATH}/teams")) == (409, "linear_not_connected")
    assert error_of(await call(rig, "POST", f"{PATH}/access-check", ALICE, {"team_id": rig.team.id})) == (
        409,
        "linear_not_connected",
    )


async def test_the_access_check_says_ok_for_an_existing_project_and_for_a_new_one(rig) -> None:
    bind(rig)

    existing = await call(
        rig, "POST", f"{PATH}/access-check", ALICE, {"team_id": rig.team.id, "project_id": rig.project.id}
    )
    new = await call(rig, "POST", f"{PATH}/access-check", ALICE, {"team_id": rig.team.id})

    assert existing.json() == {"result": "ok", "team_key": "ENG", "project_name": "Roadmap"}
    assert new.json() == {"result": "ok", "team_key": "ENG", "project_name": None}
    assert rig.linear.writes == []  # a check never writes


@pytest.mark.parametrize(
    ("form", "failure", "reason", "field"),
    [
        ({"team_id": "nope"}, None, "linear_team_not_found", "team"),
        ({"project_id": "nope"}, None, "linear_project_not_found", "project"),
        ({}, LinearUnauthorizedError(), "linear_reconnect_required", None),
        ({}, LinearForbiddenError(), "linear_forbidden", None),
        ({}, LinearUnavailableError(), "linear_unreachable", None),
    ],
)
async def test_the_access_check_names_what_failed_as_a_closed_reason(rig, form, failure, reason, field) -> None:
    bind(rig)
    if failure is not None:
        rig.linear.fail_next(failure, method="teams")
    sent = {"team_id": rig.team.id} | form

    answer = await call(rig, "POST", f"{PATH}/access-check", ALICE, sent)

    assert answer.status_code == 200
    assert answer.json() == {"result": "failed", "reason": reason, "field": field}


async def test_a_project_of_another_team_fails_the_check_on_the_project(rig) -> None:
    bind(rig)
    other_project = (await rig.linear.projects(rig.other.id))[0]

    answer = await call(
        rig, "POST", f"{PATH}/access-check", ALICE, {"team_id": rig.team.id, "project_id": other_project.id}
    )

    assert answer.json() == {"result": "failed", "reason": "linear_project_not_found", "field": "project"}
