"""The Jira routes of ``docs/jira-sync/api.md``: the settings and access-check routes of the web app
(FR-JIRA-CFG-01..07) and the two agent routes behind ``sync_to_jira`` and ``jira_sync_status``
(FR-JIRA-MCP-01..03), over the REST app with a real ``JiraSyncRunner`` on the fake Jira, the fake Jira tables and a
stub ``sync_once``.

Standings: alice owns ``Alpha``, ben is its collaborator, carol is no member. The web routes answer ``Authorization``
tokens of the web audience, the agent routes tokens of the MCP audience.

Sequence of ``PUT`` (the lock order of ``03-handoff.md`` "For 05")::

    web -> project_owner (membership, own transaction) -> body checked -> transaction:
        db.jira.save_target (target row FOR UPDATE; no lock of ``projects``) -> commit
        -> runner.after_settings (after the commit; starts a pass, never waits for it) -> 200 JiraSettingsOut
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from api.jira.runner import JiraRunnerDependencies, JiraSyncRunner
from api.jira.sync import SyncCounts, SyncResult
from db import jira as jira_db
from db import members as members_db
from db.jira import JiraTargetView
from db.models import JiraIssue
from jira.reasons import Reason
from jira_db_fakes import FakeJiraDb
from jira_fake_support import FakeJiraGateway
from specflow_api_agent_support import ALPHA, FOREIGN, UNKNOWN_PROJECT, agent_env, log_haystack, mcp_token, serve_agent
from specflow_api_support import token as web_token

SITE = "https://example.atlassian.net"
ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
OK = SyncResult("succeeded", None, SyncCounts(created=2, updated=1))
SERVICE_EMAIL, TEST_TOKEN = "service@example.com", "test-only-not-a-token"
SRC = Path(__file__).resolve().parent.parent / "src"


class StubSync:
    """``sync_once`` stand-in: records the projects and waits on ``gate`` when one is set."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, project_id: str, _deps: Any) -> SyncResult:
        self.calls.append(project_id)
        if self.gate is not None:
            await self.gate.wait()
        return OK


def target_view(project_id: uuid.UUID, **over: Any) -> JiraTargetView:
    values: dict[str, Any] = {
        "project_id": project_id,
        "project_name": "Alpha",
        "jira_project": "ABC",
        "epic_key": None,
        "enabled": True,
        "updated_at": T0,
        "last_status": None,
        "last_trigger": None,
        "last_started_at": None,
        "last_finished_at": None,
        "last_error_code": None,
        "last_counts": None,
    }
    return JiraTargetView(**(values | over))


def issue(project_id: uuid.UUID, node_id: str, kind: str, key: str | None, **over: Any) -> JiraIssue:
    return JiraIssue(project_id=project_id, node_id=node_id, kind=kind, jira_key=key, requested_at=T0, **over)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    """The app with Jira on: a runner over the fake Jira, ``runtime.transaction`` counted (``rig.depth``)."""

    atlassian = {"ATLASSIAN_SITE_URL": SITE, "ATLASSIAN_EMAIL": SERVICE_EMAIL, "ATLASSIAN_API_TOKEN": TEST_TOKEN}
    env = agent_env(valid_env, **atlassian)
    async with serve_agent(monkeypatch, env) as served:
        db, gateway, stub = FakeJiraDb(), FakeJiraGateway(), StubSync()
        db.install(monkeypatch)
        runtime, repo = served.runtime, served.repo
        depth = SimpleNamespace(open=0)
        real = runtime.transaction

        @asynccontextmanager
        async def counted():
            depth.open += 1
            try:
                async with real() as session:
                    yield session
            finally:
                depth.open -= 1

        runtime.transaction = counted
        gateway.on_call = lambda method: _no_transaction(depth, method)
        runner = JiraSyncRunner(
            gateway,
            transaction=runtime.transaction,
            store=None,
            public_url=None,
            site_url=SITE,
            dependencies=JiraRunnerDependencies(sync_once=stub),
        )
        runtime.jira = runner
        ben = await repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name=None))
        alpha = uuid.UUID(ALPHA)
        repo.members.add((alpha, ben.id))
        repo.roles[(alpha, ben.id)] = "collaborator"
        await repo.upsert_user(None, SimpleNamespace(subject=f"user:{CAROL}", email=CAROL, name=None))
        yield SimpleNamespace(
            http=served.client,
            app=served.app,
            runtime=runtime,
            db=db,
            gateway=gateway,
            runner=runner,
            stub=stub,
            alpha=alpha,
            depth=depth,
            repo=repo,
            users=served.users,
            ben=ben,
        )
        await runner.aclose()


def _no_transaction(depth: Any, method: str) -> None:
    assert depth.open == 0, f"a transaction is open during the Jira call {method}"


async def web(rig: Any, method: str, path: str, who: str = ALICE, body: Any = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {web_token(who)}"}
    return await rig.http.request(method, f"/api/v1/projects/{path}", json=body, headers=headers)


async def agent(rig: Any, method: str, path: str, who: str = ALICE, project: str = ALPHA, **params: Any) -> Any:
    headers = {"Authorization": f"Bearer {mcp_token(who)}"}
    query = {"project_id": project, **params}
    body = {} if method == "POST" else None
    return await rig.http.request(method, f"/api/v1/agent/{path}", params=query, json=body, headers=headers)


def error_of(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def settle(rig: Any) -> None:
    tasks = [p.task for p in rig.runner._projects.values() if p.task is not None]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[Any]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


# --- GET (FR-JIRA-CFG-07) -------------------------------------------------------------------------------------------


async def test_get_without_a_target_is_off_with_no_sync_and_the_standing_of_the_caller(rig):
    owner = await web(rig, "GET", f"{ALPHA}/jira")
    collaborator = await web(rig, "GET", f"{ALPHA}/jira", BEN)

    assert owner.status_code == collaborator.status_code == 200
    assert owner.json() == {
        "available": True,
        "editable": True,
        "site_url": SITE,
        "jira_project": None,
        "epic_key": None,
        "created_epic_key": None,
        "enabled": False,
        "sync": None,
    }
    assert collaborator.json()["editable"] is False and collaborator.json()["available"] is True


async def test_get_with_a_target_shows_it_with_the_epic_specflow_created_and_the_runner_status(rig):
    pid = rig.alpha
    rig.db.targets[pid] = target_view(pid, last_status="succeeded", last_trigger="mcp", last_finished_at=T0)
    rig.db.issues[pid, "EPIC"] = issue(pid, "EPIC", "epic", "ABC-7")

    answer = (await web(rig, "GET", f"{ALPHA}/jira", BEN)).json()

    assert (answer["jira_project"], answer["epic_key"], answer["created_epic_key"]) == ("ABC", None, "ABC-7")
    assert answer["enabled"] is True and answer["editable"] is False
    assert answer["sync"]["status"] == "succeeded" and answer["sync"]["epic_key"] == "ABC-7"


async def test_get_without_a_runner_is_not_available_and_reads_a_stored_running_pass_as_interrupted(rig):
    """FR-JIRA-CFG-07: no ``ATLASSIAN_*`` (no runner): 200 with ``available`` false, ``site_url`` null; ``sync`` is the
    stored target alone, a stored ``running`` row with no live pass reads ``failed`` / ``interrupted``."""

    rig.runtime.jira = None
    pid = rig.alpha
    none_yet = (await web(rig, "GET", f"{ALPHA}/jira")).json()
    rig.db.targets[pid] = target_view(pid, last_status="running", last_trigger="mcp", last_started_at=T0)

    answer = (await web(rig, "GET", f"{ALPHA}/jira")).json()

    assert none_yet["available"] is False and none_yet["site_url"] is None and none_yet["sync"] is None
    assert answer["available"] is False and answer["site_url"] is None and answer["jira_project"] == "ABC"
    sync = answer["sync"]
    assert (sync["status"], sync["error_code"], sync["queued"]) == ("failed", "interrupted", False)


async def test_get_answers_the_one_404_for_a_non_member_a_missing_project_and_a_malformed_id(rig):
    answers = [
        await web(rig, "GET", f"{project}/jira", who)
        for project in (FOREIGN, UNKNOWN_PROJECT, "not-a-uuid")
        for who in (ALICE, CAROL)
    ]

    assert {error_of(a) for a in answers} == {(404, "project_not_found")}
    assert len({a.content for a in answers}) == 1


# --- PUT (FR-JIRA-CFG-01..06) ---------------------------------------------------------------------------------------


async def test_put_saves_the_target_normalised_and_starts_a_pass_without_waiting_for_it(rig, caplog):
    """FR-JIRA-CFG-02, -04, -05: ``' abc '`` is ``ABC``, a blank Epic is null; the new enabled target starts a pass
    that is still running (held) when the answer comes: ``PUT`` never waits for a pass."""

    caplog.set_level("INFO")
    rig.stub.gate = asyncio.Event()

    answer = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": " abc ", "epic_key": "  ", "enabled": True})

    body = answer.json()
    assert answer.status_code == 200
    assert (body["jira_project"], body["epic_key"], body["enabled"], body["editable"]) == ("ABC", None, True, True)
    assert body["sync"]["status"] == "running"
    assert rig.db.targets[rig.alpha].jira_project == "ABC"
    assert [r.trigger for r in events(caplog, "api.jira.sync.queued")] == ["settings"]
    rig.stub.gate.set()
    await settle(rig)
    after = (await web(rig, "GET", f"{ALPHA}/jira")).json()["sync"]
    assert after["status"] == "succeeded" and rig.stub.calls == [ALPHA]


async def test_get_right_after_put_shows_the_live_pass_with_its_trigger_and_reads_the_target_once(rig):
    """The pass of the ``PUT`` is queued but has stored nothing yet: the live ``GET`` still names its trigger and time,
    and answers from one read of the target (one snapshot)."""

    rig.stub.gate = asyncio.Event()
    await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": "ABC", "epic_key": None, "enabled": True})
    rig.db.calls.clear()

    sync = (await web(rig, "GET", f"{ALPHA}/jira", BEN)).json()["sync"]

    assert (sync["status"], sync["trigger"]) == ("running", "settings") and sync["started_at"] is not None
    assert [name for name, _ in rig.db.calls].count("get_target") == 1
    rig.stub.gate.set()
    await settle(rig)


async def test_put_with_an_epic_of_the_project_normalises_it_and_a_disabled_target_starts_nothing(rig):
    body = {"jira_project": "abc", "epic_key": " abc-12 ", "enabled": False}

    saved = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, body)

    out = saved.json()
    assert saved.status_code == 200
    assert (out["epic_key"], out["enabled"], out["sync"]["status"]) == ("ABC-12", False, "idle")
    assert rig.stub.calls == []


async def test_put_that_changes_nothing_starts_no_pass(rig):
    body = {"jira_project": "ABC", "epic_key": None, "enabled": True}
    await web(rig, "PUT", f"{ALPHA}/jira", ALICE, body)
    await settle(rig)
    rig.stub.calls.clear()

    again = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, body)

    assert again.status_code == 200
    assert rig.stub.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"jira_project": "A", "epic_key": None, "enabled": True},
        {"jira_project": "1ABC", "epic_key": None, "enabled": True},
        {"jira_project": "ABCDEFGHIJK", "epic_key": None, "enabled": True},
        {"jira_project": "ABC", "epic_key": "XYZ-1", "enabled": True},
        {"jira_project": "ABC", "epic_key": "ABC-0", "enabled": True},
        {"jira_project": "ABC", "epic_key": "ABC", "enabled": True},
        {"jira_project": "ABC", "epic_key": None, "enabled": "yes"},
        {"jira_project": "ABC", "epic_key": None},
        {"jira_project": "ABC", "epic_key": None, "enabled": True, "extra": 1},
    ],
)
async def test_put_refuses_a_value_the_rules_forbid_with_422_and_saves_nothing(rig, body):
    answer = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, body)

    assert error_of(answer) == (422, "validation_failed")
    assert rig.alpha not in rig.db.targets and rig.stub.calls == []


async def test_put_is_owner_only_a_collaborator_is_403_a_non_member_404(rig):
    body = {"jira_project": "ABC", "epic_key": None, "enabled": True}

    collaborator = await web(rig, "PUT", f"{ALPHA}/jira", BEN, body)
    outsider = await web(rig, "PUT", f"{ALPHA}/jira", CAROL, body)

    assert error_of(collaborator) == (403, "owner_required")
    assert error_of(outsider) == (404, "project_not_found")
    assert rig.db.targets == {} and rig.stub.calls == []


async def test_put_without_a_jira_account_is_503_jira_unavailable_and_saves_nothing(rig):
    rig.runtime.jira = None

    answer = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": "ABC", "epic_key": None, "enabled": True})

    assert error_of(answer) == (503, "jira_unavailable")
    assert answer.json()["error"]["message"] == "Jira sync is not configured on this server."
    assert rig.db.targets == {}


async def test_put_that_changes_the_jira_project_drops_the_map_and_logs_ids_only(rig, caplog):
    caplog.set_level("INFO")
    pid = rig.alpha
    rig.db.targets[pid] = target_view(pid)
    rig.db.issues[pid, "EPIC"] = issue(pid, "EPIC", "epic", "ABC-1")

    answer = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": "NEW", "epic_key": None, "enabled": False})

    assert answer.status_code == 200 and rig.db.issues == {}
    (saved,) = events(caplog, "api.jira.settings.saved")
    assert (saved.enabled, saved.project_changed, saved.project_id) == (False, True, ALPHA)
    assert saved.user_id == str(rig.users["alice"].id)
    assert "NEW" not in log_haystack(caplog).replace("NEW_", "")  # no key value in any record


async def test_put_locks_the_target_first_never_projects_and_commits_before_it_tells_the_runner(rig, monkeypatch):
    """03-handoff "For 05": a pass holds the target ``FOR SHARE`` then needs ``projects`` ``FOR KEY SHARE``, so ``PUT``
    must lock the target first (``save_target`` is ``FOR UPDATE``) and take no ``projects`` lock at all. Proved by the
    order of the calls, the open transaction and the router's source."""

    order: list[tuple[str, int]] = []

    def spy(name: str, real: Any) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            order.append((name, rig.depth.open))
            return await real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(jira_db, "save_target", spy("save_target", jira_db.save_target))
    monkeypatch.setattr(members_db, "lock_project", spy("lock_project", members_db.lock_project))
    after = rig.runner.after_settings

    async def told(project_id: str, saved: Any) -> None:
        order.append(("after_settings", rig.depth.open))
        await after(project_id, saved)

    monkeypatch.setattr(rig.runner, "after_settings", told)

    answer = await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": "ABC", "epic_key": None, "enabled": True})
    await settle(rig)

    assert answer.status_code == 200
    assert order == [("save_target", 1), ("after_settings", 0)]  # inside one transaction; the runner after the commit
    source = (SRC / "api/routers/jira.py").read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert "lock_project" not in names and "save_target" in names


# --- access check ---------------------------------------------------------------------------------------------------


async def check(rig: Any, who: str = ALICE, **over: Any) -> httpx.Response:
    body = {"jira_project": "ABC", "epic_key": None, **over}
    return await web(rig, "POST", f"{ALPHA}/jira/access-check", who, body)


def seed_epic(rig: Any, key: str = "ABC-5", kind: str = "Epic", project: str = "ABC") -> None:
    fields = {"summary": "Owner epic", "issuetype": {"name": kind}, "project": {"key": project}}
    rig.gateway.add_issue(fields, key=key)


async def test_access_check_ok_names_the_project_and_the_epic_summary_and_calls_jira_outside_any_transaction(rig):
    rig.gateway.projects["ABC"] = rig.gateway.projects["PRJ"].__class__("Alpha Shop", "1", "2", "3")
    seed_epic(rig)

    without = await check(rig)
    with_epic = await check(rig, epic_key=" abc-5 ")

    assert without.json() == {"result": "ok", "project_name": "Alpha Shop", "epic_summary": None}
    assert with_epic.json() == {"result": "ok", "project_name": "Alpha Shop", "epic_summary": "Owner epic"}
    assert [m for m, _ in rig.gateway.calls][:3] == ["myself", "project", "missing_permissions"]  # the order of the doc
    assert rig.depth.open == 0


def _nothing(rig: Any) -> None:
    """The default Jira has the project and no Epic."""


def _types_missing(rig: Any) -> None:
    rig.gateway.projects["ABC"] = rig.gateway.projects["PRJ"].__class__("Alpha", "1", None, "3")


@pytest.mark.parametrize(
    ("arrange", "over", "reason", "field"),
    [
        (lambda rig: rig.gateway.fail("myself", Reason.AUTH_FAILED, status=401), {}, "jira_auth_failed", None),
        (_nothing, {"jira_project": "NOPE"}, "jira_not_found", "jira_project"),
        (lambda rig: rig.gateway.fail("project", Reason.FORBIDDEN, status=403), {}, "jira_forbidden", "jira_project"),
        (_types_missing, {}, "jira_issue_types_missing", "jira_project"),
        (lambda rig: setattr(rig.gateway, "missing", frozenset({"EDIT_ISSUES"})), {}, "jira_forbidden", "jira_project"),
        (_nothing, {"epic_key": "ABC-99"}, "jira_not_found", "epic_key"),
        (lambda rig: seed_epic(rig, kind="Story"), {"epic_key": "ABC-5"}, "jira_epic_invalid", "epic_key"),
        (lambda rig: seed_epic(rig, project="OTH"), {"epic_key": "ABC-5"}, "jira_epic_invalid", "epic_key"),
        (lambda rig: rig.gateway.fail("project", Reason.UNREACHABLE, status=503), {}, "jira_unreachable", None),
        (lambda rig: rig.gateway.fail("myself", Reason.INVALID_FIELD, status=400), {}, "jira_unexpected", None),
        (lambda rig: rig.gateway.fail("myself", Reason.UNEXPECTED, status=418), {}, "jira_unexpected", None),
    ],
)
async def test_access_check_answers_the_first_failure_with_its_reason_and_the_field_to_fix(
    rig, arrange, over, reason, field
):
    """FR-JIRA-CFG-03 / api.md "Access check": 200 ``JiraAccessFailed`` (a failed check is an answer, not an error)."""

    rig.gateway.projects["ABC"] = rig.gateway.projects["PRJ"]
    arrange(rig)

    answer = await check(rig, **over)

    assert answer.status_code == 200
    assert answer.json() == {"result": "failed", "reason": reason, "field": field}


async def test_access_check_stops_at_the_first_failure(rig):
    rig.gateway.fail("myself", Reason.AUTH_FAILED, status=401)

    await check(rig, epic_key="ABC-5")

    assert [m for m, _ in rig.gateway.calls] == ["myself"]


async def test_access_check_is_owner_only_needs_jira_and_checks_the_body_like_put(rig, caplog):
    caplog.set_level("INFO")
    collaborator = await check(rig, BEN)
    outsider = await check(rig, CAROL)
    invalid = await check(rig, jira_project="a")
    rig.gateway.projects["ABC"] = rig.gateway.projects["PRJ"]
    ok = await check(rig)
    rig.runtime.jira = None
    unavailable = await check(rig)

    assert error_of(collaborator) == (403, "owner_required")
    assert error_of(outsider) == (404, "project_not_found")
    assert error_of(invalid) == (422, "validation_failed")
    assert ok.json()["result"] == "ok"
    assert error_of(unavailable) == (503, "jira_unavailable")
    (logged,) = events(caplog, "api.jira.access.checked")
    assert (logged.result, logged.reason, logged.project_id) == ("ok", None, ALPHA)
    assert not rig.gateway.closed  # the check uses the runner's shared gateway and leaves it open


# --- agent routes (FR-JIRA-MCP-01..03) ------------------------------------------------------------------------------


async def test_sync_to_jira_starts_a_pass_for_any_member_and_a_repeat_merges(rig, caplog):
    """FR-JIRA-MCP-01: a collaborator may start a sync; ``running`` at once; a repeat while it runs is ``queued``.
    ``user_id`` is in the runner's log only, never in the answer."""

    caplog.set_level("INFO")
    rig.db.targets[rig.alpha] = target_view(rig.alpha)
    rig.stub.gate = asyncio.Event()

    first = await agent(rig, "POST", "jira-sync", BEN)
    second = await agent(rig, "POST", "jira-sync", ALICE)
    rig.stub.gate.set()
    await settle(rig)

    assert first.status_code == second.status_code == 200
    assert (first.json()["status"], first.json()["queued"], first.json()["trigger"]) == ("running", False, "mcp")
    assert (second.json()["status"], second.json()["queued"]) == ("running", True)
    assert "user_id" not in first.json() and str(rig.ben.id) not in first.text
    assert [r.user_id for r in events(caplog, "api.jira.sync.queued")] == [str(rig.ben.id)]
    assert [r.user_id for r in events(caplog, "api.jira.sync.merged")] == [str(rig.users["alice"].id)]
    assert len(rig.stub.calls) == 2  # the merged start made one more pass


async def test_sync_to_jira_without_an_enabled_target_is_409_jira_not_configured_and_starts_nothing(rig):
    missing = await agent(rig, "POST", "jira-sync")
    rig.db.targets[rig.alpha] = target_view(rig.alpha, enabled=False)
    disabled = await agent(rig, "POST", "jira-sync")

    assert error_of(missing) == error_of(disabled) == (409, "jira_not_configured")
    assert missing.json()["error"]["message"] == "The project has no enabled Jira target."
    assert rig.stub.calls == [] and rig.runner._projects == {}


async def test_sync_to_jira_without_a_jira_account_is_503_and_a_non_member_is_404_first(rig):
    outsider = await agent(rig, "POST", "jira-sync", CAROL)
    foreign = await agent(rig, "POST", "jira-sync", project=FOREIGN)
    malformed = await agent(rig, "POST", "jira-sync", project="not-a-uuid")
    rig.runtime.jira = None
    unavailable = await agent(rig, "POST", "jira-sync")
    unavailable_for_foreign = await agent(rig, "POST", "jira-sync", project=FOREIGN)

    denied = (outsider, foreign, malformed, unavailable_for_foreign)
    assert {error_of(a) for a in denied} == {(404, "project_not_found")}
    assert error_of(unavailable) == (503, "jira_unavailable")


async def test_sync_to_jira_while_the_runner_is_closing_is_503_jira_unavailable_not_500(rig):
    rig.db.targets[rig.alpha] = target_view(rig.alpha)
    await rig.runner.aclose()
    closing = await agent(rig, "POST", "jira-sync")

    assert error_of(closing) == (503, "jira_unavailable")
    assert closing.json()["error"]["message"] == "Jira sync is shutting down."
    assert rig.stub.calls == []


async def seed_items(rig: Any) -> None:
    pid = rig.alpha
    rig.db.targets[pid] = target_view(
        pid,
        last_status="succeeded",
        last_trigger="plan_run",
        last_started_at=T0,
        last_finished_at=T0,
        last_counts={"created": 6},
    )
    rows = [("EPIC", "epic")] + [(f"STORY::{n}", "story") for n in "AB"] + [(f"TASK::{n}", "subtask") for n in "ABC"]
    for number, (node_id, kind) in enumerate(rows, 1):
        rig.db.issues[pid, node_id] = issue(pid, node_id, kind, f"ABC-{number}")


async def test_jira_sync_status_reports_the_run_and_pages_the_items_by_kind_then_node_id(rig):
    """FR-JIRA-MCP-02: ``limit`` 4 of 6 items: the epic, the stories, then the first sub-task; the cursor continues."""

    await seed_items(rig)

    first = (await agent(rig, "GET", "jira-sync-status", BEN, limit=4)).json()
    second = (await agent(rig, "GET", "jira-sync-status", BEN, limit=4, cursor=first["next_cursor"])).json()

    assert [i["node_id"] for i in first["items"]] == ["EPIC", "STORY::A", "STORY::B", "TASK::A"]
    assert first["next_cursor"] is not None
    assert [i["node_id"] for i in second["items"]] == ["TASK::B", "TASK::C"] and second["next_cursor"] is None
    assert (first["status"], first["counts"]["created"], first["jira_project"]) == ("succeeded", 6, "ABC")
    assert first["items"][0] == {
        "node_id": "EPIC",
        "kind": "epic",
        "jira_key": "ABC-1",
        "url": f"{SITE}/browse/ABC-1",
        "removed": False,
        "error_code": None,
    }
    default = (await agent(rig, "GET", "jira-sync-status")).json()
    assert len(default["items"]) == 6 and default["next_cursor"] is None


async def test_jira_sync_status_continues_after_an_item_that_is_gone(rig):
    await seed_items(rig)
    cursor = (await agent(rig, "GET", "jira-sync-status", limit=2)).json()["next_cursor"]
    del rig.db.issues[rig.alpha, "STORY::A"]

    page = (await agent(rig, "GET", "jira-sync-status", limit=2, cursor=cursor)).json()

    assert [i["node_id"] for i in page["items"]] == ["STORY::B", "TASK::A"]


@pytest.mark.parametrize(
    "cursor",
    ["not-a-cursor", "e30", "eyJ2IjoxLCJhZnRlciI6IkZFQVQ6OkYxIn0", "x" * 513, "e30=" * 3],
)
async def test_jira_sync_status_refuses_a_forged_cursor_with_400_invalid_cursor(rig, cursor):
    await seed_items(rig)

    answer = await agent(rig, "GET", "jira-sync-status", cursor=cursor)

    assert error_of(answer) == (400, "invalid_cursor")


async def test_jira_sync_status_forges_no_cursor_from_another_listing(rig):
    """A feature cursor (``{"v":1,"after":"FEAT::F1"}``) has no ``<kind order>:<node id>`` position."""

    await seed_items(rig)
    raw = json.dumps({"v": 1, "after": "9:EPIC"}, separators=(",", ":")).encode()
    out_of_range = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    assert error_of(await agent(rig, "GET", "jira-sync-status", cursor=out_of_range)) == (400, "invalid_cursor")


async def test_jira_sync_status_shows_a_disabled_target_but_a_missing_one_is_409(rig):
    missing = await agent(rig, "GET", "jira-sync-status")
    rig.db.targets[rig.alpha] = target_view(
        rig.alpha, enabled=False, last_status="failed", last_error_code="jira_auth_failed"
    )
    disabled = await agent(rig, "GET", "jira-sync-status")

    assert error_of(missing) == (409, "jira_not_configured")
    assert disabled.status_code == 200
    shown = disabled.json()
    assert (shown["enabled"], shown["status"], shown["error_code"]) == (False, "failed", "jira_auth_failed")


async def test_jira_sync_status_without_a_jira_account_is_503_and_for_a_stranger_404(rig):
    stranger = await agent(rig, "GET", "jira-sync-status", CAROL)
    rig.runtime.jira = None
    unavailable = await agent(rig, "GET", "jira-sync-status")

    assert error_of(stranger) == (404, "project_not_found")
    assert error_of(unavailable) == (503, "jira_unavailable")


async def test_the_jira_routes_log_no_text_of_the_plan_or_of_jira(rig, caplog):
    caplog.set_level("INFO")
    await seed_items(rig)
    await web(rig, "PUT", f"{ALPHA}/jira", ALICE, {"jira_project": "ABC", "epic_key": None, "enabled": True})
    await agent(rig, "POST", "jira-sync", BEN)
    await settle(rig)

    haystack = log_haystack(caplog)

    assert TEST_TOKEN not in haystack and SERVICE_EMAIL not in haystack
