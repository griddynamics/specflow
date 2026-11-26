"""The task status mirror (decision O4): the Linear sub-issue follows the status an agent gives a task, forward only,
with one comment for ``failed`` and ``blocked`` and the pull request as a link; never an error for the agent."""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.linear_plan.gateway import NotConnectedError
from api.linear_plan.mirror import MirrorDeps, TaskChange, first_state, mirror_changes
from db.models import LinearIssue
from jira_sync_support import make_plan, story, task
from linear.errors import LinearError, LinearRateLimitedError, LinearUnavailableError
from linear_plan_support import Harness

TASK = "TASK::A::1"
OTHER = "TASK::A::2"
PR = "https://git.example.test/org/repo/pull/7"


class Rig(Harness):
    """A pass has made the issues; ``mirror`` runs the mirror over the same fake rows."""

    async def issue_of(self, _project_id: str, node_id: str) -> LinearIssue | None:
        return self.store.items.get(node_id)

    async def mirror(self, *changes: TaskChange) -> None:
        deps = MirrorDeps(self.store, self._gateway_for, self.issue_of, self._load_plan)
        await mirror_changes(str(self.project_id), changes, deps)

    def state(self, node_id: str = TASK) -> str:
        return self.issue(node_id).state.name

    async def bodies(self, node_id: str = TASK) -> list[str]:
        return [comment.body for comment in await self.linear.comments(self.issue(node_id).id)]

    def attached(self, node_id: str = TASK) -> list[tuple[str, str]]:
        return [(link.url, link.title) for link in self.linear.attachments(self.issue(node_id).id)]


@pytest.fixture
async def rig() -> Rig:
    made = Rig()
    await made.push()
    made.linear.calls.clear()
    made.linear.writes.clear()
    return made


async def test_a_claim_moves_the_issue_to_the_first_started_state_and_records_it(rig: Rig):
    await rig.mirror(TaskChange(TASK, "in_progress"))
    assert rig.state() == "In Progress"
    assert rig.store.items[TASK].mirrored_state == "In Progress"


async def test_a_completion_moves_it_to_the_first_completed_state(rig: Rig):
    await rig.mirror(TaskChange(TASK, "in_progress"))
    await rig.mirror(TaskChange(TASK, "completed"))
    assert rig.state() == "Done"
    assert rig.store.items[TASK].mirrored_state == "Done"


async def test_the_lowest_position_of_the_type_wins(rig: Rig):
    team = rig.linear.add_team("OPS", states=[("Late", "started"), ("Todo", "unstarted"), ("Early", "started")])
    states = await rig.linear.workflow_states(team.id)
    chosen = first_state(sorted(states, key=lambda state: state.name), "started")
    assert chosen is not None and chosen.name == "Late"
    assert first_state(states, "canceled") is None


async def test_a_person_who_moved_it_further_is_left_alone(rig: Rig):
    rig.linear.move_issue(rig.issue(TASK).id, "Done")
    await rig.mirror(TaskChange(TASK, "in_progress"))
    assert rig.state() == "Done" and rig.writes() == []
    assert rig.store.items[TASK].mirrored_state is None


async def test_an_issue_in_review_is_not_moved_to_the_first_started_state(rig: Rig):
    rig.linear.move_issue(rig.issue(TASK).id, "In Review")
    await rig.mirror(TaskChange(TASK, "in_progress"))
    assert rig.state() == "In Review" and rig.writes() == []


async def test_a_canceled_issue_is_never_reopened(rig: Rig):
    rig.linear.move_issue(rig.issue(TASK).id, "Canceled")
    await rig.mirror(TaskChange(TASK, "completed"))
    assert rig.state() == "Canceled" and rig.writes() == []


async def test_failed_and_blocked_leave_one_comment_each_and_move_nothing(rig: Rig):
    await rig.mirror(TaskChange(TASK, "failed", reason="tests red"), TaskChange(TASK, "blocked", reason="finding F1"))
    assert await rig.bodies() == ["Graphico: task failed: tests red", "Graphico: task blocked: finding F1"]
    assert rig.state() == "Backlog" and rig.store.items[TASK].mirrored_state is None


async def test_a_retried_call_does_not_comment_twice_but_a_new_attempt_does(rig: Rig):
    change = TaskChange(TASK, "failed", reason="tests red")
    await rig.mirror(change)
    await rig.mirror(change)
    assert len(await rig.bodies()) == 1
    rig.plan = make_plan([story("STORY::A::ONE")], [task(TASK, attempts=1)])
    await rig.mirror(change)
    assert len(await rig.bodies()) == 2


async def test_a_reason_is_optional(rig: Rig):
    await rig.mirror(TaskChange(TASK, "blocked"))
    assert await rig.bodies() == ["Graphico: task blocked"]


async def test_the_pull_request_is_attached_once(rig: Rig):
    await rig.mirror(TaskChange(TASK, "completed", pr_url=PR))
    await rig.mirror(TaskChange(TASK, "completed", pr_url=PR))
    assert [link for link in rig.attached() if link[1] == "Pull request"] == [(PR, "Pull request")]


@pytest.mark.parametrize("case", ["no_target", "disabled", "no_row", "no_issue", "removed"])
async def test_nothing_is_called_without_an_enabled_target_or_an_issue_row(rig: Rig, case: str):
    node = TASK
    if case == "no_target":
        rig.store.target = None
    elif case == "disabled" and rig.store.target is not None:
        rig.store.target = replace(rig.store.target, enabled=False)
    elif case == "no_row":
        node = "TASK::A::UNKNOWN"
    elif case == "no_issue":
        rig.store.items[TASK].issue_id = None
    elif case == "removed":
        rig.store.items[TASK].removed_at = rig.store.items[TASK].requested_at
    await rig.mirror(TaskChange(node, "in_progress", pr_url=PR))
    assert rig.linear.calls == []


@pytest.mark.parametrize("error", [LinearUnavailableError(), LinearRateLimitedError(5.0)])
async def test_a_linear_error_is_logged_as_its_code_and_never_raised(rig: Rig, caplog, error: LinearError):
    caplog.set_level("INFO")
    rig.linear.fail_next(error, method="issue")
    await rig.mirror(TaskChange(TASK, "in_progress"))
    assert [getattr(record, "code", None) for record in caplog.records] == [str(error)]
    assert rig.state() == "Backlog"


async def test_a_project_that_is_not_connected_is_logged_not_raised(rig: Rig, caplog):
    caplog.set_level("INFO")
    rig.fail(NotConnectedError())
    await rig.mirror(TaskChange(TASK, "in_progress"))
    assert [getattr(record, "code", None) for record in caplog.records] == ["NotConnectedError"]


async def test_a_batch_reads_the_states_of_a_team_once(rig: Rig):
    await rig.mirror(TaskChange(TASK, "in_progress"), TaskChange(OTHER, "in_progress"))
    assert rig.state(OTHER) == "In Progress"
    assert rig.linear.count("workflow_states") == 1
