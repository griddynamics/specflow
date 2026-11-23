"""Story-by-story creation and the live progress of a pass (docs/jira-sync/sync.md#creation-order and #progress).

The engine runs over the fakes of ``jira_sync_support``; the runner tests use a stub engine that reports by hand.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from api.jira.desired import EPIC
from api.jira.runner import JiraRunnerDependencies, JiraSyncRunner
from api.jira.sync import SyncProgress, SyncResult, sync_once
from jira.reasons import Reason
from jira_sync_support import Harness, edit_task, task, with_plan
from test_api_jira_runner import OK, SITE, settle

S1, S2, S3 = "STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"
T1, T2, T3, T4, T5 = (f"TASK::A::{n}" for n in range(1, 6))
IN_PROGRESS = ("indeterminate", "In Progress")


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


async def run(harness: Harness):
    """One pass; returns the result and every progress it reported."""

    seen: list[SyncProgress] = []
    deps = replace(harness.deps(), report=lambda _project, progress: seen.append(progress))
    result = await sync_once(str(harness.project_id), deps)
    return result, seen


def steps(seen: list[SyncProgress], name: str) -> list[tuple[int, int]]:
    return [(p.done, p.total) for p in seen if p.step == name]


def assert_ordered_and_bounded(seen: list[SyncProgress]) -> None:
    order = [p.step for p in seen]
    assert order == sorted(order, key=["issues", "links", "order"].index)  # never goes back to an earlier step
    assert all(0 <= p.done <= p.total for p in seen)


# --- story-by-story creation


async def test_the_first_sync_creates_each_story_right_before_its_sub_tasks(h):
    """
    Scenario: three stories (two with two tasks, one with one); the first pass creates the Epic, then a story and
    its sub-tasks, then the next story.

    Engine -> Jira: create(Epic), create(story 1), create(its tasks), create(story 2), ... ; no rank call.
    """
    result, _ = await run(h)
    assert (result.status, result.counts.created, result.counts.ranked) == ("succeeded", 9, 0)
    created = [call["node_id"] for call in h.gateway.called("create_issue")]
    assert created == [EPIC, S1, T1, T2, S3, T5, S2, T3, T4]
    assert h.gateway.called("rank") == []


async def test_the_jira_order_of_each_scope_follows_the_plan_after_the_interleaved_creation(h):
    await run(h)
    keys = {node: h.key(node) for node in (S1, S2, S3, T1, T2, T3, T4, T5)}
    stories = h.gateway.rank_order[h.key(EPIC)]
    assert stories == [keys[S1], keys[S3], keys[S2]]
    assert h.gateway.rank_order[keys[S2]] == [keys[T3], keys[T4]]
    assert h.gateway.rank_order[keys[S1]] == [keys[T1], keys[T2]]
    assert (await run(h))[0].counts.ranked == 0


# --- progress of a pass


async def test_a_first_sync_reports_issues_then_links_then_order_and_every_step_reaches_its_total(h):
    """
    Scenario: the first pass creates nine issues, adds the one link and ranks nothing.

    Engine -> Host: issues 0/9, 1/9 ... 9/9, links 0/1, links 1/1, order 0/0.
    """
    result, seen = await run(h)
    assert result.status == "succeeded"
    assert steps(seen, "issues") == [(n, 9) for n in range(10)]
    assert steps(seen, "links") == [(0, 1), (1, 1)]
    assert steps(seen, "order") == [(0, 0)]
    assert_ordered_and_bounded(seen)


async def test_a_pass_with_edits_comments_and_removals_ticks_each_event_once(h):
    await run(h)
    h.gateway.set_category(h.key(T4), *IN_PROGRESS)
    plan = edit_task(edit_task(h.plan, T3, status="retired"), T4, status="superseded")
    plan = edit_task(plan, T1, title="Renamed")
    h.set_plan(plan)
    result, seen = await run(h)
    assert (result.status, result.counts.removed, result.counts.updated) == ("succeeded", 2, 2)
    issues = steps(seen, "issues")
    c = result.counts
    assert issues[0][0] == 0 and issues[-1] == (c.updated + c.removed, c.updated + c.removed)  # one tick per event
    assert_ordered_and_bounded(seen)


async def test_the_issues_step_counts_the_creates_and_the_edits_of_a_pass(h):
    await run(h)
    h.set_plan(with_plan(h.plan, tasks=[task("TASK::A::6", S1)]))
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))
    result, seen = await run(h)
    issues = steps(seen, "issues")
    total = result.counts.created + result.counts.updated  # the new task, its story's points and the renamed task
    assert (result.counts.created, total) == (1, 3)
    assert issues[0] == (0, 1 + 2) and issues[-1] == (3, 3)


async def test_an_item_that_fails_still_counts_so_done_reaches_total(h):
    await run(h)
    h.set_plan(with_plan(h.plan, tasks=[task("TASK::A::6"), task("TASK::A::7")]))
    h.gateway.fail("create_issue", Reason.INVALID_FIELD, status=400)
    result, seen = await run(h)
    assert (result.status, result.counts.failed) == ("partial", 1)
    total = result.counts.failed + result.counts.created + result.counts.updated  # the story's points are edited too
    assert steps(seen, "issues")[-1] == (total, total) and total == 3
    assert_ordered_and_bounded(seen)


async def test_the_children_of_a_missing_parent_count_too(h):
    h.gateway.fail("create_issue", Reason.INVALID_FIELD, status=400)  # the Epic is refused: the rest is skipped
    result, seen = await run(h)
    assert result.counts.created == 0
    assert steps(seen, "issues")[-1] == (9, 9)


async def test_a_pass_that_reorders_a_scope_reports_one_order_unit_for_it(h):
    await run(h)
    h.set_plan(with_plan(h.plan, waits=[(T3, T4)]))  # T4 now comes first in its story
    result, seen = await run(h)
    assert result.counts.ranked >= 1
    assert steps(seen, "order") == [(0, 1), (1, 1)]
    assert_ordered_and_bounded(seen)


async def test_a_pass_with_nothing_to_do_reports_nothing(h):
    await run(h)
    result, seen = await run(h)
    assert result.status == "succeeded" and result.counts.created == 0
    assert seen == []


# --- the runner


class ReportingSync:
    """A stub engine that reports from inside the pass and waits for the test."""

    def __init__(self, result: SyncResult = OK, error: Exception | None = None) -> None:
        self.reported = asyncio.Event()
        self.release = asyncio.Event()
        self.result, self.error = result, error

    async def __call__(self, project_id, deps) -> SyncResult:
        deps.report(project_id, SyncProgress("issues", 2, 5))
        self.reported.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result


def runner_of(harness: Harness, sync) -> JiraSyncRunner:
    deps = JiraRunnerDependencies(
        sync_once=sync, load_plan=harness._load_plan, spec_names=harness._spec_names, now=harness._now
    )
    return JiraSyncRunner(
        harness.gateway,
        transaction=harness.db.transaction,
        store=None,
        public_url=None,
        site_url=SITE,
        dependencies=deps,
    )


async def test_a_running_status_carries_the_progress_and_a_finished_one_does_not(monkeypatch):
    harness = Harness(monkeypatch)
    sync = ReportingSync()
    runner = runner_of(harness, sync)
    pid = str(harness.project_id)

    first = await runner.start(pid, "mcp")
    assert first.progress is None  # queued: the pass has not reached its work
    await asyncio.wait_for(sync.reported.wait(), 2)
    during = await runner.status(pid)
    assert during is not None and during.status == "running"
    assert during.progress is not None and during.progress.model_dump() == {"step": "issues", "done": 2, "total": 5}
    sync.release.set()
    await settle(runner)
    after = await runner.status(pid)
    assert after is not None and after.status == "succeeded" and after.progress is None


async def test_a_failed_pass_clears_the_progress(monkeypatch):
    harness = Harness(monkeypatch)
    sync = ReportingSync(error=RuntimeError("boom"))
    runner = runner_of(harness, sync)
    pid = str(harness.project_id)

    await runner.start(pid, "mcp")
    await asyncio.wait_for(sync.reported.wait(), 2)
    assert (await runner.status(pid)).progress is not None
    sync.release.set()
    await settle(runner)
    after = await runner.status(pid)
    assert after is not None and after.status != "running" and after.progress is None
    assert runner._projects[pid].progress is None


async def test_the_engine_reports_through_the_runner_end_to_end(monkeypatch):
    harness = Harness(monkeypatch)
    runner = JiraSyncRunner(
        harness.gateway,
        transaction=harness.db.transaction,
        store=None,
        public_url=None,
        site_url=SITE,
        dependencies=JiraRunnerDependencies(
            load_plan=harness._load_plan, spec_names=harness._spec_names, now=harness._now
        ),
    )
    pid = str(harness.project_id)
    await runner.start(pid, "mcp")
    await settle(runner)
    done = await runner.status(pid)
    assert done is not None and done.status == "succeeded" and done.progress is None
    assert done.counts is not None and done.counts.created == 9
