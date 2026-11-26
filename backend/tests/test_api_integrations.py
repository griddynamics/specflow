"""The project integration routes over the REST app (docs/mission-control/integrations-api.md): who each app of a
project is connected by, connecting Atlassian and GitHub as the caller, and removing a connection. Atlassian and GitHub
run on their scripted fakes (a connection row of each member), Slack on a binding file; the rows of
``project_integrations`` are an in-memory fake.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence of ``PUT`` (an app is called only while no transaction is open)::

    web -> project_member -> the caller's own connection read (Atlassian / GitHub service)
        -> transaction: bind_integration + point the Jira target / the repositories at the caller -> commit
        -> 200 ProjectIntegrationOut (health read through the service again)
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio

from api.jira.connections import AtlassianConnections
from api.jira.runner import JiraSyncRunner
from api.slack.app import NotBinderError
from api.slack.channels import Binding, SlackChannels
from atlassian_support import CLOUD, FakeAtlassianDb, FakeOAuthClient, oauth_env, oauth_settings
from github.connections import GitHubConnections
from github_support import CODE_CANARY, USER_ID, FakeGitHub, MemoryStore, github_settings
from integrations_db_fakes import FakeIntegrationsDb
from notion_support import FakeNotionConnections
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
SITE_HOST = "example.atlassian.net"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env, tmp_path):
    """The app with Atlassian (oauth mode), GitHub, Slack and Notion on: ``rig.atlassian`` and ``rig.github_store``
    hold the members' connections, ``rig.integrations`` the project rows, ``rig.channels`` the Slack bindings."""

    async with serve_agent(monkeypatch, agent_env(valid_env, **oauth_env())) as served:
        atlassian = FakeAtlassianDb()
        atlassian.install(monkeypatch)
        integrations = FakeIntegrationsDb()
        integrations.install(monkeypatch)
        runtime, repo = served.runtime, served.repo
        connections = AtlassianConnections(oauth_settings(), transaction=runtime.transaction, client=FakeOAuthClient())
        runner = JiraSyncRunner(
            None, transaction=runtime.transaction, store=None, public_url=None, site_url="x", connections=connections
        )
        runtime.jira = runner
        github, store = FakeGitHub(), MemoryStore()
        github_connections = GitHubConnections(github_settings(), store=store.transaction, client=github.client())
        runtime.github = github_connections
        runtime.notion = notion = FakeNotionConnections()
        channels = SlackChannels(tmp_path)
        runtime.slack = SimpleNamespace(threads=SimpleNamespace(channels=channels))
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        alpha = uuid.UUID(ALPHA)
        repo.members.add((alpha, ben.id))
        repo.roles[(alpha, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        alice = served.users["alice"]
        integrations.names |= {alice.id: "Alice", ben.id: "Ben"}
        yield SimpleNamespace(
            http=served.client,
            runtime=runtime,
            atlassian=atlassian,
            github=github,
            github_store=store,
            notion=notion,
            integrations=integrations,
            channels=channels,
            alpha=alpha,
            alice=alice,
            ben=ben,
        )
        await runner.aclose()
        await github_connections.aclose()


async def connect_github(rig: Any, user_id: uuid.UUID, login: str = "octo") -> None:
    """The member's own GitHub connection (the callback of the connect flow, on the scripted GitHub)."""

    rig.github.logins[USER_ID] = login
    rig.github.codes[CODE_CANARY] = USER_ID
    state = parse_qs(urlsplit(rig.runtime.github.authorize_url(user_id, "/p")).query)["state"][0]
    await rig.runtime.github.connect(user_id, code=CODE_CANARY, state=state)


def apps(response: httpx.Response) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200
    return {item["app"]: item for item in response.json()["integrations"]}


# GET ------------------------------------------------------------------------------------------------------------------


async def test_a_project_without_connections_lists_the_five_apps_with_nothing_acting(rig) -> None:
    answer = await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN)

    assert [item["app"] for item in answer.json()["integrations"]] == [
        "atlassian",
        "github",
        "slack",
        "linear",
        "notion",
    ]
    assert list(apps(answer).values()) == [
        {
            "app": app,
            "available": app != "linear",  # Linear is off until its connections exist (stream S1)
            "connected_by": None,
            "workspace": None,
            "connected_at": None,
            "health": "none",
        }
        for app in ("atlassian", "github", "slack", "linear", "notion")
    ]


async def test_a_connected_app_names_the_member_its_workspace_and_the_health_of_their_connection(rig) -> None:
    rig.atlassian.add(rig.alice.id)
    await connect_github(rig, rig.ben.id, "octo-ben")
    rig.integrations.put(rig.alpha, "atlassian", rig.alice.id, workspace_id=CLOUD, workspace_name="stored")
    rig.integrations.put(rig.alpha, "github", rig.ben.id, workspace_id="octo-ben", workspace_name="octo-ben")
    rig.integrations.put(rig.alpha, "slack", rig.alice.id, workspace_id="T1", workspace_name="Shop")
    rig.notion.users[rig.ben.id] = "active"
    rig.integrations.put(rig.alpha, "notion", rig.ben.id, workspace_id="ws-1", workspace_name="Sample Workspace")
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.alice.id)))

    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN))

    assert listed["atlassian"]["connected_by"] == {"user_id": str(rig.alice.id), "name": "Alice"}
    assert listed["atlassian"]["workspace"] == {"id": CLOUD, "name": SITE_HOST}  # the configured site names it
    assert listed["github"]["connected_by"] == {"user_id": str(rig.ben.id), "name": "Ben"}
    assert listed["github"]["workspace"] == {"id": "octo-ben", "name": "octo-ben"}
    assert listed["slack"]["workspace"] == {"id": "T1", "name": "Shop"}
    assert listed["notion"]["connected_by"] == {"user_id": str(rig.ben.id), "name": "Ben"}
    assert listed["notion"]["workspace"] == {"id": "ws-1", "name": "Sample Workspace"}
    assert {app: item["health"] for app, item in listed.items()} == {
        **dict.fromkeys(("atlassian", "github", "slack", "notion"), "active"),
        "linear": "none",
    }
    assert all(item["connected_at"] is not None for app, item in listed.items() if app != "linear")


async def test_an_expired_connection_asks_to_reconnect_and_a_missing_one_or_member_acts_as_nothing(rig) -> None:
    rig.atlassian.add(rig.alice.id, status="expired")
    rig.integrations.put(rig.alpha, "atlassian", rig.alice.id)
    rig.integrations.put(rig.alpha, "github", rig.ben.id)  # ben has no GitHub connection any more
    rig.integrations.put(rig.alpha, "slack", None)  # its member left

    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))

    assert listed["atlassian"]["health"] == "reconnect_required"
    assert listed["github"]["health"] == "none" and listed["github"]["connected_by"] is not None
    assert listed["slack"]["health"] == "none" and listed["slack"]["connected_by"] is None
    assert listed["slack"]["workspace"] is not None  # the workspace stays: D3, one per project


async def test_an_app_the_server_does_not_run_is_unavailable(rig) -> None:
    rig.runtime.github = None
    rig.runtime.slack = None
    rig.runtime.notion = None
    rig.runtime.jira = SimpleNamespace(connections=None)  # token mode: one account for the server, no per-user ones

    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))

    assert {app: item["available"] for app, item in listed.items()} == dict.fromkeys(
        ("atlassian", "github", "slack", "linear", "notion"), False
    )


async def test_a_non_member_learns_nothing_of_the_integrations(rig) -> None:
    for method, path in (("GET", ""), ("PUT", "/github"), ("DELETE", "/slack")):
        answer = await call(rig, method, f"projects/{ALPHA}/integrations{path}", CAROL)
        assert error_of(answer) == (404, "project_not_found"), path


# PUT ------------------------------------------------------------------------------------------------------------------


async def test_a_member_connects_atlassian_as_themselves_and_the_jira_target_acts_as_them(rig) -> None:
    rig.atlassian.add(rig.ben.id)

    answer = await call(rig, "PUT", f"projects/{ALPHA}/integrations/atlassian", BEN)  # a collaborator: D6

    assert answer.status_code == 200
    assert answer.json()["connected_by"] == {"user_id": str(rig.ben.id), "name": "Ben"}
    assert answer.json()["workspace"] == {"id": CLOUD, "name": SITE_HOST} and answer.json()["health"] == "active"
    assert rig.integrations.rows[rig.alpha, "atlassian"].user_id == rig.ben.id
    assert rig.integrations.pointed == [("jira", rig.alpha, rig.ben.id)]


async def test_a_member_connects_github_as_themselves_and_the_repositories_are_cloned_with_them(rig) -> None:
    await connect_github(rig, rig.alice.id)

    answer = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github")

    assert answer.status_code == 200
    assert answer.json()["workspace"] == {"id": "octo", "name": "octo"} and answer.json()["health"] == "active"
    assert rig.integrations.pointed == [("repositories", rig.alpha, rig.alice.id)]


@pytest.mark.parametrize("app", ["atlassian", "github"])
async def test_connecting_needs_the_callers_own_active_connection(rig, app) -> None:
    missing = await call(rig, "PUT", f"projects/{ALPHA}/integrations/{app}")
    if app == "atlassian":
        rig.atlassian.add(rig.alice.id, status="expired")
    else:
        await connect_github(rig, rig.alice.id)
        rig.github_store.rows[rig.alice.id] = rig.github_store.rows[rig.alice.id].__class__(
            **{**vars(rig.github_store.rows[rig.alice.id]), "status": "expired"}
        )
    expired = await call(rig, "PUT", f"projects/{ALPHA}/integrations/{app}")

    assert error_of(missing) == error_of(expired) == (409, f"{app}_not_connected")
    assert rig.integrations.rows == {} and rig.integrations.pointed == []


async def test_a_project_stays_in_the_one_workspace_of_its_app(rig) -> None:
    await connect_github(rig, rig.ben.id, "other-org")
    rig.integrations.put(rig.alpha, "github", rig.alice.id, workspace_id="octo", workspace_name="octo")

    refused = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github", BEN)

    assert error_of(refused) == (409, "integration_workspace_mismatch")
    assert rig.integrations.rows[rig.alpha, "github"].user_id == rig.alice.id and rig.integrations.pointed == []


async def test_a_member_takes_over_a_workspace_whose_member_left_and_another_member_of_it_reconnects(rig) -> None:
    await connect_github(rig, rig.ben.id, "other-org")
    rig.integrations.put(rig.alpha, "github", None, workspace_id="octo", workspace_name="octo")  # nobody acts on it

    taken = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github", BEN)

    assert taken.status_code == 200 and taken.json()["workspace"]["id"] == "other-org"
    assert taken.json()["connected_by"]["user_id"] == str(rig.ben.id)
    assert rig.integrations.pointed == [("repositories", rig.alpha, rig.ben.id)]


async def test_a_member_cannot_take_over_a_binding_whose_connection_is_still_active(rig) -> None:
    await connect_github(rig, rig.ben.id, "octo")
    rig.integrations.put(rig.alpha, "github", rig.alice.id, workspace_id="octo", workspace_name="octo")

    held = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github", BEN)
    rig.integrations.inactive.add(rig.alice.id)  # her connection expired: the take-over is allowed
    taken = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github", BEN)

    assert error_of(held) == (409, "integration_already_connected")
    assert taken.status_code == 200 and taken.json()["connected_by"]["user_id"] == str(rig.ben.id)


async def test_the_slack_disconnect_of_a_project_is_the_connectors_or_the_owners(rig) -> None:
    calls: list[bool] = []

    async def disconnect(_project_id: str, *, user_id: uuid.UUID, is_owner: bool) -> None:
        calls.append(is_owner)
        if not is_owner and user_id != rig.alice.id:
            raise NotBinderError

    rig.runtime.slack.disconnect = disconnect

    refused = await call(rig, "DELETE", f"projects/{ALPHA}/slack", BEN)
    allowed = await call(rig, "DELETE", f"projects/{ALPHA}/slack", ALICE)

    assert error_of(refused) == (403, "integration_forbidden") and allowed.status_code == 204
    assert calls == [False, True]


async def test_the_apps_the_server_does_not_run_refuse_a_connect(rig) -> None:
    rig.runtime.github = None
    rig.runtime.jira = SimpleNamespace(connections=None)  # token mode: one account for the server

    github = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github")
    atlassian = await call(rig, "PUT", f"projects/{ALPHA}/integrations/atlassian")

    assert error_of(github) == (503, "github_unavailable")
    assert error_of(atlassian) == (503, "atlassian_oauth_unavailable")


async def test_linear_is_off_and_refuses_a_connect_until_its_connections_exist(rig) -> None:
    refused = await call(rig, "PUT", f"projects/{ALPHA}/integrations/linear")

    assert error_of(refused) == (503, "linear_unavailable")


async def test_slack_is_connected_by_its_command_not_by_put(rig) -> None:
    answer = await call(rig, "PUT", f"projects/{ALPHA}/integrations/slack")

    assert error_of(answer) == (422, "validation_failed")


# DELETE ---------------------------------------------------------------------------------------------------------------


async def test_the_member_who_connected_an_app_or_the_owner_removes_it_and_what_used_it_stops(rig) -> None:
    rig.integrations.put(rig.alpha, "atlassian", rig.ben.id)
    rig.integrations.put(rig.alpha, "github", rig.ben.id)

    by_ben = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/atlassian", BEN)
    by_owner = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/github")  # alice owns the project

    assert by_ben.status_code == by_owner.status_code == 204
    assert rig.integrations.rows == {}
    assert rig.integrations.pointed == [("jira", rig.alpha, None), ("repositories", rig.alpha, None)]


async def test_another_member_may_not_remove_it_and_nothing_changes(rig) -> None:
    rig.integrations.put(rig.alpha, "atlassian", rig.alice.id)

    refused = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/atlassian", BEN)

    assert error_of(refused) == (403, "integration_forbidden")
    assert (rig.alpha, "atlassian") in rig.integrations.rows and rig.integrations.pointed == []


async def test_a_connection_whose_member_left_is_removed_by_the_owner_only(rig) -> None:
    rig.integrations.put(rig.alpha, "github", None)

    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/github", BEN)) == (
        403,
        "integration_forbidden",
    )
    assert (await call(rig, "DELETE", f"projects/{ALPHA}/integrations/github")).status_code == 204


async def test_removing_what_is_not_connected_is_404_and_an_unknown_app_is_422(rig) -> None:
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/github")) == (
        404,
        "integration_not_found",
    )
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack")) == (
        404,
        "integration_not_found",
    )
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion")) == (
        404,
        "integration_not_found",
    )
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/linear")) == (
        404,
        "integration_not_found",
    )
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/asana")) == (422, "validation_failed")


async def test_removing_slack_unbinds_its_channel(rig) -> None:
    rig.integrations.put(rig.alpha, "slack", rig.alice.id)
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.alice.id)))

    answer = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack", BEN)  # not the connector

    assert error_of(answer) == (403, "integration_forbidden")
    assert (await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack")).status_code == 204
    assert await rig.channels.binding(str(rig.alpha)) is None and rig.integrations.rows == {}


async def test_a_slack_row_stays_when_the_channel_cannot_be_unbound(rig, monkeypatch, caplog) -> None:
    rig.integrations.put(rig.alpha, "slack", rig.alice.id)
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.alice.id)))

    async def broken(_project_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.channels, "unbind", broken)

    with caplog.at_level(logging.INFO):
        answer = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack")

    assert error_of(answer) == (502, "slack_unreachable")  # a closed answer, not a 500
    assert "disk full" not in answer.text and "disk full" not in caplog.text
    assert (
        rig.alpha,
        "slack",
    ) in rig.integrations.rows  # a row that says "connected" is never left without its channel


async def test_removing_slack_checks_who_may_while_slack_is_off_on_this_server(rig) -> None:
    rig.runtime.slack = None
    path = f"projects/{ALPHA}/integrations/slack"

    assert error_of(await call(rig, "DELETE", path)) == (404, "integration_not_found")  # no row
    rig.integrations.put(rig.alpha, "slack", rig.alice.id)
    assert error_of(await call(rig, "DELETE", path, BEN)) == (403, "integration_forbidden")  # not the holder
    assert (rig.alpha, "slack") in rig.integrations.rows
    rig.integrations.put(rig.alpha, "slack", rig.ben.id)
    assert (await call(rig, "DELETE", path, BEN)).status_code == 204  # the holder
    assert (rig.alpha, "slack") not in rig.integrations.rows
    rig.integrations.put(rig.alpha, "slack", rig.ben.id)
    assert (await call(rig, "DELETE", path)).status_code == 204  # the owner


async def test_a_slack_binding_taken_over_during_the_removal_is_forbidden_not_silently_removed(
    rig, monkeypatch
) -> None:
    rig.integrations.put(rig.alpha, "slack", rig.ben.id)
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.ben.id)))
    unbind = rig.channels.unbind

    async def unbind_then_a_takeover(project_id: str) -> None:
        await unbind(project_id)
        rig.integrations.put(rig.alpha, "slack", rig.alice.id)  # another member took the binding over meanwhile

    monkeypatch.setattr(rig.channels, "unbind", unbind_then_a_takeover)

    answer = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack", BEN)

    assert error_of(answer) == (403, "integration_forbidden") and (rig.alpha, "slack") in rig.integrations.rows


async def test_a_channel_bound_before_its_row_existed_can_still_be_removed(rig) -> None:
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.alice.id)))

    answer = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack", BEN)

    assert answer.status_code == 204 and await rig.channels.binding(str(rig.alpha)) is None


async def test_a_binding_that_cannot_be_read_is_a_closed_502_and_the_row_stays(rig, monkeypatch, caplog) -> None:
    rig.integrations.put(rig.alpha, "slack", rig.alice.id)

    async def broken(_project_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.channels, "binding", broken)

    with caplog.at_level(logging.INFO):
        answer = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/slack")

    assert error_of(answer) == (502, "slack_unreachable")  # not a 500: the removal is not attempted
    assert "disk full" not in answer.text and "disk full" not in caplog.text
    assert "api.integrations.binding_failed" in caplog.text
    assert (rig.alpha, "slack") in rig.integrations.rows


# Notion ---------------------------------------------------------------------------------------------------------------


async def test_a_member_connects_notion_as_themselves_and_nothing_is_pointed(rig) -> None:
    rig.notion.users[rig.ben.id] = "active"

    answer = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion", BEN)  # a collaborator: D6

    assert answer.status_code == 200
    body = answer.json()
    assert body["connected_by"] == {"user_id": str(rig.ben.id), "name": "Ben"}
    assert body["workspace"] == {"id": "ws-1", "name": "Sample Workspace"} and body["health"] == "active"
    assert rig.integrations.rows[rig.alpha, "notion"].user_id == rig.ben.id
    assert rig.integrations.pointed == []  # Notion has no acting-account column: its uses read the binding every call


@pytest.mark.parametrize("state", ["missing", "expired"])
async def test_connecting_notion_needs_the_callers_own_active_connection(rig, state: str) -> None:
    if state == "expired":
        rig.notion.users[rig.alice.id] = "expired"

    refused = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion")

    assert error_of(refused) == (409, "notion_not_connected")
    assert rig.integrations.rows == {} and rig.integrations.pointed == []


async def test_a_project_stays_in_the_one_notion_workspace(rig) -> None:
    rig.notion.users[rig.ben.id] = "active"
    rig.integrations.put(rig.alpha, "notion", rig.alice.id, workspace_id="another-ws", workspace_name="Other")

    refused = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion", BEN)

    assert error_of(refused) == (409, "integration_workspace_mismatch")
    assert rig.integrations.rows[rig.alpha, "notion"].user_id == rig.alice.id


async def test_a_notion_binding_is_taken_over_only_when_its_connection_is_no_longer_active(rig) -> None:
    rig.notion.users[rig.ben.id] = "active"
    rig.integrations.put(rig.alpha, "notion", rig.alice.id, workspace_id="ws-1", workspace_name="Sample Workspace")

    held = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion", BEN)
    rig.integrations.inactive.add(rig.alice.id)
    taken = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion", BEN)

    assert error_of(held) == (409, "integration_already_connected")
    assert taken.status_code == 200 and taken.json()["connected_by"]["user_id"] == str(rig.ben.id)


async def test_a_notion_binding_whose_member_expired_asks_to_reconnect_and_one_who_left_acts_as_nothing(rig) -> None:
    rig.notion.users[rig.alice.id] = "expired"
    rig.integrations.put(rig.alpha, "notion", rig.alice.id, workspace_id="ws-1", workspace_name="Sample Workspace")
    expired = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))["notion"]
    del rig.notion.users[rig.alice.id]  # disconnected altogether
    gone = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))["notion"]

    assert expired["health"] == "reconnect_required"
    assert gone["health"] == "none" and gone["connected_by"] is not None


async def test_notion_off_on_this_server_refuses_a_connect_and_is_unavailable(rig) -> None:
    rig.runtime.notion = None

    refused = await call(rig, "PUT", f"projects/{ALPHA}/integrations/notion")
    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))["notion"]

    assert error_of(refused) == (503, "notion_unavailable")
    assert listed["available"] is False and rig.integrations.rows == {}


async def test_the_member_who_connected_notion_or_the_owner_removes_it_and_nobody_else(rig) -> None:
    rig.integrations.put(rig.alpha, "notion", rig.ben.id)

    other = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion", CAROL)
    by_alice = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion")  # alice owns the project
    rig.integrations.put(rig.alpha, "notion", rig.alice.id)
    forbidden = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion", BEN)
    by_alice_again = await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion")

    assert error_of(other) == (404, "project_not_found")
    assert by_alice.status_code == by_alice_again.status_code == 204
    assert error_of(forbidden) == (403, "integration_forbidden")
    assert rig.integrations.rows == {} and rig.integrations.pointed == []


async def test_a_notion_member_connected_to_another_workspace_than_the_binding_must_reconnect(rig) -> None:
    rig.notion.users[rig.ben.id] = "active"
    rig.integrations.put(rig.alpha, "notion", rig.ben.id, workspace_id="ws-1", workspace_name="Sample Workspace")
    rig.notion.workspace_id = "ws-1"
    same = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN))["notion"]["health"]
    rig.notion.workspace_id = "ws-other"  # they reconnected into another Notion workspace

    other = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN))["notion"]["health"]

    assert (same, other) == ("active", "reconnect_required")
