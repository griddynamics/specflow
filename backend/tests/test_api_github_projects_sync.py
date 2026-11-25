"""The GitHub Projects pass (docs/github/projects.md "Pass"): create, update in To Do, comment past To Do, removal,
reorder, rate limit and lost access, on a fake GitHub and in-memory rows."""

from __future__ import annotations

from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.github_projects.sync import NO_PLAN, NOT_CONFIGURED, PLAN_APPLYING, REMOVED_LABEL, issue_content
from github.codes import GitHubCode, GitHubError
from github_projects_support import Harness
from graph.plan.models import CurrentPlan
from graph.plan.reader import LoadedPlan
from jira_sync_support import basic_plan, make_plan, story, task

STORIES = ["STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"]


def plan_with(*, drop: str | None = None, title: str | None = None, waits: list[tuple[str, str]] | None = None):
    tasks = [
        task("TASK::A::1", complexity="low", done_when=({"text": "it works", "source": "planner"},)),
        task("TASK::A::2", description="Second.\n\nMore."),
        task("TASK::A::3", "STORY::A::TWO", complexity="high", title=title or "Task TASK::A::3"),
        task("TASK::A::4", "STORY::A::TWO"),
        task("TASK::A::5", "STORY::A::THREE", "completed"),
    ]
    tasks = [t for t in tasks if t.task_id != drop]
    return make_plan([story(s) for s in STORIES], tasks, waits=waits or [("TASK::A::2", "TASK::A::1")])


async def first_pass(harness: Harness) -> None:
    result = await harness.push()
    assert (result.status, result.error_code) == (SUCCEEDED, None)


async def test_the_first_pass_makes_issues_for_stories_and_sub_issues_for_tasks() -> None:
    harness = Harness()

    result = await harness.push()

    assert (result.status, result.error_code, result.counts.created, result.counts.failed) == (SUCCEEDED, None, 8, 0)
    gateway = harness.gateway
    assert len(gateway.issues) == 8 and len(gateway.items) == 3  # stories only are items of the project
    one = gateway.number_of("Story STORY::A::ONE")
    first = gateway.issues[gateway.number_of("Task TASK::A::1")]
    assert first.parent == one and gateway.issues[one].parent is None
    assert "specflow" in gateway.issues[one].labels and "area-a" in gateway.issues[one].labels
    assert "- [ ] it works" in first.body and "TASK::A::1" in first.body
    rows = harness.store.items
    assert rows["STORY::A::ONE"].item_id is not None and rows["TASK::A::1"].item_id is None
    in_order = sorted(STORIES, key=lambda story_id: rows[story_id].position or 0)
    assert [rows[story_id].position for story_id in in_order] == [0, 1, 2]
    assert [gateway.item_issue[item] for item in gateway.items] == [gateway.number_of(f"Story {s}") for s in in_order]


async def test_a_second_pass_with_nothing_changed_writes_nothing() -> None:
    harness = Harness()
    await first_pass(harness)
    calls = len(harness.gateway.calls)

    result = await harness.push()

    assert (result.status, result.counts.unchanged, result.counts.created) == (SUCCEEDED, 8, 0)
    assert len(harness.gateway.calls) == calls


async def test_an_open_issue_that_is_not_started_takes_the_plan_fields() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = plan_with(title="Renamed task")

    result = await harness.push()

    assert (result.counts.updated, result.counts.commented) == (1, 0)
    [number] = [n for n, issue in harness.gateway.issues.items() if issue.title == "Renamed task"]
    assert harness.gateway.issues[number].comments == []


async def test_an_issue_past_to_do_gets_one_comment_and_is_left_as_it_is() -> None:
    harness = Harness()
    await first_pass(harness)
    number = harness.gateway.number_of("Task TASK::A::3")
    harness.gateway.issues[number].status = "In progress"
    harness.plan = plan_with(title="Renamed task")

    result = await harness.push()
    again = await harness.push()

    issue = harness.gateway.issues[number]
    assert (result.counts.commented, result.counts.updated) == (1, 0)
    assert issue.title == "Task TASK::A::3" and len(issue.comments) == 1 and "Title changed." in issue.comments[0]
    assert (again.counts.commented, again.counts.unchanged) == (0, 8)  # the stored content moved on: no repeat


async def test_a_closed_issue_is_commented_not_edited() -> None:
    harness = Harness()
    await first_pass(harness)
    number = harness.gateway.number_of("Task TASK::A::3")
    harness.gateway.issues[number].open = False
    harness.plan = plan_with(title="Renamed task")

    result = await harness.push()

    assert (result.counts.commented, result.counts.updated) == (1, 0)


async def test_an_item_that_leaves_the_plan_is_labelled_and_commented_never_deleted_and_comes_back() -> None:
    harness = Harness()
    await first_pass(harness)
    number = harness.gateway.number_of("Task TASK::A::4")
    harness.plan = plan_with(drop="TASK::A::4")

    left = await harness.push()

    issue = harness.gateway.issues[number]
    assert left.counts.removed == 1 and REMOVED_LABEL in issue.labels and issue.open and not issue.deleted
    assert "removed from the Graphico plan" in issue.comments[0]
    assert harness.store.items["TASK::A::4"].removed_at is not None
    assert (await harness.push()).counts.removed == 0  # once

    harness.plan = plan_with()
    back = await harness.push()

    assert (
        back.counts.created == 0
        and REMOVED_LABEL not in issue.labels
        and "back in the Graphico plan" in issue.comments[1]
    )
    assert harness.store.items["TASK::A::4"].removed_at is None


async def test_a_changed_story_order_moves_the_items_and_stores_the_new_positions() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = plan_with(waits=[("TASK::A::1", "TASK::A::3")])  # story ONE now waits for story TWO

    result = await harness.push()

    gateway, rows = harness.gateway, harness.store.items
    order = [gateway.item_issue[item] for item in gateway.items]
    one, two = gateway.number_of("Story STORY::A::ONE"), gateway.number_of("Story STORY::A::TWO")
    assert result.counts.ranked >= 1 and "move_item" in gateway.names()
    assert order.index(two) < order.index(one)
    assert sorted(STORIES, key=lambda story_id: rows[story_id].position or 0) == [
        story_id for item in gateway.items for story_id in STORIES if rows[story_id].item_id == item
    ]
    assert (await harness.push()).counts.ranked == 0


async def test_a_rate_limit_ends_the_pass_failed_with_one_closed_code() -> None:
    harness = Harness()
    harness.gateway.fail["create_issue"] = GitHubError(GitHubCode.UNREACHABLE)

    result = await harness.push()

    assert (result.status, result.error_code, result.counts.created) == (FAILED, "github_unreachable", 0)
    harness.gateway.fail.clear()
    assert (await harness.push()).status == SUCCEEDED  # the next pass starts over from the pending row


async def test_lost_access_ends_the_pass_with_no_github_call() -> None:
    harness = Harness()
    harness.gateway_error = GitHubError(GitHubCode.RECONNECT_REQUIRED)

    result = await harness.push()

    assert (result.status, result.error_code) == (FAILED, "github_reconnect_required")
    assert harness.gateway.calls == [] and harness.store.items == {}


async def test_a_repository_the_account_lost_ends_the_pass_with_its_code() -> None:
    harness = Harness()
    harness.gateway.fail["create_issue"] = GitHubError(GitHubCode.REPO_NOT_ACCESSIBLE)

    assert (await harness.push()).error_code == "github_repo_not_accessible"


async def test_an_installation_that_did_not_approve_the_permissions_is_one_closed_code() -> None:
    harness = Harness()
    harness.gateway.fail["create_issue"] = GitHubError(GitHubCode.PERMISSIONS_MISSING)

    result = await harness.push()

    assert (result.status, result.error_code) == (FAILED, "github_permissions_missing")


async def test_an_unknown_refusal_is_reported_as_a_closed_code_not_its_text() -> None:
    harness = Harness()
    harness.gateway.fail["create_issue"] = GitHubError(GitHubCode.REFUSED)

    assert (await harness.push()).error_code == "github_refused"


async def test_an_issue_github_refuses_fails_that_item_and_the_rest_is_made() -> None:
    harness = Harness()
    harness.gateway.refuse_title = "Task TASK::A::4"

    result = await harness.push()

    assert (result.status, result.counts.failed, result.counts.created) == (PARTIAL, 1, 7)
    assert harness.store.items["TASK::A::4"].error_code == "github_item_invalid"
    assert (await harness.push()).counts.failed == 1  # asked again, refused again


async def test_a_story_that_stopped_before_its_item_is_attached_on_the_next_pass_without_a_second_issue() -> None:
    harness = Harness()
    harness.gateway.fail["add_item"] = GitHubError(GitHubCode.UNREACHABLE)
    assert (await harness.push()).status == FAILED
    assert len(harness.gateway.issues) == 1  # the first story, made before its item failed
    harness.gateway.fail.clear()

    result = await harness.push()

    assert result.status == SUCCEEDED and result.counts.created == 7
    assert len(harness.gateway.issues) == 8 and len(harness.gateway.items) == 3
    assert len({issue.title for issue in harness.gateway.issues.values()}) == 8


async def test_an_issue_a_person_deleted_is_not_made_again() -> None:
    harness = Harness()
    await first_pass(harness)
    number = harness.gateway.number_of("Task TASK::A::3")
    harness.gateway.issues[number].deleted = True
    harness.plan = plan_with(title="Renamed task")

    result = await harness.push()
    again = await harness.push()

    row = harness.store.items["TASK::A::3"]
    assert result.counts.removed == 1 and row.removed_at is not None and row.error_code == "github_issue_gone"
    assert (again.counts.created, again.counts.removed) == (0, 0) and len(harness.gateway.issues) == 8


async def test_a_pass_skips_without_an_enabled_target_a_plan_or_while_the_plan_applies() -> None:
    off = Harness(enabled=False)
    assert (await off.push()).error_code == NOT_CONFIGURED
    none = Harness()
    none.store.target = None
    assert (await none.push()).error_code == NOT_CONFIGURED
    empty = Harness(LoadedPlan(CurrentPlan(plan=None)))
    assert (await empty.push()).error_code == NO_PLAN
    applying = Harness(make_plan([story("STORY::A::ONE")], [], applying=True))
    result = await applying.push()
    assert (result.status, result.error_code) == (SKIPPED, PLAN_APPLYING)
    assert applying.gateway.calls == []


async def test_a_destination_changed_during_the_pass_stops_it_without_more_writes() -> None:
    harness = Harness()
    harness.store.changed = True

    result = await harness.push()

    assert (result.status, result.error_code) == (SKIPPED, "target_changed")
    assert harness.gateway.writes() == []


def test_the_issue_holds_the_plan_fields_and_the_node_id() -> None:
    content = issue_content(
        {
            "summary": "A title",
            "text": "Body.",
            "release": "R1",
            "phase": "logic",
            "done_when": ["x", "y"],
            "specs": [{"id": "SPEC::1", "name": "Cart"}],
            "labels": ["specflow", "area-a"],
        },
        "TASK::A::1",
    )

    assert content.title == "A title" and list(content.labels) == ["specflow", "area-a"]
    for text in ("Body.", "**Release:** R1", "- [ ] x", "- [ ] y", "Cart (SPEC::1)", "`TASK::A::1`"):
        assert text in content.body


def test_the_basic_plan_has_the_stories_the_tests_name() -> None:
    assert [s.story_id for s in basic_plan().current.stories] == STORIES
