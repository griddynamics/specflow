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
from datetime import UTC, datetime
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
from box.connections import ConnectionView, StoredConnection
from github.connections import GitHubConnections
from github_support import CODE_CANARY, USER_ID, FakeGitHub, MemoryStore, github_settings
from integrations_db_fakes import FakeIntegrationsDb
from specflow_api_agent_support import ALPHA, agent_env, serve_agent
from test_api_atlassian_routes import call, error_of

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
SITE_HOST = "example.atlassian.net"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env, tmp_path):
    """The app with Atlassian (oauth mode), GitHub and Slack on: ``rig.atlassian`` and ``rig.github_store`` hold the
    members' connections, ``rig.integrations`` the project rows, ``rig.channels`` the Slack bindings."""

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
        box_views: dict[uuid.UUID, ConnectionView] = {}

        async def box_get(user_id: uuid.UUID) -> ConnectionView | None:
            return box_views.get(user_id)

        runtime.box = SimpleNamespace(get=box_get)  # the Box service as the routes read it: one view per member
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
            box_views=box_views,
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


async def test_a_project_without_connections_lists_the_four_apps_with_nothing_acting(rig) -> None:
    answer = await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN)

    assert [item["app"] for item in answer.json()["integrations"]] == ["atlassian", "github", "slack", "box"]
    assert list(apps(answer).values()) == [
        {
            "app": app,
            "available": True,
            "connected_by": None,
            "workspace": None,
            "connected_at": None,
            "health": "none",
        }
        for app in ("atlassian", "github", "slack", "box")
    ]


async def test_a_connected_app_names_the_member_its_workspace_and_the_health_of_their_connection(rig) -> None:
    rig.atlassian.add(rig.alice.id)
    await connect_github(rig, rig.ben.id, "octo-ben")
    rig.integrations.put(rig.alpha, "atlassian", rig.alice.id, workspace_id=CLOUD, workspace_name="stored")
    rig.integrations.put(rig.alpha, "github", rig.ben.id, workspace_id="octo-ben", workspace_name="octo-ben")
    rig.integrations.put(rig.alpha, "slack", rig.alice.id, workspace_id="T1", workspace_name="Shop")
    await rig.channels.bind(Binding(str(rig.alpha), "C1", "shop-gaps", str(rig.alice.id)))

    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations", BEN))

    assert listed["atlassian"]["connected_by"] == {"user_id": str(rig.alice.id), "name": "Alice"}
    assert listed["atlassian"]["workspace"] == {"id": CLOUD, "name": SITE_HOST}  # the configured site names it
    assert listed["github"]["connected_by"] == {"user_id": str(rig.ben.id), "name": "Ben"}
    assert listed["github"]["workspace"] == {"id": "octo-ben", "name": "octo-ben"}
    assert listed["slack"]["workspace"] == {"id": "T1", "name": "Shop"}
    assert {app: item["health"] for app, item in listed.items()} == {
        **dict.fromkeys(("atlassian", "github", "slack"), "active"),
        "box": "none",  # nobody connected it
    }
    assert all(item["connected_at"] is not None for app, item in listed.items() if app != "box")


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
    rig.runtime.box = None
    rig.runtime.jira = SimpleNamespace(connections=None)  # token mode: one account for the server, no per-user ones

    listed = apps(await call(rig, "GET", f"projects/{ALPHA}/integrations"))

    assert {app: item["available"] for app, item in listed.items()} == dict.fromkeys(
        ("atlassian", "github", "slack", "box"), False
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


async def test_a_member_connects_box_as_themselves_and_the_workspace_is_the_enterprise_or_the_person(rig) -> None:
    missing = await call(rig, "PUT", f"projects/{ALPHA}/integrations/box")
    rig.box_views[rig.alice.id] = ConnectionView.of(
        StoredConnection(rig.alice.id, "11", "Alice", "ent-1", "Acme", b"x", "expired", T0, T0)
    )
    expired = await call(rig, "PUT", f"projects/{ALPHA}/integrations/box")
    rig.box_views[rig.alice.id] = ConnectionView.of(
        StoredConnection(rig.alice.id, "11", "Alice", "ent-1", "Acme", b"x", "active", T0, T0)
    )

    answer = await call(rig, "PUT", f"projects/{ALPHA}/integrations/box")

    assert error_of(missing) == (409, "box_not_connected")
    assert error_of(expired) == (409, "box_reconnect_required")  # a connection that expired is not a missing one
    assert answer.status_code == 200
    assert answer.json()["workspace"] == {"id": "ent-1", "name": "Acme"} and answer.json()["health"] == "active"
    assert rig.integrations.pointed == []  # Box has no acting-account column: the importer reads the bound member
    personal = ConnectionView.of(StoredConnection(rig.alice.id, "22", "Pat", None, None, b"x", "active", T0, T0))
    assert (personal.workspace_id, personal.workspace_name) == ("user:22", "Pat")


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

    rig.runtime.box = None

    github = await call(rig, "PUT", f"projects/{ALPHA}/integrations/github")
    atlassian = await call(rig, "PUT", f"projects/{ALPHA}/integrations/atlassian")
    box = await call(rig, "PUT", f"projects/{ALPHA}/integrations/box")

    assert error_of(github) == (503, "github_unavailable")
    assert error_of(atlassian) == (503, "atlassian_oauth_unavailable")
    assert error_of(box) == (503, "box_unavailable")


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
    assert error_of(await call(rig, "DELETE", f"projects/{ALPHA}/integrations/notion")) == (422, "validation_failed")


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
