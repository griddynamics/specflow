"""``PlanDestinations``: the plan runner's one post-plan hook calls each registered destination in order, and a
destination that fails never reaches the plan run or the destinations after it (docs/mission-control/
integrations-api.md "Plan destinations")."""

from __future__ import annotations

import logging

import pytest

from api.plan.runner import GraphPlanRunner
from api.plan_destinations import PlanDestinations
from db import notion_plan as plan_db
from test_api_notion_plan_runner import PID as NOTION_PROJECT
from test_api_notion_plan_runner import Rig as NotionRig

PROJECT = "00000000-0000-4000-8000-000000000001"
ERROR_TEXT = "text-of-the-error-canary"


class Recorder:
    def __init__(self, name: str, calls: list[tuple[str, str]], *, fail: bool = False) -> None:
        self.name, self._calls, self._fail = name, calls, fail

    async def after_plan(self, project_id: str) -> None:
        self._calls.append((self.name, project_id))
        if self._fail:
            raise RuntimeError(ERROR_TEXT)


async def test_every_destination_is_told_in_registration_order() -> None:
    calls: list[tuple[str, str]] = []
    destinations = PlanDestinations()
    destinations.register(Recorder("jira", calls))
    destinations.register(Recorder("projects", calls))

    await destinations.after_plan(PROJECT)

    assert calls == [("jira", PROJECT), ("projects", PROJECT)]


async def test_a_failing_destination_is_logged_by_class_only_and_the_next_one_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[tuple[str, str]] = []
    destinations = PlanDestinations()
    destinations.register(Recorder("jira", calls, fail=True))
    destinations.register(Recorder("projects", calls))

    with caplog.at_level(logging.WARNING):
        await destinations.after_plan(PROJECT)  # never raises

    assert calls == [("jira", PROJECT), ("projects", PROJECT)]
    [logged] = [r for r in caplog.records if getattr(r, "event", None) == "api.plan.destination_failed"]
    assert (logged.project_id, logged.destination, logged.error_type) == (PROJECT, "Recorder", "RuntimeError")
    assert ERROR_TEXT not in caplog.text


async def test_without_a_destination_the_hook_does_nothing() -> None:
    await PlanDestinations().after_plan(PROJECT)


async def test_the_plan_runner_calls_the_fan_out_after_a_plan_that_did_not_fail() -> None:
    """``GraphPlanRunner._start_jira_sync`` is the runner's only call of the hook; the runner is not changed."""

    calls: list[tuple[str, str]] = []
    destinations = PlanDestinations()
    destinations.register(Recorder("jira", calls))
    runner = object.__new__(GraphPlanRunner)  # without the runner's heavy setup
    vars(runner)["_jira"] = destinations  # what ``jira=`` sets

    await runner._start_jira_sync(PROJECT)

    assert calls == [("jira", PROJECT)]


# The Notion plan destination ---------------------------------------------------------------------------------------


async def test_the_notion_runner_is_a_destination_whose_pass_starts_after_the_plan_with_the_plan_run_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = NotionRig(monkeypatch)
    calls: list[tuple[str, str]] = []
    destinations = PlanDestinations()
    destinations.register(Recorder("jira", calls))
    destinations.register(rig.runner)

    await destinations.after_plan(str(NOTION_PROJECT))
    await rig.drain()

    assert calls == [("jira", str(NOTION_PROJECT))]
    assert [trigger for trigger, _ in rig.starts] == ["plan_run"] and rig.runs == 1


async def test_a_notion_runner_that_fails_to_read_its_target_does_not_stop_the_destinations_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = NotionRig(monkeypatch)

    async def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(ERROR_TEXT)

    monkeypatch.setattr(plan_db, "get_target", broken)
    calls: list[tuple[str, str]] = []
    destinations = PlanDestinations()
    destinations.register(rig.runner)
    destinations.register(Recorder("projects", calls))

    await destinations.after_plan(str(NOTION_PROJECT))  # never raises

    assert calls == [("projects", str(NOTION_PROJECT))] and rig.runs == 0


async def test_a_notion_project_without_a_destination_starts_no_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = NotionRig(monkeypatch)
    rig.on = False
    destinations = PlanDestinations()
    destinations.register(rig.runner)

    await destinations.after_plan(str(NOTION_PROJECT))

    assert rig.starts == [] and rig.runs == 0
