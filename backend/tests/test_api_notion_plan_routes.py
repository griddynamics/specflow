"""The Notion plan destination routes over the REST app (docs/notion/plan.md "Routes"): the target and the access
check, as the member the project's Notion is connected by. Notion is ``FakeNotionGateway`` behind
``FakeNotionConnections``; the rows are in memory.

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

from api.notion_plan.runner import NotionPlanRunner
from api.notion_plan.sync import PushCounts, PushDeps, PushResult
from db import notion_plan as plan_db
from db.notion_plan import ParentChoice, PlanTargetView, TargetSaved
from integrations_db_fakes import FakeIntegrationsDb
from notion.fake import PRD_PAGE_ID, REQUIREMENTS_DATABASE_ID, ROOT_PAGE_ID
from notion.reasons import NotionCode
from notion_plan_support import PARENT_ID, target_view
from notion_support import FakeNotionConnections
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of
from test_api_github_routes import ALICE, BEN, CAROL

PID = uuid.UUID(ALPHA)
PATH = f"projects/{ALPHA}/notion-plan"
ROOT_LINK = "https://www.notion.so/Specflow-Notion-Lab-" + ROOT_PAGE_ID.replace("-", "")
DATABASE_LINK = "https://www.notion.so/" + REQUIREMENTS_DATABASE_ID.replace("-", "") + "?v=" + "a" * 32
PRD_LINK = "https://www.notion.so/" + PRD_PAGE_ID.replace("-", "")


class FakePlanDb:
    """The target functions of ``db.notion_plan`` over one dict."""

    def __init__(self) -> None:
        self.targets: dict[uuid.UUID, PlanTargetView] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("get_target", "save_target", "record_start", "record_end", "running_targets"):
            monkeypatch.setattr(plan_db, name, getattr(self, name))

    async def get_target(self, _session: Any, project_id: uuid.UUID) -> PlanTargetView | None:
        return self.targets.get(project_id)

    async def save_target(
        self,
        _session: Any,
        project_id: uuid.UUID,
        *,
        parent: ParentChoice,
        enabled: bool,
        now: datetime,
        database_lost: bool = False,
    ) -> TargetSaved:
        before = self.targets.get(project_id)
        base = before or target_view(project_id)
        changed = before is None or before.parent_page_id != parent.page_id or database_lost
        self.targets[project_id] = replace(
            base,
            parent_page_id=parent.page_id,
            parent_page_title=parent.title,
            parent_page_url=parent.url,
            database_id=None if changed else base.database_id,
            enabled=enabled,
            updated_at=now,
        )
        return TargetSaved(
            destination_changed=before is not None and changed,
            turned_on=enabled and (before is None or not before.enabled),
            keys_changed=changed or base.database_id is None,
        )

    async def record_start(self, _session: Any, project_id: uuid.UUID, *, trigger: str, queued_at: datetime) -> None:
        self._update(project_id, last_status="running", last_trigger=trigger, last_started_at=queued_at)

    async def record_end(self, _session: Any, project_id: uuid.UUID, **values: Any) -> None:
        self._update(
            project_id,
            last_status=values["status"],
            last_error_code=values["error_code"],
            last_counts=values["counts"],
        )

    async def running_targets(self, _session: Any) -> list[uuid.UUID]:
        return []

    def _update(self, project_id: uuid.UUID, **columns: Any) -> None:
        self.targets[project_id] = replace(self.targets[project_id], **columns)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        db, integrations, connections = FakePlanDb(), FakeIntegrationsDb(), FakeNotionConnections()
        passes: list[str] = []
        integrations.install(monkeypatch)
        db.install(monkeypatch)

        async def push(project_id: str, _deps: PushDeps) -> PushResult:
            passes.append(project_id)
            return PushResult("succeeded", None, PushCounts(created=3, deferred=1))

        runtime, repo = served.runtime, served.repo
        runner = NotionPlanRunner(connections, transaction=runtime.transaction, graph_store=object(), push=push)
        runtime.notion, runtime.notion_plan = connections, runner
        alice = served.users["alice"]
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        repo.members.add((PID, ben.id))
        repo.roles[(PID, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        connections.users[alice.id] = "active"
        connections.users[ben.id] = "active"
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            runner=runner,
            db=db,
            integrations=integrations,
            connections=connections,
            fake=connections.fake,
            passes=passes,
            alice=alice.id,
            ben=ben.id,
        )
        runtime.notion = runtime.notion_plan = None
        await runner.aclose()


def bind(rig: Any, user: uuid.UUID | None = None) -> None:
    """The project's Notion is connected by ``user`` (alice by default)."""

    rig.integrations.put(
        PID, "notion", user or rig.alice, workspace_id=rig.connections.workspace_id, workspace_name="Sample Workspace"
    )


async def settle(rig: Any) -> None:
    tasks = [p.task for p in rig.runner._projects.values() if p.task is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def body(**values: Any) -> dict[str, Any]:
    return {"link": ROOT_LINK, "enabled": True} | values


# GET and PUT ------------------------------------------------------------------------------------------------------


async def test_a_project_with_no_destination_reads_as_available_and_empty(rig) -> None:
    answer = (await call(rig, "GET", PATH)).json()

    assert answer["available"] is True and answer["enabled"] is False
    assert (answer["parent_page_id"], answer["database_url"], answer["sync"]) == (None, None, None)
    assert (answer["connected_by_name"], answer["connected_by_me"]) == (None, False)


async def test_saving_a_destination_resolves_the_page_as_the_bound_member_and_starts_a_pass(rig) -> None:
    bind(rig)

    answer = await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)

    assert answer.status_code == 200
    saved = answer.json()
    assert (saved["parent_page_id"], saved["parent_page_title"]) == (PARENT_ID, "Specflow Notion Lab")
    assert saved["parent_page_url"] and saved["database_url"] is None and saved["enabled"] is True
    assert (saved["connected_by_name"], saved["connected_by_me"]) == ("Sample Workspace", True)
    assert saved["sync"]["status"] == "running" and saved["sync"]["trigger"] == "settings"
    assert rig.passes == [ALPHA] and rig.connections.gateways_asked[0] == rig.alice
    later = (await call(rig, "GET", PATH)).json()
    assert (later["sync"]["status"], later["sync"]["counts"]["created"]) == ("succeeded", 3)
    assert later["sync"]["counts"]["deferred"] == 1


async def test_saving_the_same_page_again_before_its_database_exists_asks_for_a_pass_and_turning_it_off_saves_it(
    rig,
) -> None:
    bind(rig)
    await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)

    again = await call(rig, "PUT", PATH, ALICE, body())
    off = await call(rig, "PUT", PATH, ALICE, body(enabled=False))
    await settle(rig)

    assert again.status_code == off.status_code == 200
    assert off.json()["enabled"] is False and rig.passes == [
        ALPHA,
        ALPHA,
    ]  # no database yet: the first pass may have failed


async def test_a_pass_acts_in_the_workspace_of_the_member_the_project_is_bound_to_not_the_caller(rig) -> None:
    bind(rig, rig.ben)

    answer = (await call(rig, "PUT", PATH, ALICE, body())).json()
    await settle(rig)

    assert (answer["connected_by_name"], answer["connected_by_me"]) == ("Sample Workspace", False)
    assert rig.connections.gateways_asked[0] == rig.ben


async def test_saving_without_the_projects_notion_binding_is_refused_and_saves_nothing(rig) -> None:
    assert error_of(await call(rig, "PUT", PATH, ALICE, body())) == (409, "notion_not_connected")
    assert rig.db.targets == {} and rig.passes == []


async def test_saving_with_an_expired_connection_asks_to_reconnect(rig) -> None:
    bind(rig)
    rig.connections.expire(rig.alice)

    assert error_of(await call(rig, "PUT", PATH, ALICE, body())) == (409, "notion_reconnect_required")
    assert rig.db.targets == {}


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (DATABASE_LINK, (422, "notion_parent_not_a_page")),
        ("https://example.com/page", (422, "notion_link_invalid")),
        ("  ", (422, "notion_link_invalid")),
        (PRD_LINK.replace("e12275fc660d", "0" * 12), (409, "notion_not_shared")),
    ],
)
async def test_a_link_that_is_no_usable_page_is_one_closed_error_and_saves_nothing(rig, link, expected) -> None:
    bind(rig)

    assert error_of(await call(rig, "PUT", PATH, ALICE, body(link=link))) == expected
    assert rig.db.targets == {} and rig.passes == []


async def test_a_page_the_member_did_not_share_and_a_missing_capability_are_closed_errors(rig) -> None:
    bind(rig)
    rig.fake.unshare(PRD_PAGE_ID)
    unshared = await call(rig, "PUT", PATH, ALICE, body(link=PRD_LINK))
    rig.fake.fail("page", NotionCode.PERMISSIONS_MISSING)
    forbidden = await call(rig, "PUT", PATH, ALICE, body())
    rig.fake.fail("page", NotionCode.UNREACHABLE)
    down = await call(rig, "PUT", PATH, ALICE, body())

    assert error_of(unshared) == (409, "notion_not_shared")
    assert error_of(forbidden) == (409, "notion_permissions_missing")
    assert error_of(down) == (502, "notion_unreachable")


async def test_the_body_takes_a_link_and_a_flag_and_nothing_else(rig) -> None:
    bind(rig)

    extra = await call(rig, "PUT", PATH, ALICE, body(surprise=1))
    missing = await call(rig, "PUT", PATH, ALICE, {"link": ROOT_LINK})
    loose = await call(rig, "PUT", PATH, ALICE, body(enabled="yes"))

    assert [r.status_code for r in (extra, missing, loose)] == [422, 422, 422]


async def test_notion_off_on_this_server_is_a_503_and_an_unavailable_read(rig) -> None:
    rig.runtime.notion_plan = None

    assert (await call(rig, "GET", PATH)).json()["available"] is False
    assert error_of(await call(rig, "PUT", PATH, ALICE, body())) == (503, "notion_unavailable")
    assert error_of(await call(rig, "POST", f"{PATH}/access-check", ALICE, {"link": ROOT_LINK})) == (
        503,
        "notion_unavailable",
    )


async def test_a_collaborator_reads_and_saves_and_a_stranger_sees_no_project(rig) -> None:
    bind(rig)

    assert (await call(rig, "PUT", PATH, BEN, body())).status_code == 200
    assert (await call(rig, "GET", PATH, BEN)).json()["connected_by_me"] is False
    assert (await call(rig, "GET", PATH, ALICE)).json()["connected_by_me"] is True
    assert (await call(rig, "GET", PATH, CAROL)).status_code == 404


# The access check ---------------------------------------------------------------------------------------------------


async def test_the_access_check_says_ok_with_the_title_of_the_page(rig) -> None:
    bind(rig)

    answer = await call(rig, "POST", f"{PATH}/access-check", ALICE, {"link": ROOT_LINK})

    assert answer.json() == {"result": "ok", "parent_page_title": "Specflow Notion Lab"}
    assert rig.db.targets == {} and rig.passes == []  # a check saves nothing


@pytest.mark.parametrize(
    ("setup", "link", "reason"),
    [
        ("unbound", ROOT_LINK, "notion_not_connected"),
        ("expired", ROOT_LINK, "notion_reconnect_required"),
        ("bound", DATABASE_LINK, "notion_parent_not_a_page"),
        ("bound", "https://example.com/x", "notion_link_invalid"),
        ("unshared", ROOT_LINK, "notion_not_shared"),
        ("permissions", ROOT_LINK, "notion_permissions_missing"),
        ("down", ROOT_LINK, "notion_unreachable"),
        ("refused", ROOT_LINK, "notion_auth_failed"),
    ],
)
async def test_the_access_check_names_what_failed_as_a_closed_reason_with_a_200(rig, setup, link, reason) -> None:
    if setup != "unbound":
        bind(rig)
    if setup == "expired":
        rig.connections.expire(rig.alice)
    elif setup == "unshared":
        rig.fake.unshare(ROOT_PAGE_ID)
    elif setup in ("permissions", "down", "refused"):
        code = {"permissions": NotionCode.PERMISSIONS_MISSING, "down": NotionCode.UNREACHABLE}
        rig.fake.fail("page", code.get(setup, NotionCode.REFUSED))

    answer = await call(rig, "POST", f"{PATH}/access-check", ALICE, {"link": link})

    assert answer.status_code == 200
    assert answer.json() == {"result": "failed", "reason": reason}
    assert not rig.connections.gateways_asked or setup != "unbound"


async def test_an_access_check_of_another_member_acts_as_the_bound_member(rig) -> None:
    bind(rig, rig.ben)

    await call(rig, "POST", f"{PATH}/access-check", ALICE, {"link": ROOT_LINK})

    assert rig.connections.gateways_asked == [rig.ben]


async def test_saving_the_same_page_while_its_database_is_trashed_starts_over_and_starts_a_pass(rig) -> None:
    bind(rig)
    await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)
    made = await rig.fake.create_database(ROOT_PAGE_ID, "Graphico plan", {})
    rig.db.targets[PID] = replace(rig.db.targets[PID], database_id=made.id, data_source_id="ds")
    rig.fake.databases[made.id] = replace(made, in_trash=True)

    answer = await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)

    assert answer.status_code == 200
    assert rig.db.targets[PID].database_id is None
    assert rig.passes == [ALPHA, ALPHA]


async def test_saving_the_same_page_while_its_database_is_alive_keeps_it_and_starts_no_pass(rig) -> None:
    bind(rig)
    await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)
    made = await rig.fake.create_database(ROOT_PAGE_ID, "Graphico plan", {})
    rig.db.targets[PID] = replace(rig.db.targets[PID], database_id=made.id, data_source_id="ds")

    await call(rig, "PUT", PATH, ALICE, body())
    await settle(rig)

    assert rig.db.targets[PID].database_id == made.id and rig.passes == [ALPHA]


async def test_a_member_who_reconnected_into_another_workspace_is_refused_before_any_notion_call(rig) -> None:
    bind(rig)
    rig.connections.workspace_id = "ws-other"  # the binding still names the first workspace

    checked = (await call(rig, "POST", f"{PATH}/access-check", ALICE, {"link": ROOT_LINK})).json()
    saved = await call(rig, "PUT", PATH, ALICE, body())

    assert checked["result"] == "failed" and checked["reason"] == "notion_reconnect_required"
    assert error_of(saved) == (409, "notion_reconnect_required")
    assert rig.fake.calls == [] and rig.connections.gateways_asked == [] and rig.passes == []
