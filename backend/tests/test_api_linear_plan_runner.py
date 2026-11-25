"""The Linear plan runner on the lifted ``ExportRunner``: starts, merged starts, the plan hook, recovery, the closed
runner, the member a pass acts as (rule R1) and the retry after a rate limit; the pass itself is a stub here
(``test_api_linear_plan_sync``)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from api.linear_plan.gateway import NotConnectedError
from api.linear_plan.runner import LinearPlanClosedError, LinearPlanRunner
from api.linear_plan.sync import NOT_CONFIGURED, RATE_LIMITED, PushCounts, PushDeps, PushResult
from db import integrations as integrations_db
from db import linear_plan as plan_db
from db.integrations import IntegrationView
from jira_sync_support import T0
from linear.fake import FakeLinear
from linear_plan_support import FakePlanDb, target_view

PID = uuid.uuid4()
FAKE = FakeLinear()
TEAM = FAKE.add_team("ENG")


class Rig:
    """The runner over ``FakePlanDb`` with a scripted pass; the knobs are what a test turns."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = FakePlanDb()
        self.db.targets[PID] = target_view(PID, TEAM, "p1")
        self.db.install(monkeypatch)
        monkeypatch.setattr(integrations_db, "get_integration", self.get_integration)
        self.gate: asyncio.Event | None = None
        self.wake: asyncio.Event | None = None  # holds a retry in its wait until a test sets it
        self.results: list[PushResult] = []
        self.passes, self.asked, self.slept = 0, [], []
        self.bound: uuid.UUID | None = uuid.uuid4()  # the member the project's Linear is bound to now
        self.bound_row = True
        self.runner = LinearPlanRunner(
            self.gateway, transaction=self.transaction, graph_store=object(), push=self.push, sleep=self.sleep
        )

    started = property(lambda self: self.db.started)
    ended = property(lambda self: self.db.ended)

    @property
    def enabled(self) -> bool:
        return self.db.targets[PID].enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.db.targets[PID] = replace(self.db.targets[PID], enabled=value)

    @asynccontextmanager
    async def gateway(self, user_id: uuid.UUID) -> AsyncIterator[FakeLinear]:
        self.asked.append(user_id)
        yield FAKE

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.wake is not None:
            await self.wake.wait()

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield None

    async def get_integration(self, _session: Any, project_id: uuid.UUID, app: str) -> IntegrationView | None:
        if not self.bound_row or app != "linear":
            return None
        return IntegrationView(project_id, app, self.bound, None, "w1", "Graphico", T0)

    async def push(self, _project_id: str, _deps: PushDeps) -> PushResult:
        self.passes += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.results.pop(0) if self.results else PushResult("succeeded", None, PushCounts(created=2))

    async def settle(self) -> None:
        running = [run.task for run in self.runner._projects.values()]
        await asyncio.wait_for(asyncio.gather(*filter(None, running)), timeout=2)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


def triggers(rig: Rig) -> list[str]:
    return [trigger for trigger, _ in rig.started]


async def test_a_start_without_an_enabled_target_starts_nothing(rig: Rig) -> None:
    rig.enabled = False

    answer = await rig.runner.start(str(PID), "settings")

    assert answer.model_dump(include={"status", "error_code", "queued"}) == {
        "status": "skipped",
        "error_code": NOT_CONFIGURED,
        "queued": False,
    }
    assert (triggers(rig), rig.passes) == ([], 0)


async def test_a_start_runs_one_pass_and_records_its_start_and_end(rig: Rig) -> None:
    answer = await rig.runner.start(str(PID), "plan_run")
    await rig.settle()

    assert (answer.status, answer.queued, answer.trigger) == ("running", False, "plan_run")
    assert triggers(rig) == ["plan_run"] and rig.ended == [("succeeded", None, rig.ended[0][2])]
    assert rig.ended[0][2]["created"] == 2 and not any(rig.runner.live(str(PID)))


async def test_a_start_while_a_pass_runs_is_merged_into_one_more_pass(rig: Rig) -> None:
    rig.gate = gate = asyncio.Event()
    await rig.runner.start(str(PID), "plan_run")
    await asyncio.sleep(0)

    second = await rig.runner.start(str(PID), "settings")
    gate.set()
    await rig.settle()

    assert (second.queued, second.trigger) == (True, "plan_run")
    assert (rig.passes, triggers(rig)) == (2, ["plan_run", "settings"])


async def test_the_plan_hook_starts_a_pass_and_never_raises(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await rig.runner.after_plan(str(PID))
    await rig.settle()

    async def storage_down(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage down")

    monkeypatch.setattr(plan_db, "get_target", storage_down)
    await rig.runner.after_plan(str(PID))  # contained: logged, not raised
    assert rig.passes == 1


async def test_recovery_restarts_the_passes_a_stopped_process_left_running(rig: Rig) -> None:
    rig.db.running = [PID]

    rig.runner.start_recovery()
    assert rig.runner._recovery is not None
    await asyncio.wait_for(rig.runner._recovery, timeout=2)
    await rig.settle()

    assert (triggers(rig), rig.passes) == (["recovery"], 1)


async def test_a_closed_runner_refuses_a_start_with_its_own_error(rig: Rig) -> None:
    await rig.runner.aclose()

    with pytest.raises(LinearPlanClosedError, match="the linear plan runner is closed"):
        await rig.runner.start(str(PID), "settings")


async def test_the_status_of_a_target_reads_live_and_stored(rig: Rig) -> None:
    rig.gate = asyncio.Event()
    assert await rig.runner.status(str(uuid.uuid4())) is None
    await rig.runner.start(str(PID), "plan_run")
    await asyncio.sleep(0)

    live = await rig.runner.status(str(PID))
    rig.gate.set()
    await rig.settle()

    assert live is not None and (live.status, live.trigger, live.team_key) == ("running", "plan_run", "ENG")


# Rule R1 ----------------------------------------------------------------------------------------------------------


async def test_a_pass_acts_as_the_member_the_projects_linear_is_bound_to_now(rig: Rig) -> None:
    first = rig.bound
    async with rig.runner._gateway_for(PID):
        pass
    rig.bound = uuid.uuid4()  # another member took the binding over

    async with rig.runner._gateway_for(PID):
        pass

    assert rig.asked == [first, rig.bound] and first != rig.bound


@pytest.mark.parametrize("removed", ["member", "integration"])
async def test_a_removed_member_or_integration_ends_the_pass_with_linear_not_connected(rig: Rig, removed: str) -> None:
    if removed == "member":
        rig.bound = None  # the member left: the row stays, its user is null
    else:
        rig.bound_row = False  # the integration was removed

    with pytest.raises(NotConnectedError):
        async with rig.runner._gateway_for(PID):
            pass

    assert rig.asked == []


# Rate limit -------------------------------------------------------------------------------------------------------


async def test_a_rate_limited_pass_is_started_again_after_the_wait_linear_sent(rig: Rig) -> None:
    rig.results = [PushResult("failed", RATE_LIMITED, PushCounts(created=3), 7.0)]
    rig.wake = asyncio.Event()

    await rig.runner.start(str(PID), "plan_run")
    await rig.settle()
    [waiting] = rig.runner._retries.values()
    assert rig.started == [("plan_run", rig.started[0][1])]  # nothing runs during the wait
    rig.wake.set()
    await asyncio.wait_for(waiting, timeout=2)
    await rig.settle()

    assert rig.slept == [7.0]
    assert [trigger for trigger, _ in rig.started] == ["plan_run", "recovery"]
    assert [state for state, _, _ in rig.ended] == ["failed", "succeeded"]


async def test_the_wait_is_never_shorter_than_a_second_or_longer_than_an_hour(rig: Rig) -> None:
    limited = [PushResult("failed", RATE_LIMITED, PushCounts(), wait) for wait in (0.0, 99999.0)]
    rig.results = limited

    await rig.runner.start(str(PID), "plan_run")
    for _ in range(40):  # each pass ends, its retry waits (a stub that returns at once), the next pass follows
        await asyncio.sleep(0)

    await rig.settle()
    assert rig.slept == [1.0, 3600.0]


async def test_closing_the_runner_cancels_a_waiting_retry(rig: Rig) -> None:
    rig.results = [PushResult("failed", RATE_LIMITED, PushCounts(), 5.0)]
    never = asyncio.Event()

    async def wait(seconds: float) -> None:
        await never.wait()

    rig.runner._sleep = wait
    await rig.runner.start(str(PID), "plan_run")
    await rig.settle()
    [waiting] = rig.runner._retries.values()

    await rig.runner.aclose()

    assert waiting.cancelled() and rig.passes == 1


async def test_the_seam_of_the_status_mirror_names_its_stream(rig: Rig) -> None:
    with pytest.raises(NotImplementedError, match="S3b"):
        await rig.runner.mirror_task_status(str(PID), "TASK::A::1", "in_progress")
