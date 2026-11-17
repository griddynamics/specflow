"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1``): the collaborator routes on a real, migrated scratch database.

Create with emails, invite adoption on the first sign-in (the web ``/me`` and an agent route share ``upsert_user``,
FR-PRJ-INV-02), the full people flow with the real queries, and the end of access after a removal on both surfaces
(FR-PRJ-MEM, FR-PRJ-ACC). The graph is a fake; the plan answers "no plan".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_contracts import AGENT_ROUTES
from db.models import ProjectInvite, ProjectMember, User
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_api_agent_support import AgentRig, agent_env, bearer_header, mcp_token, serve_agent
from specflow_api_members_support import ScriptedPlan, install
from specflow_api_support import bearer
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
LIST_PROJECTS = AGENT_ROUTES["list_projects"]


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


@pytest_asyncio.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def rig(monkeypatch, env, factory) -> AsyncIterator[AgentRig]:
    async with serve_agent(monkeypatch, env, session_factory=factory) as served:
        install(served.runtime, ScriptedPlan(no_plan=True))
        yield served


async def web(rig: AgentRig, method: str, path: str, who: str = ALICE, body: Any = None) -> Any:
    return await rig.client.request(method, f"/api/v1{path}", json=body, headers=bearer(who))


async def agent_projects(rig: AgentRig, who: str) -> Any:
    return await rig.client.get(LIST_PROJECTS, headers=bearer_header(mcp_token(who)))


async def rows(factory: async_sessionmaker[AsyncSession], statement: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(statement)).all())


async def sign_in(rig: AgentRig, who: str) -> None:
    assert (await web(rig, "GET", "/me", who)).status_code == 200


async def test_create_with_emails_writes_members_and_invites_in_one_go_and_a_bad_list_writes_nothing(rig, factory):
    await sign_in(rig, BEN)
    created = await web(
        rig, "POST", "/projects", body={"name": "Team", "collaborators": [BEN.upper(), "new@example.com"]}
    )
    assert created.status_code == 201
    pid = uuid.UUID(created.json()["id"])
    people = await rows(
        factory,
        select(User.email, ProjectMember.role)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == pid),
    )
    roles = dict(people)
    assert roles == {ALICE: "admin", BEN: "collaborator"}
    assert [
        e for (e,) in await rows(factory, select(ProjectInvite.email_norm).where(ProjectInvite.project_id == pid))
    ] == ["new@example.com"]
    before = [n for (n,) in await rows(factory, select(func.count()).select_from(ProjectMember))]
    bad = await web(rig, "POST", "/projects", body={"name": "Bad", "collaborators": ["new2@example.com", "nope"]})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["errors"] == [
        {"loc": ["body", "collaborators", "1"], "type": "invalid_email"}
    ]
    after = [n for (n,) in await rows(factory, select(func.count()).select_from(ProjectMember))]
    assert before == after


async def test_an_invite_is_adopted_by_the_first_web_sign_in(rig, factory):
    created = await web(rig, "POST", "/projects", body={"name": "Team", "collaborators": [CAROL]})
    pid = created.json()["id"]
    assert [i["email"] for i in (await web(rig, "GET", f"/projects/{pid}/members")).json()["invites"]] == [CAROL]
    await sign_in(rig, CAROL)  # /me upserts the user, which adopts
    listed = (await web(rig, "GET", f"/projects/{pid}/members")).json()
    assert [m["email"] for m in listed["members"]] == [ALICE, CAROL] and listed["invites"] == []
    assert (await web(rig, "GET", f"/projects/{pid}", CAROL)).status_code == 200
    assert await rows(factory, select(ProjectInvite)) == []


async def test_an_invite_is_adopted_by_the_first_agent_call(rig):
    pid = (await web(rig, "POST", "/projects", body={"name": "Team", "collaborators": [CAROL]})).json()["id"]
    answer = await agent_projects(rig, CAROL)  # the agent surface provisions the user the same way
    assert answer.status_code == 200
    assert [item["project_id"] for item in answer.json()["items"]] == [pid]


async def test_the_people_flow_on_real_rows(rig, factory):
    await sign_in(rig, BEN)
    pid = (await web(rig, "POST", "/projects", body={"name": "Alpha"})).json()["id"]
    base = f"/projects/{pid}"
    member = await web(rig, "POST", f"{base}/members", body={"email": BEN})
    invite = await web(rig, "POST", f"{base}/members", body={"email": "Late@Example.com"})
    assert (member.json()["kind"], invite.json()["kind"]) == ("member", "invite")
    assert (await web(rig, "POST", f"{base}/members", body={"email": BEN.upper()})).json()["error"][
        "code"
    ] == "already_member"
    assert (await web(rig, "POST", f"{base}/members", body={"email": "late@example.com"})).json()["error"][
        "code"
    ] == "already_invited"
    assert (await web(rig, "POST", f"{base}/members", BEN, {"email": CAROL})).json()["error"][
        "code"
    ] == "admin_required"

    renamed = await web(rig, "PATCH", base, body={"name": " Beta "})
    assert renamed.json()["name"] == "Beta"
    suggestions = await web(rig, "POST", "/users/suggestions", body={"q": "ben", "project_id": pid})
    assert suggestions.json() == {"items": []}  # Ben is a member now
    assert (await web(rig, "POST", "/users/suggestions", body={"q": "ben"})).json()["items"][0]["email"] == BEN

    invite_id = invite.json()["invite"]["id"]
    assert (await web(rig, "DELETE", f"{base}/invites/{invite_id}")).status_code == 204
    await sign_in(rig, "late@example.com")  # the cancelled invite is not adopted
    assert (await web(rig, "GET", base, "late@example.com")).status_code == 404

    left = await web(rig, "POST", f"{base}/leave", BEN)
    assert (left.status_code, left.json()) == (200, {"claims_released": 0, "claims_release_failed": False})
    assert (await web(rig, "POST", f"{base}/leave")).json()["error"]["code"] == "admin_cannot_leave"


async def test_a_removed_member_loses_web_and_agent_access_at_once(rig, factory):
    await sign_in(rig, BEN)
    pid = (await web(rig, "POST", "/projects", body={"name": "Alpha", "collaborators": [BEN]})).json()["id"]
    assert [item["project_id"] for item in (await agent_projects(rig, BEN)).json()["items"]] == [pid]
    ben_id = (await web(rig, "GET", "/me", BEN)).json()["id"]
    removed = await web(rig, "DELETE", f"/projects/{pid}/members/{ben_id}")
    assert (removed.status_code, removed.json()) == (200, {"claims_released": 0, "claims_release_failed": False})
    assert (await web(rig, "GET", f"/projects/{pid}", BEN)).status_code == 404
    assert (await agent_projects(rig, BEN)).json()["items"] == []
    assert (await web(rig, "DELETE", f"/projects/{pid}/members/{ben_id}")).json()["error"]["code"] == "member_not_found"
    assert (await web(rig, "DELETE", f"/projects/{pid}/members/{uuid.uuid4()}")).json()["error"][
        "code"
    ] == "member_not_found"


async def test_a_removed_member_gets_404_on_a_project_scoped_agent_route(rig):
    """FR-PRJ-ACC-04: the agent route of one project (not just the account-scoped list) answers 404 after removal."""

    await sign_in(rig, BEN)
    pid = (await web(rig, "POST", "/projects", body={"name": "Alpha", "collaborators": [BEN]})).json()["id"]
    features = {"project_id": pid, "limit": 2}
    headers = bearer_header(mcp_token(BEN))
    before = await rig.client.get(AGENT_ROUTES["list_features"], params=features, headers=headers)
    assert before.status_code != 404  # a member passes the membership check (the graph may answer otherwise)
    ben_id = (await web(rig, "GET", "/me", BEN)).json()["id"]
    assert (await web(rig, "DELETE", f"/projects/{pid}/members/{ben_id}")).status_code == 200
    after = await rig.client.get(AGENT_ROUTES["list_features"], params=features, headers=headers)
    assert (after.status_code, after.json()["error"]["code"]) == (404, "project_not_found")


async def test_each_project_route_issues_exactly_one_membership_select(rig, factory):
    """NFR-PRJ-04 on real SQL: the statements run by one request hold exactly one ``projects JOIN project_members``
    read (the membership dependency); no route reads the caller's role a second time."""

    await sign_in(rig, BEN)
    pid = (await web(rig, "POST", "/projects", body={"name": "Alpha", "collaborators": [BEN]})).json()["id"]
    ben_id = (await web(rig, "GET", "/me", BEN)).json()["id"]
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(" ".join(statement.split()))

    engine = factory.kw["bind"].sync_engine
    base = f"/projects/{pid}"
    routes = [
        ("GET", base, ALICE, None),
        ("GET", f"{base}/members", ALICE, None),
        ("POST", f"{base}/members", ALICE, {"email": CAROL}),
        ("PATCH", base, ALICE, {"name": "Beta"}),
        ("POST", "/users/suggestions", ALICE, {"q": "car", "project_id": pid}),
        ("DELETE", f"{base}/invites/{uuid.uuid4()}", ALICE, None),
        ("DELETE", f"{base}/members/{ben_id}", ALICE, None),
        ("POST", f"{base}/leave", ALICE, None),  # the admin: refused after the one membership read
    ]
    event.listen(engine, "before_cursor_execute", record)
    try:
        for method, path, who, body in routes:
            statements.clear()
            answer = await web(rig, method, path, who, body)
            assert answer.status_code < 500, (method, path)
            reads = [s for s in statements if "FROM projects JOIN project_members" in s]
            assert len(reads) == 1, (method, path, statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_at_fifty_people_an_existing_members_email_is_already_member_and_a_new_one_member_limit(rig, factory):
    """FR-PRJ-MEM-06/07: the member check precedes the limit, so a known member never reads ``member_limit``."""

    await sign_in(rig, BEN)
    pid = (await web(rig, "POST", "/projects", body={"name": "Alpha", "collaborators": [BEN]})).json()["id"]
    alice_id = uuid.UUID((await web(rig, "GET", "/me")).json()["id"])
    async with factory() as session, session.begin():
        session.add_all(
            ProjectInvite(project_id=uuid.UUID(pid), email_norm=f"p{n}@example.com", invited_by=alice_id)
            for n in range(48)
        )
    base = f"/projects/{pid}/members"
    assert (await web(rig, "POST", base, body={"email": BEN.upper()})).json()["error"]["code"] == "already_member"
    assert (await web(rig, "POST", base, body={"email": ALICE})).json()["error"]["code"] == "already_member"
    assert (await web(rig, "POST", base, body={"email": "new@example.com"})).json()["error"]["code"] == "member_limit"
    assert (await web(rig, "POST", base, body={"email": "p1@example.com"})).json()["error"]["code"] == "member_limit"
