"""The Linear plan pass (plans/LINEAR/LINEAR-PLAN.md section 3): create, idempotent re-run, a retried create, overwrite
while the issue has not started, comment after, removal and return, estimate refused, rate limit and lost access, on
``FakeLinear`` and in-memory rows."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.linear_plan.gateway import NotConnectedError
from api.linear_plan.sync import (
    ESTIMATE_REFUSED,
    NO_PLAN,
    NOT_CONFIGURED,
    NOT_CONNECTED,
    PLAN_APPLYING,
    PROJECT_NOT_FOUND,
    RATE_LIMITED,
    RECONNECT_REQUIRED,
    TARGET_CHANGED,
)
from graph.plan.models import CurrentPlan
from graph.plan.reader import LoadedPlan
from jira_sync_support import make_plan, story, task
from linear.errors import LinearInvalidError, LinearUnauthorizedError
from linear.ids import KIND_MILESTONE, KIND_RELATION, node_uuid
from linear.models import NewIssue
from linear_plan_support import PUBLIC_URL, STORIES, Harness, crossing_plan


async def first_pass(harness: Harness) -> None:
    result = await harness.push()
    assert (result.status, result.error_code, result.counts.failed) == (SUCCEEDED, None, 0)


async def edges(harness: Harness) -> set[tuple[str, str]]:
    """Every ``blocker blocks waiter`` relation of the fake, by node id."""

    nodes = {issue.id: node for node in harness.store.items for issue in [harness.issue(node)]}
    found: set[tuple[str, str]] = set()
    for issue in harness.linear.all_issues():
        for relation in await harness.linear.relations(issue.id):
            assert relation.type == "blocks"
            found.add((nodes[relation.issue_id], nodes[relation.related_issue_id]))
    return found


async def label_names(harness: Harness, node: str) -> dict[str, str | None]:
    """The labels of an issue by name, each with the name of its group (or None)."""

    every = {label.id: label for label in await harness.linear.labels(harness.team.id)}
    return {
        label.name: None if label.parent_id is None else every[label.parent_id].name
        for label in harness.issue(node).labels
    }


# The first pass ---------------------------------------------------------------------------------------------------


async def test_the_first_pass_makes_issues_sub_issues_labels_milestones_estimates_and_links() -> None:
    harness = Harness(crossing_plan())

    result = await harness.push()

    assert (result.status, result.error_code, result.counts.created, result.counts.failed) == (SUCCEEDED, None, 8, 0)
    one, first = harness.issue("STORY::A::ONE"), harness.issue("TASK::A::1")
    assert (first.parent_id, one.parent_id) == (one.id, None)
    assert {issue.project_id for issue in harness.linear.all_issues()} == {harness.linear_project.id}
    assert (one.estimate, first.estimate, harness.issue("TASK::A::3").estimate) == (4, 1, 5)  # a story sums its tasks
    assert await label_names(harness, "TASK::A::1") == {"graphico": None, "area-a": "Graphico area"}
    two = harness.issue("STORY::A::TWO")
    assert two.milestone_id == str(node_uuid(harness.project_id, KIND_MILESTONE, "R2")) and one.milestone_id is None
    assert "- [ ] it works" in (first.description or "") and ">>> Graphico" in (first.description or "")
    assert "> [!NOTE]" in (first.description or "") and (first.description or "").rstrip().endswith(">>>")
    [link] = harness.linear.attachments(first.id)
    assert (link.title, link.url) == ("Open in Graphico", f"{PUBLIC_URL}/projects/{harness.project_id}")
    rows = harness.store.items
    assert {row.identifier for row in rows.values()} == {issue.identifier for issue in harness.linear.all_issues()}
    assert rows["TASK::A::1"].content["blocked_by"] == [] and rows["TASK::A::2"].content["blocked_by"] == ["TASK::A::1"]


async def test_wait_for_edges_become_blocks_relations_at_task_and_story_level() -> None:
    harness = Harness(crossing_plan())

    result = await harness.push()

    assert await edges(harness) == {
        ("TASK::A::1", "TASK::A::2"),
        ("TASK::A::1", "TASK::A::3"),
        ("STORY::A::ONE", "STORY::A::TWO"),
    }
    assert result.counts.relations_added == 3


async def test_a_story_is_in_plan_order_and_a_task_follows_its_story() -> None:
    harness = Harness(crossing_plan())

    await first_pass(harness)

    order = [issue.identifier for issue in harness.linear.all_issues()]
    nodes = [harness.issue(node).identifier for node in ("STORY::A::ONE", "TASK::A::1", "TASK::A::2")]
    assert [order.index(identifier) for identifier in nodes] == sorted(order.index(i) for i in nodes)
    assert order.index(nodes[0]) < order.index(nodes[1])


async def test_the_link_is_left_out_without_a_public_url() -> None:
    harness = Harness(crossing_plan())
    harness.public_url = None

    await first_pass(harness)

    assert harness.linear.count("attach_link") == 0


# Idempotence ------------------------------------------------------------------------------------------------------


async def test_a_second_pass_with_nothing_changed_makes_no_call_at_all() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    calls = len(harness.linear.calls)

    result = await harness.push()

    assert (result.status, result.counts.unchanged, result.counts.created) == (SUCCEEDED, 8, 0)
    assert len(harness.linear.calls) == calls


async def test_a_retried_create_adopts_the_issue_a_crashed_pass_made() -> None:
    """The pass crashed after Linear made the issue and before its row got the id: the row is pending, the retry gets
    "already exists" for the client id and reads the issue instead of making a second one."""

    harness = Harness(crossing_plan())
    node = "STORY::A::ONE"
    target = harness.store.target
    assert target is not None
    made = await harness.linear.create_issue(
        NewIssue(harness.issue_uuid(node), harness.team.id, f"Story {node}", project_id=harness.linear_project.id)
    )
    await harness.store.add_pending(target, node_id=node, kind="story", requested_at=target.updated_at)

    result = await harness.push()

    assert (result.status, result.counts.created, result.counts.failed) == (SUCCEEDED, 8, 0)
    assert [i.title for i in harness.linear.all_issues()].count(f"Story {node}") == 1
    assert harness.store.items[node].issue_id == uuid.UUID(made.id)
    assert harness.store.items[node].identifier == made.identifier
    assert harness.linear.count("issue") == 1  # the adoption read


async def test_a_retried_relation_adopts_the_one_a_crashed_pass_made() -> None:
    harness = Harness(crossing_plan())
    harness.linear.rate_limit_next(1, method="create_relation")
    stopped = await harness.push()
    assert (stopped.error_code, stopped.counts.created, stopped.counts.relations_added) == (RATE_LIMITED, 8, 0)
    first, second = harness.issue("TASK::A::1"), harness.issue("TASK::A::2")
    relation_id = str(node_uuid(harness.project_id, KIND_RELATION, "TASK::A::1>TASK::A::2"))
    await harness.linear.create_relation(relation_id, first.id, second.id, "blocks")  # made, row not written

    result = await harness.push()

    assert (result.status, result.counts.created, result.counts.relations_added) == (SUCCEEDED, 0, 2)
    assert len(await edges(harness)) == 3
    assert (await harness.push()).counts.unchanged == 8 and harness.linear.count("create_relation") == 5


# Updates ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["Backlog", "Todo"])
async def test_an_issue_that_has_not_started_takes_the_plan_fields(state: str) -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    harness.linear.move_issue(harness.issue("TASK::A::3").id, state)
    harness.plan = crossing_plan(title="Renamed task")

    result = await harness.push()

    assert (result.status, result.counts.updated, result.counts.commented) == (SUCCEEDED, 1, 0)
    changed = harness.issue("TASK::A::3")
    assert changed.title == "Renamed task" and await harness.linear.comments(changed.id) == []
    assert (await harness.push()).counts.unchanged == 8


async def test_labels_a_person_added_stay_when_the_plan_fields_are_written() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    issue = harness.issue("TASK::A::3")
    bug = await harness.linear.create_label(harness.team.id, str(uuid.uuid4()), "bug")
    await harness.linear.update_issue(issue.id, label_ids=[*issue.label_ids, bug.id])
    harness.plan = crossing_plan(title="Renamed task")

    await harness.push()

    assert set(await label_names(harness, "TASK::A::3")) == {"graphico", "area-a", "bug"}


async def test_a_release_change_moves_the_story_to_its_milestone() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    harness.plan = crossing_plan(release="R1")

    result = await harness.push()

    assert result.counts.updated == 1
    assert harness.issue("STORY::A::ONE").milestone_id == str(node_uuid(harness.project_id, KIND_MILESTONE, "R1"))


async def test_an_issue_work_has_started_on_gets_one_comment_that_lists_the_changes() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    harness.linear.move_issue(harness.issue("TASK::A::3").id, "In Progress")
    harness.plan = crossing_plan(title="Renamed task")

    result = await harness.push()

    assert (result.status, result.counts.updated, result.counts.commented) == (SUCCEEDED, 0, 1)
    issue = harness.issue("TASK::A::3")
    assert issue.title == "Task TASK::A::3"  # the fields stay
    [comment] = await harness.linear.comments(issue.id)
    assert 'Summary: "Task TASK::A::3" → "Renamed task"' in comment.body
    assert 'This issue is in state "In Progress"' in comment.body and comment.body.startswith("Graphico plan update")
    before = len(harness.linear.calls)
    assert (await harness.push()).counts.unchanged == 8 and len(harness.linear.calls) == before  # said once


async def test_a_new_task_in_a_story_work_has_started_on_is_created_under_it() -> None:
    harness = Harness(make_plan([story(STORIES[0])], [task("TASK::A::1")]))
    await first_pass(harness)
    harness.linear.move_issue(harness.issue("STORY::A::ONE").id, "In Progress")
    harness.plan = make_plan([story(STORIES[0])], [task("TASK::A::1"), task("TASK::A::2")])

    result = await harness.push()

    assert result.counts.created == 1 and harness.issue("TASK::A::2").parent_id == harness.issue("STORY::A::ONE").id


# Removal ----------------------------------------------------------------------------------------------------------


async def test_a_task_that_leaves_the_plan_is_labelled_and_commented_never_deleted_and_comes_back() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    issues = len(harness.linear.all_issues())
    harness.plan = crossing_plan(drop="TASK::A::4")

    gone = await harness.push()

    assert (gone.status, gone.counts.removed, gone.counts.commented) == (SUCCEEDED, 1, 1)
    leaving = harness.issue("TASK::A::4")
    assert len(harness.linear.all_issues()) == issues and "graphico-removed" in await label_names(harness, "TASK::A::4")
    assert [c.body for c in await harness.linear.comments(leaving.id)] == [
        "Graphico plan update for `TASK::A::4`. This item was removed from the Graphico plan."
    ]
    assert harness.store.items["TASK::A::4"].removed_at is not None
    writes = len(harness.linear.writes)
    assert (await harness.push()).counts.unchanged == 7 and len(harness.linear.writes) == writes

    harness.plan = crossing_plan()
    back = await harness.push()

    assert (back.status, back.counts.created) == (SUCCEEDED, 0)
    assert set(await label_names(harness, "TASK::A::4")) == {"graphico", "area-a"}
    assert "back in the Graphico plan" in (await harness.linear.comments(leaving.id))[-1].body
    assert harness.store.items["TASK::A::4"].removed_at is None and len(harness.linear.all_issues()) == issues


async def test_an_issue_that_left_the_plan_and_started_still_gets_the_removal_label() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    harness.linear.move_issue(harness.issue("TASK::A::4").id, "In Progress")
    harness.plan = crossing_plan(drop="TASK::A::4")

    await harness.push()

    assert "graphico-removed" in await label_names(harness, "TASK::A::4")


# Estimate ---------------------------------------------------------------------------------------------------------


async def test_an_estimate_the_team_refuses_is_left_out_and_recorded_not_failed() -> None:
    harness = Harness(crossing_plan(), estimate_allowed=lambda _points: False)

    result = await harness.push()

    assert (result.status, result.error_code, result.counts.created) == (SUCCEEDED, None, 8)
    assert (result.counts.estimate_refused, result.counts.failed) == (8, 0)
    assert {issue.estimate for issue in harness.linear.all_issues()} == {None}
    assert {row.error_code for row in harness.store.items.values()} == {ESTIMATE_REFUSED}
    writes = len(harness.linear.writes)
    assert (await harness.push()).counts.unchanged == 8 and len(harness.linear.writes) == writes


async def test_an_estimate_the_team_refuses_on_an_overwrite_is_left_out_too() -> None:
    harness = Harness(crossing_plan())
    await first_pass(harness)
    harness.linear.estimate_allowed = lambda _points: False
    harness.plan = crossing_plan(title="Renamed task")

    result = await harness.push()

    assert (result.status, result.counts.updated, result.counts.estimate_refused) == (SUCCEEDED, 1, 1)
    assert harness.issue("TASK::A::3").title == "Renamed task"
    assert harness.store.items["TASK::A::3"].error_code == ESTIMATE_REFUSED


# Rate limit, lost access and refusals -------------------------------------------------------------------------------


async def test_a_rate_limit_stops_the_pass_with_the_wait_and_the_next_pass_finishes_the_work() -> None:
    harness = Harness(crossing_plan())
    harness.linear.rate_limit_next(7, method="create_issue")

    stopped = await harness.push()

    assert (stopped.status, stopped.error_code, stopped.retry_after) == (FAILED, RATE_LIMITED, 7)
    assert stopped.counts.created == 0

    result = await harness.push()

    assert (result.status, result.counts.created) == (SUCCEEDED, 8)
    assert len(harness.linear.all_issues()) == 8


async def test_a_rate_limit_without_a_wait_is_retried_after_the_default() -> None:
    harness = Harness(crossing_plan())
    harness.linear.rate_limit_next(None, method="labels")

    stopped = await harness.push()

    assert (stopped.error_code, stopped.retry_after) == (RATE_LIMITED, 60.0)


async def test_a_project_with_no_binding_fails_with_no_linear_call() -> None:
    harness = Harness(crossing_plan())
    harness.fail(NotConnectedError())

    result = await harness.push()

    assert (result.status, result.error_code) == (FAILED, NOT_CONNECTED) and harness.linear.calls == []


async def test_an_expired_connection_asks_to_reconnect() -> None:
    harness = Harness(crossing_plan())
    harness.fail(LinearUnauthorizedError())

    result = await harness.push()

    assert (result.status, result.error_code) == (FAILED, RECONNECT_REQUIRED)


async def test_a_linear_project_that_is_gone_fails_the_pass_before_any_write() -> None:
    harness = Harness(crossing_plan())
    assert harness.store.target is not None
    harness.store.target = harness.store.target.__class__(
        **{**vars(harness.store.target), "linear_project_id": str(uuid.uuid4())}
    )

    result = await harness.push()

    assert (result.status, result.error_code) == (FAILED, PROJECT_NOT_FOUND) and harness.linear.writes == []


async def test_linear_refusing_one_issue_fails_that_item_and_the_pass_goes_on() -> None:
    harness = Harness(crossing_plan())
    harness.linear.fail_next(LinearInvalidError("title"), method="create_issue")

    result = await harness.push()

    assert (result.status, result.error_code, result.counts.failed) == (PARTIAL, None, 1)
    assert harness.store.items["STORY::A::ONE"].error_code == "linear_invalid"
    assert "TASK::A::1" not in harness.store.items  # waits for its story

    later = await harness.push()

    assert (later.status, later.counts.failed) == (SUCCEEDED, 0) and len(harness.linear.all_issues()) == 8


@pytest.mark.parametrize(
    ("plan", "enabled", "expected"),
    [
        (crossing_plan(), False, (SKIPPED, NOT_CONFIGURED)),
        (LoadedPlan(CurrentPlan(plan=None)), True, (SKIPPED, NO_PLAN)),
        (make_plan([story(STORIES[0])], [task("TASK::A::1")], applying=True), True, (SKIPPED, PLAN_APPLYING)),
    ],
)
async def test_a_pass_with_nothing_to_export_is_skipped_with_its_reason(
    plan: Any, expected: Any, *, enabled: bool
) -> None:
    harness = Harness(plan, enabled=enabled)

    result = await harness.push()

    assert (result.status, result.error_code) == expected and harness.linear.calls == []


async def test_a_destination_changed_during_the_pass_stops_it_without_a_write_to_the_new_one() -> None:
    harness = Harness(crossing_plan())
    harness.store.changed = True

    result = await harness.push()

    assert (result.status, result.error_code) == (SKIPPED, TARGET_CHANGED)
