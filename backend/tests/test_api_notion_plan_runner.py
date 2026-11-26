"""The Notion plan runner on the lifted ``ExportRunner``: starts, merged starts, the row-cap restart, the plan hook,
recovery, the closed runner, the status it shows and the member a pass acts as. The pass itself is a stub here
(``test_api_notion_plan_sync``)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from api.notion_plan.runner import NotionPlanClosedError, NotionPlanRunner, status_of
from api.notion_plan.sync import NOT_CONFIGURED, PushCounts, PushDeps, PushResult
from db import integrations as integrations_db
from db import notion_plan as plan_db
from db.integrations import IntegrationView
from notion.reasons import NotionCode, NotionError
from notion_plan_support import target_view
from notion_support import ACTING, NOW, FakeNotionConnections

PID = uuid.uuid4()


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.target = target_view(PID)
        self.on = True
        self.starts: list[tuple[str, str]] = []
        self.ends: list[tuple[str, str | None, dict[str, int]]] = []
        self.hold: asyncio.Event | None = None
        self.runs = 0
        self.interrupted: list[uuid.UUID] = []
        self.bound: uuid.UUID | None = ACTING  # the member the project's Notion is bound to now
        self.bound_row = True
        self.more_rows = 0  # passes that report rows left (and ask for one more)
        self.connections = FakeNotionConnections()
        monkeypatch.setattr(integrations_db, "get_integration", self.get_integration)
        monkeypatch.setattr(plan_db, "get_target", self.get_target)
        monkeypatch.setattr(plan_db, "record_start", self.record_start)
        monkeypatch.setattr(plan_db, "record_end", self.record_end)
        monkeypatch.setattr(plan_db, "running_targets", self.running_targets)
        self.runner = NotionPlanRunner(
            self.connections, transaction=self.transaction, graph_store=object(), push=self.fake_push
        )

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield None

    async def get_integration(self, _session: Any, project_id: uuid.UUID, app: str) -> IntegrationView | None:
        if not self.bound_row or app != "notion":
            return None
        return IntegrationView(
            project_id, app, self.bound, None, "ws-1", "Sample Workspace", NOW
        )

    async def get_target(self, _session: Any, project_id: uuid.UUID) -> Any:
        return replace(self.target, enabled=self.on) if project_id == PID else None

    async def record_start(self, _session: Any, _project_id: uuid.UUID, *, trigger: str, queued_at: Any) -> None:
        self.starts.append((trigger, str(queued_at)))

    async def record_end(self, _session: Any, _project_id: uuid.UUID, **values: Any) -> None:
        self.ends.append((values["status"], values["error_code"], values["counts"]))

    async def running_targets(self, _session: Any) -> list[uuid.UUID]:
        return self.interrupted

    async def fake_push(self, project_id: str, deps: PushDeps) -> PushResult:
        self.runs += 1
        if self.hold is not None:
            await self.hold.wait()
        if self.more_rows:
            self.more_rows -= 1
            await deps.restart(project_id)  # what a pass that hit the row cap does
            return PushResult("succeeded", None, PushCounts(created=120, deferred=5))
        return PushResult("succeeded", None, PushCounts(created=2))

    async def drain(self) -> None:
        tasks = [p.task for p in self.runner._projects.values() if p.task is not None]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


async def test_a_start_without_an_enabled_target_starts_nothing(rig: Rig) -> None:
    rig.on = False

    status = await rig.runner.start(str(PID), "settings")

    assert (status.status, status.error_code, status.queued) == ("skipped", NOT_CONFIGURED, False)
    assert status.parent_page_title == "Specflow Notion Lab" and rig.starts == [] and rig.runs == 0


async def test_a_start_runs_one_pass_and_records_its_start_and_end(rig: Rig) -> None:
    status = await rig.runner.start(str(PID), "plan_run")
    await rig.drain()

    assert (status.status, status.queued, status.trigger) == ("running", False, "plan_run")
    assert [trigger for trigger, _ in rig.starts] == ["plan_run"]
    [(state, code, counts)] = rig.ends
    assert (state, code, counts["created"], counts["deferred"]) == ("succeeded", None, 2, 0)
    assert rig.runner.live(str(PID)) == (False, False)


async def test_a_start_while_a_pass_runs_is_merged_into_one_more_pass(rig: Rig) -> None:
    rig.hold = asyncio.Event()
    await rig.runner.start(str(PID), "plan_run")
    await asyncio.sleep(0)

    merged = await rig.runner.start(str(PID), "settings")
    rig.hold.set()
    await rig.drain()

    assert (merged.status, merged.queued, merged.trigger) == ("running", True, "plan_run")
    assert rig.runs == 2 and [trigger for trigger, _ in rig.starts] == ["plan_run", "settings"]


async def test_a_pass_that_left_rows_asks_for_one_more_pass_with_the_plan_run_trigger(rig: Rig) -> None:
    rig.more_rows = 1

    await rig.runner.start(str(PID), "settings")
    await rig.drain()

    assert rig.runs == 2
    assert [trigger for trigger, _ in rig.starts] == ["settings", "plan_run"]
    assert [counts["deferred"] for _, _, counts in rig.ends] == [5, 0]


async def test_the_plan_hook_starts_a_pass_and_never_raises(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await rig.runner.after_plan(str(PID))
    await rig.drain()
    assert rig.runs == 1

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage down")

    monkeypatch.setattr(plan_db, "get_target", broken)
    await rig.runner.after_plan(str(PID))  # contained: logged, not raised


async def test_saving_a_destination_starts_a_pass_only_when_it_was_turned_on_or_changed(rig: Rig) -> None:
    await rig.runner.after_settings(
        str(PID), plan_db.TargetSaved(destination_changed=False, turned_on=False, keys_changed=False)
    )
    assert rig.runs == 0

    await rig.runner.after_settings(
        str(PID), plan_db.TargetSaved(destination_changed=False, turned_on=True, keys_changed=False)
    )
    await rig.drain()
    await rig.runner.after_settings(
        str(PID), plan_db.TargetSaved(destination_changed=True, turned_on=False, keys_changed=True)
    )
    await rig.drain()

    assert [trigger for trigger, _ in rig.starts] == ["settings", "settings"]


async def test_recovery_restarts_the_passes_a_stopped_process_left_running(rig: Rig) -> None:
    rig.interrupted = [PID]

    rig.runner.start_recovery()
    recovery = rig.runner._recovery
    assert recovery is not None
    await asyncio.wait_for(recovery, timeout=2)
    await rig.drain()

    assert [trigger for trigger, _ in rig.starts] == ["recovery"] and rig.runs == 1


async def test_a_closed_runner_refuses_a_start_with_its_own_error(rig: Rig) -> None:
    await rig.runner.aclose()

    with pytest.raises(NotionPlanClosedError, match="the notion plan runner is closed"):
        await rig.runner.start(str(PID), "settings")


async def test_a_pass_acts_as_the_member_the_projects_notion_is_bound_to_now(rig: Rig) -> None:
    other = uuid.uuid4()
    rig.connections.users[other] = "active"

    await rig.runner._gateway_for(PID)
    rig.bound = other  # another member took the binding over
    await rig.runner._gateway_for(PID)

    assert rig.connections.gateways_asked == [ACTING, other]


@pytest.mark.parametrize("removed", ["member", "integration"])
async def test_a_removed_member_or_integration_ends_the_pass_with_notion_not_connected(rig: Rig, removed: str) -> None:
    if removed == "member":
        rig.bound = None  # the member left: the row stays, its user is null
    else:
        rig.bound_row = False  # the integration was removed

    with pytest.raises(NotionError) as raised:
        await rig.runner._gateway_for(PID)

    assert raised.value.code is NotionCode.NOT_CONNECTED


async def test_an_expired_connection_of_the_bound_member_asks_to_reconnect(rig: Rig) -> None:
    rig.connections.expire(ACTING)

    with pytest.raises(NotionError) as raised:
        await rig.runner._gateway_for(PID)

    assert raised.value.code is NotionCode.RECONNECT_REQUIRED


def test_the_status_of_a_target_reads_its_stored_run() -> None:
    base = target_view(PID)
    stored = replace(base, last_status="partial", last_error_code=None, last_finished_at=NOW, last_counts={"failed": 1})

    status = status_of(stored, live=False, queued=False)
    live = status_of(stored, live=True, queued=True)
    interrupted = status_of(replace(base, last_status="running"), live=False, queued=False)

    assert status.status == "partial" and status.counts is not None and status.counts.failed == 1
    assert (live.status, live.queued) == ("running", True)
    assert (interrupted.status, interrupted.error_code) == ("failed", "interrupted")
    assert status_of(replace(base, enabled=False), live=False, queued=False).status == "idle"


async def test_a_member_who_reconnected_into_another_workspace_ends_the_pass_before_any_notion_call(rig: Rig) -> None:
    rig.connections.workspace_id = "ws-other"  # the binding still names the first workspace

    with pytest.raises(NotionError) as raised:
        await rig.runner._gateway_for(PID)

    assert raised.value.code is NotionCode.RECONNECT_REQUIRED and rig.connections.gateways_asked == []
