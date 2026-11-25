"""The GitHub Projects runner on the lifted ``ExportRunner``: starts, merged starts, the plan hook, recovery and the
closed runner; the pass itself is a stub here (``test_api_github_projects_sync``)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from api.github_projects.gateway import MemberGateway
from api.github_projects.runner import GitHubProjectsClosedError, GitHubProjectsRunner
from api.github_projects.sync import NOT_CONFIGURED, PushCounts, PushDeps, PushResult
from db import github_projects as projects_db
from db import integrations as integrations_db
from db.integrations import IntegrationView
from github.codes import GitHubCode, GitHubError
from github.connections import GitHubConnections
from github_projects_support import target_view
from github_support import FakeGitHub, MemoryStore, github_settings

PID = uuid.uuid4()


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.target = target_view(PID)
        self.enabled = True
        self.started: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str | None, dict[str, int]]] = []
        self.gate: asyncio.Event | None = None
        self.passes = 0
        self.running: list[uuid.UUID] = []
        self.bound: uuid.UUID | None = uuid.uuid4()  # the member the project's GitHub is bound to now
        self.bound_row = True
        monkeypatch.setattr(integrations_db, "get_integration", self.get_integration)
        monkeypatch.setattr(projects_db, "get_target", self.get_target)
        monkeypatch.setattr(projects_db, "record_start", self.record_start)
        monkeypatch.setattr(projects_db, "record_end", self.record_end)
        monkeypatch.setattr(projects_db, "running_targets", self.running_targets)
        connections = GitHubConnections(
            github_settings(), store=MemoryStore().transaction, client=FakeGitHub().client()
        )
        self.runner = GitHubProjectsRunner(
            connections, transaction=self.transaction, graph_store=object(), push=self.push
        )

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield None

    async def get_integration(self, _session: Any, project_id: uuid.UUID, app: str) -> IntegrationView | None:
        if not self.bound_row or app != "github":
            return None
        return IntegrationView(project_id, app, self.bound, None, "acme", "acme", self.target.updated_at)

    async def get_target(self, _session: Any, project_id: uuid.UUID) -> Any:
        return replace(self.target, enabled=self.enabled) if project_id == PID else None

    async def record_start(self, _session: Any, _project_id: uuid.UUID, *, trigger: str, queued_at: Any) -> None:
        self.started.append((trigger, str(queued_at)))

    async def record_end(self, _session: Any, _project_id: uuid.UUID, **values: Any) -> None:
        self.ended.append((values["status"], values["error_code"], values["counts"]))

    async def running_targets(self, _session: Any) -> list[uuid.UUID]:
        return self.running

    async def push(self, _project_id: str, _deps: PushDeps) -> PushResult:
        self.passes += 1
        if self.gate is not None:
            await self.gate.wait()
        return PushResult("succeeded", None, PushCounts(created=2))

    async def settle(self) -> None:
        tasks = [p.task for p in self.runner._projects.values() if p.task is not None]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


async def test_a_start_without_an_enabled_target_starts_nothing(rig: Rig) -> None:
    rig.enabled = False

    status = await rig.runner.start(str(PID), "settings")

    assert (status.status, status.error_code, status.queued) == ("skipped", NOT_CONFIGURED, False)
    assert rig.started == [] and rig.passes == 0


async def test_a_start_runs_one_pass_and_records_its_start_and_end(rig: Rig) -> None:
    status = await rig.runner.start(str(PID), "plan_run")
    await rig.settle()

    assert (status.status, status.queued, status.trigger) == ("running", False, "plan_run")
    assert [trigger for trigger, _ in rig.started] == ["plan_run"]
    [(state, code, counts)] = rig.ended
    assert (state, code, counts["created"]) == ("succeeded", None, 2)
    assert rig.runner.live(str(PID)) == (False, False)


async def test_a_start_while_a_pass_runs_is_merged_into_one_more_pass(rig: Rig) -> None:
    rig.gate = asyncio.Event()
    await rig.runner.start(str(PID), "plan_run")
    await asyncio.sleep(0)

    merged = await rig.runner.start(str(PID), "settings")
    rig.gate.set()
    await rig.settle()

    assert (merged.status, merged.queued, merged.trigger) == ("running", True, "plan_run")
    assert rig.passes == 2 and [trigger for trigger, _ in rig.started] == ["plan_run", "settings"]


async def test_the_plan_hook_starts_a_pass_and_never_raises(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await rig.runner.after_plan(str(PID))
    await rig.settle()
    assert rig.passes == 1

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage down")

    monkeypatch.setattr(projects_db, "get_target", broken)
    await rig.runner.after_plan(str(PID))  # contained: logged, not raised


async def test_recovery_restarts_the_passes_a_stopped_process_left_running(rig: Rig) -> None:
    rig.running = [PID]

    rig.runner.start_recovery()
    recovery = rig.runner._recovery
    assert recovery is not None
    await asyncio.wait_for(recovery, timeout=2)
    await rig.settle()

    assert [trigger for trigger, _ in rig.started] == ["recovery"] and rig.passes == 1


async def test_a_closed_runner_refuses_a_start_with_its_own_error(rig: Rig) -> None:
    await rig.runner.aclose()

    with pytest.raises(GitHubProjectsClosedError, match="the github projects runner is closed"):
        await rig.runner.start(str(PID), "settings")


async def acting_user(rig: Rig) -> uuid.UUID:
    gateway = await rig.runner._gateway_for(PID)
    assert isinstance(gateway, MemberGateway)
    return gateway.user_id


async def test_a_pass_acts_as_the_member_the_projects_github_is_bound_to_now(rig: Rig) -> None:
    first = rig.bound
    assert await acting_user(rig) == first
    rig.bound = uuid.uuid4()  # another member took the binding over

    assert await acting_user(rig) == rig.bound != first


@pytest.mark.parametrize("removed", ["member", "integration"])
async def test_a_removed_member_or_integration_ends_the_pass_with_github_not_connected(rig: Rig, removed: str) -> None:
    if removed == "member":
        rig.bound = None  # the member left: the row stays, its user is null
    else:
        rig.bound_row = False  # the integration was removed

    with pytest.raises(GitHubError) as raised:
        await rig.runner._gateway_for(PID)

    assert raised.value.code is GitHubCode.NOT_CONNECTED
