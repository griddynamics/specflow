"""Tier 1: the Jira sync runner (FR-JIRA-RUN-01..09, CFG-06/07/08 start side, NFR-JIRA-02) and its runtime wiring.

``sync_once`` is a stub (or the real engine over the fakes of the engine tests); the repository is ``FakeJiraDb``.
No Jira, no database, no API process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest

from api import runtime as runtime_module
from api.jira.connections import AtlassianConnections
from api.jira.runner import JiraRunnerClosedError, JiraRunnerDependencies, JiraSyncRunner
from api.jira.state import items_of, status_of
from api.jira.sync import SyncCounts, SyncResult, sync_once
from atlassian_support import oauth_env
from db.jira import TargetSaved
from db.models import JiraIssue
from jira.http import HttpJiraGateway
from jira_sync_support import T0, Harness
from logger_harness import events, fields
from settings import load_settings
from test_specflow_api_rfix import stub_runtime_services

SITE = "https://example.atlassian.net"
OK = SyncResult("succeeded", None, SyncCounts(created=2, updated=1))
LOGGED = {"event", "project_id", "trigger", "user_id", "status", "error_code", "error_type", "counts"}


class StubSync:
    """Stands in for ``sync_once``: records the projects, tracks the passes in flight and waits on queued gates."""

    def __init__(self, result: SyncResult = OK) -> None:
        self.result = result
        self.calls: list[str] = []
        self.gates: deque[asyncio.Event] = deque()
        self.entered: asyncio.Queue[str] = asyncio.Queue()
        self.active = 0
        self.most = 0
        self.fail: Exception | None = None

    async def __call__(self, project_id: str, _deps: Any) -> SyncResult:
        self.calls.append(project_id)
        self.active += 1
        self.most = max(self.most, self.active)
        self.entered.put_nowait(project_id)
        try:
            if self.gates:
                await self.gates.popleft().wait()
            if self.fail is not None:
                raise self.fail
            return self.result
        finally:
            self.active -= 1


def make_runner(
    harness: Harness, sync: Callable[..., Awaitable[SyncResult]] | None = None, **plan: Any
) -> JiraSyncRunner:
    deps = JiraRunnerDependencies(
        sync_once=sync or StubSync(),
        load_plan=harness._load_plan,
        spec_names=harness._spec_names,
        now=harness._now,
        **plan,
    )
    return JiraSyncRunner(
        harness.gateway,
        transaction=harness.db.transaction,
        store=None,
        public_url=None,
        site_url=SITE,
        dependencies=deps,
    )


async def settle(runner: JiraSyncRunner) -> None:
    tasks = [project.task for project in runner._projects.values() if project.task is not None]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def stored(harness: Harness, key: str) -> Any:
    return getattr(harness.db.targets[harness.project_id], key)


# Starts, merging, the process-wide slot ------------------------------------------------


async def test_a_start_runs_one_pass_and_stores_the_start_and_the_end(monkeypatch, caplog):
    """
    Scenario: the agent route starts a pass, the pass ends ``succeeded``.

    Agent -> Runner: start(mcp) -> status running; Runner -> Db: record_start; Runner -> Engine: sync_once;
    Runner -> Db: record_end; Runner -> Log: queued, started, finished.
    """
    caplog.set_level("INFO")
    harness = Harness(monkeypatch)
    sync = StubSync()
    runner = make_runner(harness, sync)

    answer = await runner.start(str(harness.project_id), "mcp", user_id="user-1")
    during = await runner.status(str(harness.project_id))
    await settle(runner)
    after = await runner.status(str(harness.project_id))

    assert (answer.status, answer.queued, answer.trigger, answer.enabled) == ("running", False, "mcp", True)
    assert answer.started_at is not None and answer.jira_project == "PRJ"
    assert during is not None and (during.status, during.queued) == ("running", False)
    assert during.trigger == "mcp" and during.started_at == answer.started_at  # live: the pass's own, not the row's
    assert sync.calls == [str(harness.project_id)]
    assert after is not None and after.status == "succeeded" and after.trigger == "mcp"
    assert after.counts is not None and (after.counts.created, after.counts.updated) == (2, 1)
    assert after.finished_at is not None and after.error_code is None
    queued, started, finished = (
        events(caplog, f"api.jira.sync.{name}")[0] for name in ("queued", "started", "finished")
    )
    assert fields(queued)["user_id"] == "user-1" and fields(queued)["trigger"] == "mcp"
    assert fields(finished)["status"] == "succeeded" and fields(finished)["counts"]["created"] == 2
    assert fields(started)["project_id"] == str(harness.project_id)
    sync_records = [record for record in caplog.records if getattr(record, "event", "").startswith("api.jira.sync.")]
    assert sync_records and all(set(fields(record)) <= LOGGED for record in sync_records)


async def test_three_starts_during_one_pass_make_exactly_two_passes(monkeypatch, caplog):
    """FR-JIRA-RUN-02: the starts during a pass set a flag; when the pass ends exactly one more runs."""
    caplog.set_level("INFO")
    harness = Harness(monkeypatch)
    sync = StubSync()
    first = asyncio.Event()
    sync.gates.append(first)
    runner = make_runner(harness, sync)
    pid = str(harness.project_id)

    await runner.start(pid, "plan_run")
    await sync.entered.get()
    second = await runner.start(pid, "mcp", user_id="user-2")
    third = await runner.start(pid, "settings")
    during = await runner.status(pid)
    first.set()
    await settle(runner)

    assert [(a.status, a.queued) for a in (second, third)] == [("running", True), ("running", True)]
    assert during is not None and during.queued
    assert [a.trigger for a in (second, third)] == ["plan_run", "plan_run"]
    assert during.trigger == "plan_run"  # the current pass's trigger, not the merged ``settings``
    assert len(sync.calls) == 2
    assert [fields(r)["trigger"] for r in events(caplog, "api.jira.sync.merged")] == ["mcp", "settings"]
    assert stored(harness, "last_trigger") == "settings"  # the second pass was recorded with the latest start
    assert stored(harness, "last_status") == "succeeded"
    assert (await runner.status(pid)).queued is False


async def test_a_start_after_the_runs_ended_makes_a_new_pass(monkeypatch):
    harness = Harness(monkeypatch)
    sync = StubSync()
    runner = make_runner(harness, sync)

    await runner.start(str(harness.project_id), "mcp")
    await settle(runner)
    await runner.start(str(harness.project_id), "mcp")
    await settle(runner)

    assert len(sync.calls) == 2


async def test_one_pass_runs_at_a_time_and_a_waiting_one_is_already_stored_running(monkeypatch):
    """
    Scenario: project A's pass holds the slot; project B starts and waits.

    B has recorded ``running`` (and its trigger) before it waits, so a restart would recover it (FR-JIRA-RUN-03).
    """
    first = Harness(monkeypatch)
    second = Harness(monkeypatch, share=first)
    sync = StubSync()
    gate = asyncio.Event()
    sync.gates.append(gate)
    runner = make_runner(first, sync)

    await runner.start(str(first.project_id), "plan_run")
    await sync.entered.get()
    answer = await runner.start(str(second.project_id), "mcp")
    before_stored = await runner.status(str(second.project_id))  # the pass has stored nothing yet
    for _ in range(5):
        await asyncio.sleep(0)

    assert answer.status == "running"
    assert before_stored is not None and before_stored.trigger == "mcp"
    assert stored(second, "last_status") == "running" and stored(second, "last_trigger") == "mcp"
    assert sync.calls == [str(first.project_id)] and sync.active == 1
    gate.set()
    await settle(runner)

    assert sync.calls == [str(first.project_id), str(second.project_id)] and sync.most == 1
    assert stored(second, "last_status") == "succeeded"


async def test_a_pass_that_fails_in_the_runner_is_logged_and_does_not_stop_the_next_start(monkeypatch, caplog):
    caplog.set_level("INFO")
    harness = Harness(monkeypatch)
    sync = StubSync()
    sync.fail = RuntimeError("boom")
    runner = make_runner(harness, sync)

    await runner.start(str(harness.project_id), "mcp")
    await settle(runner)
    sync.fail = None
    await runner.start(str(harness.project_id), "mcp")
    await settle(runner)

    failed = events(caplog, "api.jira.sync.failed")
    assert [fields(r)["error_type"] for r in failed] == ["RuntimeError"]
    assert "boom" not in caplog.text
    assert stored(harness, "last_status") == "succeeded"


async def test_a_start_without_an_enabled_target_queues_nothing(monkeypatch):
    harness = Harness(monkeypatch)
    sync = StubSync()
    runner = make_runner(harness, sync)
    harness.db.set_target(harness.project_id, enabled=False)

    disabled = await runner.start(str(harness.project_id), "mcp")
    gone = await runner.start(str(uuid.uuid4()), "mcp")

    assert [(a.status, a.error_code, a.queued, a.enabled) for a in (disabled, gone)] == [
        ("skipped", "jira_not_configured", False, False)
    ] * 2
    assert disabled.jira_project == "PRJ" and gone.jira_project == ""
    assert sync.calls == [] and runner._projects == {}


async def test_a_start_after_close_is_refused(monkeypatch):
    harness = Harness(monkeypatch)
    runner = make_runner(harness)

    await runner.aclose()

    with pytest.raises(JiraRunnerClosedError):
        await runner.start(str(harness.project_id), "mcp")


async def test_a_start_whose_read_is_overtaken_by_close_spawns_no_pass(monkeypatch):
    harness = Harness(monkeypatch)
    sync = StubSync()
    runner = make_runner(harness, sync)
    held, release = asyncio.Event(), asyncio.Event()
    real = runner._read

    async def slow(project_id: str) -> Any:
        result = await real(project_id)
        held.set()
        await release.wait()
        return result

    monkeypatch.setattr(runner, "_read", slow)
    starting = asyncio.create_task(runner.start(str(harness.project_id), "mcp"))
    await held.wait()
    await runner.aclose()
    release.set()

    with pytest.raises(JiraRunnerClosedError):
        await starting
    assert runner._projects == {} and sync.calls == []


async def test_close_cancels_a_pass_that_waits_for_the_slot_and_leaves_it_stored_running(monkeypatch):
    first = Harness(monkeypatch)
    second = Harness(monkeypatch, share=first)
    sync = StubSync()
    sync.gates.append(asyncio.Event())
    runner = make_runner(first, sync)

    await runner.start(str(first.project_id), "plan_run")
    await sync.entered.get()
    await runner.start(str(second.project_id), "mcp")
    for _ in range(5):
        await asyncio.sleep(0)
    await runner.aclose()

    assert sync.calls == [str(first.project_id)]  # the waiting pass never ran
    assert all(not project.running for project in runner._projects.values())
    assert stored(second, "last_status") == "running"  # a restart recovers it


async def test_close_cancels_a_running_pass_and_closes_the_gateway(monkeypatch):
    harness = Harness(monkeypatch)
    sync = StubSync()
    sync.gates.append(asyncio.Event())
    closed: list[bool] = []
    harness.gateway.aclose = lambda: _flag(closed)
    runner = make_runner(harness, sync)

    await runner.start(str(harness.project_id), "mcp")
    await sync.entered.get()
    await runner.aclose()

    assert closed == [True] and sync.active == 0
    assert all(not project.running for project in runner._projects.values())


async def _flag(into: list[bool]) -> None:
    into.append(True)


async def test_a_target_change_stops_the_pass_and_the_settings_start_makes_exactly_one_more(monkeypatch):
    """
    Scenario (FR-JIRA-RUN-08, real engine): the owner changes ``epic_key`` while the first Epic create is in flight,
    and the PUT's ``settings`` start merges into the pass it stopped.

    Engine pass 1 -> skipped target_changed; Runner repeats once -> pass 2 reads the new target and succeeds.
    """
    harness = Harness(monkeypatch)
    harness.gateway.add_issue({"summary": "Owner epic", "issuetype": {"name": "Epic"}}, key="PRJ-900")
    results: list[SyncResult] = []

    async def real(project_id: str, deps: Any) -> SyncResult:
        results.append(await sync_once(project_id, deps))
        return results[-1]

    runner = make_runner(harness, real)

    async def change_target() -> None:
        del harness.gateway.holds["create_issue"]  # once
        harness.db.set_target(harness.project_id, epic_key="PRJ-900")
        await runner.after_settings(
            str(harness.project_id), TargetSaved(project_changed=False, turned_on=False, keys_changed=True)
        )

    harness.gateway.holds["create_issue"] = change_target
    await runner.start(str(harness.project_id), "plan_run")
    await settle(runner)

    assert [(r.status, r.error_code) for r in results] == [("skipped", "target_changed"), ("succeeded", None)]
    assert stored(harness, "last_trigger") == "settings" and stored(harness, "last_status") == "succeeded"


# Plan hook and settings hook ------------------------------------------------------------


async def test_the_plan_hook_starts_a_pass_only_for_an_enabled_target(monkeypatch):
    harness = Harness(monkeypatch)
    off = Harness(monkeypatch, share=harness)
    off.db.set_target(off.project_id, enabled=False)
    sync = StubSync()
    runner = make_runner(harness, sync)

    await runner.after_plan(str(off.project_id))
    await runner.after_plan(str(uuid.uuid4()))
    await runner.after_plan(str(harness.project_id))
    await settle(runner)

    assert sync.calls == [str(harness.project_id)] and stored(harness, "last_trigger") == "plan_run"


async def test_the_plan_hook_never_raises_and_logs_a_start_that_failed(monkeypatch, caplog):
    caplog.set_level("INFO")
    harness = Harness(monkeypatch)
    runner = make_runner(harness)

    def broken(name: str) -> None:
        raise ConnectionError("password=hunter2")

    harness.db.hook = broken
    await runner.after_plan(str(harness.project_id))

    (logged,) = events(caplog, "api.jira.sync.start_failed")
    assert (fields(logged)["trigger"], fields(logged)["error_type"]) == ("plan_run", "ConnectionError")
    assert fields(logged)["project_id"] == str(harness.project_id)
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize(
    ("saved", "enabled", "starts"),
    [
        (
            TargetSaved(project_changed=False, turned_on=True, keys_changed=True),
            True,
            True,
        ),  # turned on (new or switched on)
        (
            TargetSaved(project_changed=False, turned_on=False, keys_changed=True),
            True,
            True,
        ),  # a key changed on an enabled target
        (TargetSaved(project_changed=True, turned_on=False, keys_changed=True), True, True),  # the Jira project changed
        (
            TargetSaved(project_changed=False, turned_on=False, keys_changed=False),
            True,
            False,
        ),  # nothing relevant changed
        (
            TargetSaved(project_changed=False, turned_on=False, keys_changed=True),
            False,
            False,
        ),  # keys changed but the target stays off
        (TargetSaved(project_changed=False, turned_on=False, keys_changed=False), False, False),  # turned off
    ],
)
async def test_the_settings_hook_starts_a_pass_when_the_target_was_turned_on_or_changed(
    monkeypatch, saved, enabled, starts
):
    harness = Harness(monkeypatch)
    harness.db.set_target(harness.project_id, enabled=enabled)
    sync = StubSync()
    runner = make_runner(harness, sync)

    await runner.after_settings(str(harness.project_id), saved)
    await settle(runner) if starts else None

    assert bool(sync.calls) is starts
    if starts:
        assert stored(harness, "last_trigger") == "settings"


# Recovery -------------------------------------------------------------------------------


async def test_recovery_restarts_the_stale_running_passes_of_enabled_targets(monkeypatch):
    stale = Harness(monkeypatch)
    off = Harness(monkeypatch, share=stale)
    done = Harness(monkeypatch, share=stale)
    for harness in (stale, off):
        harness.db.set_target(harness.project_id, last_status="running", last_trigger="mcp")
    off.db.set_target(off.project_id, enabled=False)
    done.db.set_target(done.project_id, last_status="succeeded")
    sync = StubSync()
    runner = make_runner(stale, sync)

    runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=2)
    await settle(runner)

    assert sync.calls == [str(stale.project_id)]
    assert stored(stale, "last_trigger") == "recovery" and stored(stale, "last_status") == "succeeded"
    assert stored(off, "last_status") == "running"  # a disabled target is never started


async def test_a_recovery_failure_is_logged_with_no_project_and_never_raised(monkeypatch, caplog):
    caplog.set_level("INFO")
    harness = Harness(monkeypatch)
    runner = make_runner(harness)

    def broken(name: str) -> None:
        if name == "running_targets":
            raise OSError("database is down")

    harness.db.hook = broken
    runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=2)

    (logged,) = events(caplog, "api.jira.sync.start_failed")
    assert (fields(logged)["project_id"], fields(logged)["trigger"], fields(logged)["error_type"]) == (
        None,
        "recovery",
        "OSError",
    )
    assert "database is down" not in caplog.text


# Status and items -----------------------------------------------------------------------


async def test_status_reads_the_stored_row_when_no_pass_is_live(monkeypatch):
    harness = Harness(monkeypatch)
    runner = make_runner(harness)
    pid = harness.project_id
    stale = {"last_status": "running", "last_trigger": "mcp", "last_started_at": T0}

    assert (await runner.status(str(pid))).status == "idle"
    harness.db.set_target(pid, **stale)
    interrupted = await runner.status(str(pid))
    harness.db.set_target(pid, enabled=False)
    disabled = await runner.status(str(pid))
    harness.db.set_target(pid, enabled=True, last_status="partial", last_error_code=None, last_counts={"failed": 2})
    partial = await runner.status(str(pid))

    assert interrupted is not None and (interrupted.status, interrupted.error_code) == ("failed", "interrupted")
    assert interrupted.queued is False and interrupted.finished_at is None and interrupted.counts is None
    assert disabled is not None and (disabled.status, disabled.error_code) == ("skipped", "jira_not_configured")
    assert disabled.enabled is False
    assert partial is not None and partial.status == "partial" and partial.counts is not None
    assert partial.counts.failed == 2
    assert await runner.status(str(uuid.uuid4())) is None


async def test_the_epic_key_is_the_configured_one_else_the_created_one(monkeypatch):
    harness = Harness(monkeypatch)
    runner = make_runner(harness)
    pid = harness.project_id
    row = JiraIssue(project_id=pid, node_id="EPIC", kind="epic", jira_key="PRJ-5", requested_at=T0)
    harness.db.issues[(pid, "EPIC")] = row

    created = await runner.status(str(pid))
    harness.db.set_target(pid, epic_key="PRJ-900")
    configured = await runner.status(str(pid))

    assert created is not None and created.epic_key == "PRJ-5"
    assert configured is not None and configured.epic_key == "PRJ-900"


async def test_items_are_ordered_by_kind_then_node_id_and_link_to_the_issue(monkeypatch):
    harness = Harness(monkeypatch)
    runner = make_runner(harness)
    pid = harness.project_id
    rows = [
        JiraIssue(project_id=pid, node_id="TASK::B", kind="subtask", jira_key="PRJ-4", requested_at=T0),
        JiraIssue(project_id=pid, node_id="STORY::Z", kind="story", jira_key=None, requested_at=T0),
        JiraIssue(project_id=pid, node_id="EPIC", kind="epic", jira_key="PRJ-1", requested_at=T0),
        JiraIssue(project_id=pid, node_id="TASK::A", kind="subtask", jira_key="PRJ-3", requested_at=T0, removed_at=T0),
        JiraIssue(project_id=pid, node_id="STORY::A", kind="story", jira_key="PRJ-2", error_code="jira_unexpected"),
    ]
    for row in rows:
        harness.db.issues[(pid, row.node_id)] = row

    items = await runner.items(str(pid))

    assert [item.node_id for item in items] == ["EPIC", "STORY::A", "STORY::Z", "TASK::A", "TASK::B"]
    assert items[1].url == f"{SITE}/browse/PRJ-2" and items[1].error_code == "jira_unexpected"
    assert items[2].url is None and items[3].removed is True and items[4].removed is False
    assert items_of([], SITE) == []


def test_status_of_a_live_pass_reads_running_whatever_the_row_says(monkeypatch):
    harness = Harness(monkeypatch)
    target = replace(harness.db.targets[harness.project_id], last_status="failed", last_error_code="jira_unreachable")

    live = status_of(target, live=True, queued=True)
    idle = status_of(replace(target, last_status=None), live=True, queued=True)

    assert (live.status, live.queued, live.error_code, live.finished_at) == ("running", True, None, None)
    assert (idle.status, idle.queued) == ("running", True)  # a pass queued for a target that never ran
    assert status_of(replace(target, last_status=None), live=False, queued=True).status == "idle"


# Runtime wiring -------------------------------------------------------------------------

JIRA_ENV = {
    "ATLASSIAN_SITE_URL": SITE,
    "ATLASSIAN_EMAIL": "robot@example.com",
    "ATLASSIAN_API_TOKEN": "token-canary-5d1f",
}


async def test_build_runtime_without_atlassian_settings_has_no_jira_runner(monkeypatch, valid_env):
    stub_runtime_services(monkeypatch)

    runtime = await runtime_module.build_runtime(load_settings("api", valid_env))
    await runtime.aclose()

    assert runtime.jira is None and runtime.plans is None  # nothing else changes


async def test_build_runtime_builds_closes_and_wires_the_runner(monkeypatch, valid_env, caplog):
    """With ``ATLASSIAN_*`` the runner exists even without the graph build; with it the plan runner holds it."""
    stub_runtime_services(monkeypatch)
    env = {**valid_env, **JIRA_ENV}

    bare = await runtime_module.build_runtime(load_settings("api", env))
    await asyncio.wait_for(bare.jira._recovery, timeout=2)
    graph = await runtime_module.build_runtime(
        load_settings(
            "api",
            {
                **env,
                "GRAPH_LLM_MODEL": "main/model",
                "GRAPH_SMALL_MODEL": "small/model",
                "GRAPH_EMBEDDING_MODEL": "e/m",
            },
        )
    )
    await asyncio.wait_for(graph.jira._recovery, timeout=2)
    gateway = bare.jira._gateway
    assert bare.jira.gateway is gateway
    await bare.aclose()
    await graph.aclose()

    assert isinstance(bare.jira, JiraSyncRunner) and isinstance(gateway, HttpJiraGateway)
    assert bare.plans is None
    # the plan runner's one hook is the fan-out of the plan destinations; Jira is the first of them
    assert graph.plans._jira is graph.plan_destinations and graph.plan_destinations._destinations == [graph.jira]
    assert bare.jira._closed and gateway._client.is_closed
    assert bare.jira._deps.public_url is None and bare.jira._site_url == SITE


async def test_the_public_url_is_the_one_of_the_mcp_settings_when_they_are_set(monkeypatch, valid_env):
    stub_runtime_services(monkeypatch)
    env = {
        **valid_env,
        **JIRA_ENV,
        "MCP_RESOURCE_URL": "https://mcp.example.com/mcp",
        "PUBLIC_URL": "https://app.example.com",
    }

    runtime = await runtime_module.build_runtime(load_settings("api", env))
    await runtime.aclose()

    assert runtime.jira._deps.public_url == "https://app.example.com"


async def test_a_recovery_database_error_leaves_build_runtime_returning(monkeypatch, valid_env, caplog):
    """The stub engine has no database: the background recovery fails, is logged and the runtime is up."""
    caplog.set_level("INFO")
    stub_runtime_services(monkeypatch)

    runtime = await runtime_module.build_runtime(load_settings("api", {**valid_env, **JIRA_ENV}))
    await asyncio.wait_for(runtime.jira._recovery, timeout=2)
    await runtime.aclose()

    (logged,) = events(caplog, "api.jira.sync.start_failed")
    assert fields(logged)["project_id"] is None and fields(logged)["trigger"] == "recovery"
    assert "token-canary-5d1f" not in caplog.text


async def test_build_runtime_in_oauth_mode_wires_the_connections_logs_the_mode_and_closes_them(
    monkeypatch, valid_env, caplog
):
    """With the OAuth variables (and the token account too) the runner acts through the users' connections."""
    caplog.set_level("INFO")
    stub_runtime_services(monkeypatch)

    runtime = await runtime_module.build_runtime(load_settings("api", {**valid_env, **JIRA_ENV, **oauth_env()}))
    await asyncio.wait_for(runtime.jira._recovery, timeout=2)
    connections = runtime.jira.connections
    client = connections._client._http
    await runtime.aclose()

    assert isinstance(connections, AtlassianConnections) and runtime.jira.gateway is None
    assert runtime.jira._closed and client.is_closed
    (mode,) = events(caplog, "api.jira.mode")
    assert fields(mode)["mode"] == "oauth"
    assert "token-canary-5d1f" not in caplog.text


async def test_build_runtime_in_token_mode_logs_the_mode(monkeypatch, valid_env, caplog):
    caplog.set_level("INFO")
    stub_runtime_services(monkeypatch)

    runtime = await runtime_module.build_runtime(load_settings("api", {**valid_env, **JIRA_ENV}))
    await asyncio.wait_for(runtime.jira._recovery, timeout=2)
    await runtime.aclose()

    (mode,) = events(caplog, "api.jira.mode")
    assert fields(mode)["mode"] == "token" and runtime.jira.connections is None
