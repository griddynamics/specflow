"""The ``oauth`` mode over the REST app (docs/jira-sync/oauth.md "Routes", "Acting account of a target"): the
``/integrations/atlassian`` routes, the Jira settings and access check with per-user connections, and the runner's
acting account, on the fake Jira tables, the fake ``db.atlassian``, a scripted OAuth client and one fake Jira per
user. Outside ``oauth`` mode the connection routes answer 503 and the Jira tab is unchanged.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from api.jira.connections import AtlassianConnections
from api.jira.runner import JiraRunnerDependencies, JiraSyncRunner
from api.jira.sync import SyncCounts, SyncResult
from atlassian_support import ACCOUNT_ID, ACCOUNT_NAME, CLIENT_CANARY, FakeAtlassianDb, FakeOAuthClient, oauth_settings
from jira.oauth import OAuthCode
from jira_db_fakes import FakeJiraDb
from jira_fake_support import FakeJiraGateway
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, mcp_token, serve_agent
from specflow_api_support import token as web_token
from test_api_jira_routes import target_view

SITE = "https://example.atlassian.net"
ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
OK = SyncResult("succeeded", None, SyncCounts(created=1))
SETTINGS = {"jira_project": "PRJ", "epic_key": None, "enabled": True}
CANARIES = ("code-canary-4f1", "access-first", "refresh-first", ACCOUNT_ID, ACCOUNT_NAME, CLIENT_CANARY)


class StubSync:
    """``sync_once`` stand-in: records the gateway each pass got."""

    def __init__(self) -> None:
        self.gateways: list[Any] = []

    async def __call__(self, _project_id: str, deps: Any) -> SyncResult:
        self.gateways.append(deps.gateway)
        return OK


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    """The app in ``oauth`` mode: the runner's connections over the fakes; ``rig.jiras`` is each user's fake Jira."""

    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        jira_db, atlassian_db, client, stub = FakeJiraDb(), FakeAtlassianDb(), FakeOAuthClient(), StubSync()
        jira_db.install(monkeypatch)
        atlassian_db.install(monkeypatch)
        runtime, repo = served.runtime, served.repo
        jiras: dict[str, FakeJiraGateway] = {}

        def gateway(cloud_id: str, tokens: Any) -> FakeJiraGateway:
            made = FakeJiraGateway()
            made.tokens = tokens
            jiras[cloud_id + str(len(jiras))] = made
            return made

        connections = AtlassianConnections(
            oauth_settings(), transaction=runtime.transaction, client=client, gateway_factory=gateway
        )
        runner = JiraSyncRunner(
            None,
            transaction=runtime.transaction,
            store=None,
            public_url=None,
            site_url=SITE,
            dependencies=JiraRunnerDependencies(sync_once=stub),
            connections=connections,
        )
        runtime.jira = runner
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        alpha = uuid.UUID(ALPHA)
        repo.members.add((alpha, ben.id))
        repo.roles[(alpha, ben.id)] = "collaborator"
        carol = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            jira_db=jira_db,
            db=atlassian_db,
            client=client,
            stub=stub,
            runner=runner,
            connections=connections,
            alpha=alpha,
            alice=served.users["alice"],
            ben=ben,
            carol=carol,
        )
        await runner.aclose()


async def call(rig: Any, method: str, path: str, who: str = ALICE, body: Any = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {web_token(who)}"}
    return await rig.http.request(method, f"/api/v1/{path}", json=body, headers=headers)


def error_of(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def settle(rig: Any) -> None:
    tasks = [p.task for p in rig.runner._projects.values() if p.task is not None]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


async def authorize(rig: Any, who: str = ALICE, return_to: str = "/projects/p/settings") -> str:
    url = (await call(rig, "POST", "integrations/atlassian/authorize", who, {"return_to": return_to})).json()["url"]
    return url.split("state=", 1)[1]


# The connection routes ---------------------------------------------------------------------------------------------


async def test_get_without_a_connection_answers_the_mode_and_nothing_else(rig) -> None:
    answer = await call(rig, "GET", "integrations/atlassian")

    assert answer.status_code == 200
    assert answer.json() == {
        "mode": "oauth",
        "connected": False,
        "status": None,
        "account_name": None,
        "connected_at": None,
        "last_used_at": None,
    }


async def test_the_connect_flow_stores_the_connection_and_answers_it_with_the_return_to(rig, caplog) -> None:
    caplog.set_level("DEBUG")
    state = await authorize(rig, return_to="/projects/p/settings?tab=jira")

    answer = await call(
        rig, "POST", "integrations/atlassian/callback", body={"code": "code-canary-4f1", "state": state}
    )

    assert answer.status_code == 200
    body = answer.json()
    assert body["return_to"] == "/projects/p/settings?tab=jira"
    assert body["connection"]["connected"] is True and body["connection"]["status"] == "active"
    assert body["connection"]["account_name"] == ACCOUNT_NAME and body["connection"]["mode"] == "oauth"
    assert rig.client.exchanges == ["code-canary-4f1"]
    mine = (await call(rig, "GET", "integrations/atlassian")).json()
    assert (mine["connected"], mine["status"]) == (True, "active")
    assert (await call(rig, "GET", "integrations/atlassian", BEN)).json()["connected"] is False
    haystack = log_haystack(caplog)
    assert not [canary for canary in CANARIES if canary in haystack] and state not in haystack


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("other_user", (400, "atlassian_state_invalid")),
        ("garbage", (400, "atlassian_state_invalid")),
        ("code", (400, "atlassian_code_invalid")),
        ("site", (403, "atlassian_site_not_granted")),
        ("unreachable", (502, "atlassian_unreachable")),
    ],
)
async def test_a_refused_callback_answers_its_code_logs_it_and_keeps_nothing(rig, caplog, case, expected) -> None:
    caplog.set_level("DEBUG")
    state = await authorize(rig, BEN if case == "other_user" else ALICE)
    if case == "garbage":
        state = "not.a.state"
    if case == "code":
        rig.client.exchange_error = OAuthCode.CODE_INVALID
    if case == "site":
        rig.client.site_error = OAuthCode.SITE_NOT_GRANTED
    if case == "unreachable":
        rig.client.exchange_error = OAuthCode.UNREACHABLE

    answer = await call(
        rig, "POST", "integrations/atlassian/callback", body={"code": "code-canary-4f1", "state": state}
    )

    assert error_of(answer) == expected
    assert rig.db.rows == {}
    refused = [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.callback.refused"]
    assert [getattr(r, "code", None) for r in refused] == [expected[1]]
    haystack = log_haystack(caplog) + answer.text
    assert "code-canary-4f1" not in haystack and state not in log_haystack(caplog)


@pytest.mark.parametrize(
    "return_to", ["https://evil.example.com/x", "//evil.example.com/x", "/\\evil.example.com", "projects/p", "", "/a b"]
)
async def test_authorize_refuses_a_return_to_that_is_not_a_path_of_this_site(rig, return_to) -> None:
    answer = await call(rig, "POST", "integrations/atlassian/authorize", body={"return_to": return_to})

    assert error_of(answer) == (422, "validation_failed")


async def test_the_authorize_url_is_atlassians_with_a_state_bound_to_the_caller(rig) -> None:
    state = await authorize(rig, return_to="/p")

    assert rig.connections._states.verify(state, str(rig.alice.id)) == "/p"


async def test_disconnect_deletes_the_connection_and_logs_it(rig, caplog) -> None:
    caplog.set_level("DEBUG")
    rig.db.add(rig.alice.id)

    answer = await call(rig, "DELETE", "integrations/atlassian")

    assert answer.status_code == 204 and answer.content == b""
    assert rig.db.rows == {}
    assert [r for r in caplog.records if getattr(r, "event", None) == "api.atlassian.disconnected"]


@pytest.mark.parametrize(
    ("method", "path"), [("GET", ""), ("POST", "/authorize"), ("POST", "/callback"), ("DELETE", "")]
)
async def test_the_connection_routes_need_a_signed_in_user(rig, method, path) -> None:
    body = {"return_to": "/p"} if path == "/authorize" else {"code": "c", "state": "s"} if path else None

    answer = await rig.http.request(method, f"/api/v1/integrations/atlassian{path}", json=body)

    assert answer.status_code == 401


@pytest.mark.parametrize("mode", ["off", "token"])
async def test_outside_oauth_mode_the_connection_routes_answer_503_and_get_names_the_mode(rig, mode) -> None:
    rig.runtime.jira = (
        None
        if mode == "off"
        else JiraSyncRunner(
            FakeJiraGateway(), transaction=rig.runtime.transaction, store=None, public_url=None, site_url=SITE
        )
    )

    authorize_answer = await call(rig, "POST", "integrations/atlassian/authorize", body={"return_to": "/p"})
    callback_answer = await call(rig, "POST", "integrations/atlassian/callback", body={"code": "c", "state": "s"})
    delete_answer = await call(rig, "DELETE", "integrations/atlassian")
    get_answer = await call(rig, "GET", "integrations/atlassian")

    for answer in (authorize_answer, callback_answer, delete_answer):
        assert error_of(answer) == (503, "atlassian_oauth_unavailable")
    assert get_answer.json()["mode"] == (None if mode == "off" else "token")
    assert get_answer.json()["connected"] is False
    rig.runtime.jira = rig.runner


# The Jira settings in oauth mode -----------------------------------------------------------------------------------


async def test_saving_needs_the_owners_active_connection(rig) -> None:
    missing = await call(rig, "PUT", f"projects/{ALPHA}/jira", body=SETTINGS)
    rig.db.add(rig.alice.id, status="expired")
    expired = await call(rig, "PUT", f"projects/{ALPHA}/jira", body=SETTINGS)

    assert error_of(missing) == error_of(expired) == (409, "jira_not_connected")
    assert rig.jira_db.targets == {}


async def test_saving_makes_the_owner_the_acting_account_and_the_pass_runs_as_them(rig) -> None:
    rig.db.add(rig.alice.id)

    answer = await call(rig, "PUT", f"projects/{ALPHA}/jira", body=SETTINGS)
    await settle(rig)

    assert answer.status_code == 200
    assert rig.jira_db.targets[rig.alpha].connected_user_id == rig.alice.id
    assert answer.json()["connection"] == {
        "connected_by_name": ACCOUNT_NAME,
        "connected_by_me": True,
        "status": "active",
    }
    assert rig.stub.gateways == [await rig.connections.gateway(rig.alice.id)]
    seen_by_ben = (await call(rig, "GET", f"projects/{ALPHA}/jira", BEN)).json()["connection"]
    assert seen_by_ben == {"connected_by_name": ACCOUNT_NAME, "connected_by_me": False, "status": "active"}


@pytest.mark.parametrize(
    ("acting", "status", "connection"),
    [("none", None, "missing"), ("alice", None, "missing"), ("alice", "expired", "expired")],
)
async def test_get_shows_the_acting_account_state(rig, acting, status, connection) -> None:
    acting_id = rig.alice.id if acting == "alice" else None
    rig.jira_db.targets[rig.alpha] = target_view(rig.alpha, jira_project="PRJ", connected_user_id=acting_id)
    if status is not None:
        rig.db.add(rig.alice.id, status=status)

    answer = (await call(rig, "GET", f"projects/{ALPHA}/jira")).json()

    name = ACCOUNT_NAME if status is not None else None
    assert answer["connection"] == {
        "connected_by_name": name,
        "connected_by_me": acting == "alice",
        "status": connection,
    }


@pytest.mark.parametrize(
    ("acting", "status", "code"),
    [
        (False, None, "jira_not_connected"),
        (True, "expired", "jira_reconnect_required"),
        (True, None, "jira_not_connected"),
    ],
)
async def test_a_pass_whose_acting_connection_cannot_act_fails_with_its_code_and_no_jira_call(
    rig, acting, status, code
) -> None:
    """Members start passes through the agent route: the pass acts as the owner the target names, never the caller."""

    rig.jira_db.targets[rig.alpha] = target_view(
        rig.alpha, jira_project="PRJ", connected_user_id=rig.alice.id if acting else None
    )
    if status is not None:
        rig.db.add(rig.alice.id, status=status)
    rig.db.add(rig.ben.id)  # the member who starts the pass has a connection: it is not used

    headers = {"Authorization": f"Bearer {mcp_token(BEN)}"}
    started = await rig.http.post("/api/v1/agent/jira-sync", params={"project_id": ALPHA}, json={}, headers=headers)
    await settle(rig)

    assert started.status_code == 200
    stored = rig.jira_db.targets[rig.alpha]
    assert (stored.last_status, stored.last_error_code) == ("failed", code)
    assert rig.stub.gateways == []


async def test_a_member_started_pass_acts_as_the_owner_the_target_names(rig) -> None:
    rig.jira_db.targets[rig.alpha] = target_view(rig.alpha, jira_project="PRJ", connected_user_id=rig.alice.id)
    rig.db.add(rig.alice.id)
    rig.db.add(rig.ben.id)

    headers = {"Authorization": f"Bearer {mcp_token(BEN)}"}
    await rig.http.post("/api/v1/agent/jira-sync", params={"project_id": ALPHA}, json={}, headers=headers)
    await settle(rig)

    assert rig.stub.gateways == [await rig.connections.gateway(rig.alice.id)]
    assert rig.stub.gateways[0] is not await rig.connections.gateway(rig.ben.id)


# The access check per mode -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "reason"), [(None, "jira_not_connected"), ("expired", "jira_reconnect_required")])
async def test_the_access_check_uses_the_callers_own_connection(rig, status, reason) -> None:
    if status is not None:
        rig.db.add(rig.alice.id, status=status)

    answer = await call(rig, "POST", f"projects/{ALPHA}/jira/access-check", body={"jira_project": "PRJ"})

    assert answer.status_code == 200
    assert answer.json() == {"result": "failed", "reason": reason, "field": None}


async def test_the_access_check_of_a_connected_owner_asks_their_jira(rig) -> None:
    rig.db.add(rig.alice.id)

    answer = await call(rig, "POST", f"projects/{ALPHA}/jira/access-check", body={"jira_project": "PRJ"})

    assert answer.json() == {"result": "ok", "project_name": "Project", "epic_summary": None}
    gateway = await rig.connections.gateway(rig.alice.id)
    assert [name for name, _ in gateway.calls][:2] == ["myself", "project"]


async def test_in_token_mode_the_settings_carry_no_connection_and_saving_needs_none(rig) -> None:
    gateway = FakeJiraGateway()
    rig.runtime.jira = JiraSyncRunner(
        gateway, transaction=rig.runtime.transaction, store=None, public_url=None, site_url=SITE
    )

    saved = await call(rig, "PUT", f"projects/{ALPHA}/jira", body={**SETTINGS, "enabled": False})
    checked = await call(rig, "POST", f"projects/{ALPHA}/jira/access-check", body={"jira_project": "PRJ"})

    assert saved.status_code == 200 and saved.json()["connection"] is None
    assert checked.json()["result"] == "ok" and gateway.calls[0][0] == "myself"
    await rig.runtime.jira.aclose()
    rig.runtime.jira = rig.runner
