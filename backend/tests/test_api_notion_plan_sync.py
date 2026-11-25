"""One Notion plan pass (docs/notion/plan.md "Rules of a pass"): the database and its relations, the creates, the
to-do rule, comments, removed items, idempotence, the row cap, crash repair and the closed run codes. Notion is the
fake gateway of ``notion_plan_support``; the rows are in memory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.notion_plan.sync import (
    MAX_ROWS_PER_PASS,
    NO_PLAN,
    NOT_CONFIGURED,
    PLAN_APPLYING,
    TARGET_CHANGED,
    UNEXPECTED_ERROR,
    PushCounts,
    push_once,
    row_body,
    row_properties,
    to_do_options,
)
from db.models import NotionPlanItem
from graph.plan.reader import LoadedPlan
from jira_sync_support import T0, edit_story, edit_task, make_plan, story, task
from notion.reasons import NotionCode, NotionError
from notion_plan_support import DONE, IN_PROGRESS, NOT_STARTED, STATUS_SCHEMA, Harness

STORY_ONE, STORY_TWO = "STORY::A::ONE", "STORY::A::TWO"
NODES = [STORY_ONE, "TASK::A::1", "TASK::A::2", "STORY::A::THREE", "TASK::A::5", STORY_TWO, "TASK::A::3", "TASK::A::4"]


def counts(**values: int) -> PushCounts:
    return PushCounts(**values)


async def first_pass(harness: Harness) -> None:
    result = await harness.push()
    assert (result.status, result.error_code) == ("succeeded", None)


def without_tasks(plan, *task_ids: str):
    kept = tuple(t for t in plan.current.tasks if t.task_id not in task_ids)
    return type(plan)(plan.current.model_copy(update={"tasks": kept}))


# The first pass ---------------------------------------------------------------------------------------------------


async def test_the_first_pass_makes_the_database_under_the_parent_page_and_one_row_per_item() -> None:
    harness = Harness()

    result = await harness.push()

    assert (result.status, result.error_code) == ("succeeded", None)
    assert result.counts == counts(created=8)
    [create] = harness.gateway.called("create_database")
    assert create.args[:2] == (harness.store.target.parent_page_id, "Graphico plan · Shop")
    assert set(create.args[2]) == {"Name", "Type", "Status", "Points", "Work area", "Graphico ID", "Removed"}
    assert create.args[2]["Status"] == {"status": {}}
    [saved] = harness.store.saved
    assert harness.store.target.database_url and harness.store.target.data_source_id == saved["data_source_id"]
    assert sorted(harness.store.items) == sorted(NODES)
    assert all(item.page_id and item.content_hash and item.error_code is None for item in harness.store.items.values())


async def test_the_relations_are_added_after_the_create_and_their_synced_sides_are_renamed() -> None:
    harness = Harness()

    await first_pass(harness)

    writes = harness.gateway.writes()
    assert writes[0] == "create_database" and writes[1:5] == ["update_data_source"] * 4
    updates = [call.args[1] for call in harness.gateway.called("update_data_source")]
    source_id = harness.store.target.data_source_id
    assert updates[0] == {"Parent": {"relation": {"data_source_id": source_id, "dual_property": {}}}}
    assert updates[1] == {"synced:Parent": {"name": "Children"}}
    assert updates[2] == {"Blocked by": {"relation": {"data_source_id": source_id, "dual_property": {}}}}
    assert updates[3] == {"synced:Blocked by": {"name": "Blocks"}}
    assert {"Children", "Blocks", "Parent", "Blocked by"} <= set(harness.gateway.source().properties)


async def test_a_row_carries_its_properties_body_and_links_to_other_rows() -> None:
    harness = Harness()

    await first_pass(harness)

    gateway = harness.gateway
    assert gateway.row_text("TASK::A::1", "Graphico ID") == "TASK::A::1"
    assert (gateway.row_text(STORY_ONE, "Type"), gateway.row_text("TASK::A::1", "Type")) == ("Story", "Task")
    assert gateway.row_text("TASK::A::1", "Name") == "Task TASK::A::1"
    assert gateway.relation("TASK::A::1", "Parent") == [harness.page_of(STORY_ONE)]
    assert gateway.relation(STORY_ONE, "Parent") == []
    assert gateway.relation("TASK::A::2", "Blocked by") == [harness.page_of("TASK::A::1")]  # set by the link fix-up
    assert gateway.row("TASK::A::1").properties["Points"]["number"] == 1
    assert gateway.row("TASK::A::1").properties["Removed"]["checkbox"] is False
    assert "Status" not in gateway.called("create_page")[0].kwargs["properties"]  # Notion's default is the first To-do
    body = gateway.markdown[gateway.row("TASK::A::1").id].markdown
    assert "- [ ] it works" in body and "**Phase:** logic" in body and "`TASK::A::1`" in body


async def test_a_second_pass_changes_nothing_and_asks_notion_nothing() -> None:
    harness = Harness()
    await first_pass(harness)
    calls, asked = len(harness.gateway.calls), harness.gateway_asked

    result = await harness.push()

    assert (result.status, result.counts) == ("succeeded", counts(unchanged=8))
    assert len(harness.gateway.calls) == calls and harness.gateway_asked == asked
    assert harness.restarts == []


async def test_the_first_pass_asks_for_no_graphico_id_because_no_row_is_pending() -> None:
    harness = Harness()

    await first_pass(harness)

    assert harness.gateway.called("query_data_source") == []


# The to-do rule -----------------------------------------------------------------------------------------------------


async def test_a_row_in_the_to_do_group_takes_the_plans_properties_and_body() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.gateway.set_status("TASK::A::3", NOT_STARTED)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed", description="New text")
    mark = len(harness.gateway.calls)

    result = await harness.push()

    assert result.counts == counts(updated=1, unchanged=7)
    assert [call.method for call in harness.gateway.calls[mark:] if call.method not in ("data_source", "database")] == [
        "page",
        "update_page",
        "replace_markdown",
    ]
    assert harness.gateway.row_text("TASK::A::3", "Name") == "Renamed"
    assert "New text" in harness.gateway.markdown[harness.gateway.row("TASK::A::3").id].markdown
    assert harness.gateway.comments == []


async def test_a_started_row_gets_one_comment_and_is_left_alone() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.gateway.set_status("TASK::A::3", IN_PROGRESS)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed", phase="ui")
    mark = len(harness.gateway.calls)

    result = await harness.push()

    assert result.counts == counts(commented=1, unchanged=7)
    [(page_id, text)] = harness.gateway.comments
    assert page_id == harness.page_of("TASK::A::3")
    assert "- Name" in text and "- Phase" in text and "- Description" not in text
    assert harness.gateway.row_text("TASK::A::3", "Name") == "Task TASK::A::3"
    assert {c.method for c in harness.gateway.calls[mark:]} & {"update_page", "replace_markdown"} == set()
    again = await harness.push()
    assert again.counts == counts(unchanged=8) and len(harness.gateway.comments) == 1  # once per change


@pytest.mark.parametrize("option", [DONE, "opt-unknown", None])
async def test_a_status_that_is_done_or_cannot_be_placed_counts_as_started(option: str | None) -> None:
    harness = Harness()
    await first_pass(harness)
    page = harness.gateway.row("TASK::A::4")
    status = {"id": "Status", "type": "status", "status": None if option is None else {"id": option, "name": "x"}}
    harness.gateway.pages[page.id] = replace(page, properties={**page.properties, "Status": status})
    harness.plan = edit_task(harness.plan, "TASK::A::4", title="Renamed")

    result = await harness.push()

    assert result.counts == counts(commented=1, unchanged=7)


async def test_a_status_option_is_found_by_its_group_so_a_renamed_to_do_option_still_counts() -> None:
    schema = {"Status": {**STATUS_SCHEMA, "status": {**STATUS_SCHEMA["status"], "options": []}}}

    assert to_do_options(schema) == {NOT_STARTED}
    assert to_do_options({}) == frozenset()


# Removed items -----------------------------------------------------------------------------------------------------


async def test_an_item_that_leaves_the_plan_is_marked_removed_and_commented_never_deleted() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = without_tasks(harness.plan, "TASK::A::4")
    page = harness.page_of("TASK::A::4")

    result = await harness.push()

    assert result.counts == counts(removed=1, updated=1, unchanged=6)  # the story's points moved with its task
    assert harness.gateway.pages[page].properties["Removed"]["checkbox"] is True
    assert harness.gateway.comments == [(page, "This item was removed from the Graphico plan.")]
    assert harness.store.items["TASK::A::4"].removed_at is not None and not harness.gateway.pages[page].in_trash
    assert [c for c in harness.gateway.calls if "delete" in c.method or "archive" in c.method] == []
    again = await harness.push()
    assert again.counts == counts(unchanged=7) and len(harness.gateway.comments) == 1


async def test_an_item_that_returns_loses_the_check_and_gets_a_comment() -> None:
    harness = Harness()
    await first_pass(harness)
    plan = harness.plan
    harness.plan = without_tasks(plan, "TASK::A::4")
    await harness.push()
    harness.plan = plan

    result = await harness.push()

    page = harness.page_of("TASK::A::4")
    assert result.counts == counts(updated=2, unchanged=6)  # the revived row and its story's points
    assert harness.gateway.pages[page].properties["Removed"]["checkbox"] is False
    assert harness.gateway.comments[-1] == (page, "This item is back in the Graphico plan.")
    assert harness.store.items["TASK::A::4"].removed_at is None


async def test_a_row_a_person_trashed_is_never_made_again() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.gateway.trash("TASK::A::4")
    harness.plan = edit_task(harness.plan, "TASK::A::4", title="Renamed")

    first = await harness.push()
    second = await harness.push()

    assert first.counts == counts(removed=1, unchanged=7)
    assert harness.store.items["TASK::A::4"].error_code == NotionCode.PAGE_GONE.value
    assert second.counts == counts(unchanged=7)
    assert len(harness.gateway.called("create_page")) == 8


# The row cap -------------------------------------------------------------------------------------------------------


def many_stories(count: int):
    return make_plan([story(f"STORY::M::{i:03d}") for i in range(count)], [])


async def test_a_pass_writes_at_most_the_row_cap_and_asks_for_one_more() -> None:
    harness = Harness(many_stories(MAX_ROWS_PER_PASS + 10))

    first = await harness.push()

    assert (first.status, first.counts.created, first.counts.deferred) == ("succeeded", MAX_ROWS_PER_PASS, 10)
    assert harness.restarts == [str(harness.project_id)]
    second = await harness.push()
    assert (second.counts.created, second.counts.deferred, second.counts.unchanged) == (10, 0, MAX_ROWS_PER_PASS)
    assert harness.restarts == [str(harness.project_id)]  # no more rows: no more restarts
    assert len(harness.gateway.called("create_page")) == MAX_ROWS_PER_PASS + 10


async def test_updates_and_comments_count_towards_the_cap_too() -> None:
    plan = many_stories(MAX_ROWS_PER_PASS + 5)
    harness = Harness(plan)
    await harness.push()
    await harness.push()
    for node in list(harness.store.items):
        harness.gateway.set_status(node, NOT_STARTED)
    for index in range(MAX_ROWS_PER_PASS + 5):
        plan = edit_story(plan, f"STORY::M::{index:03d}", summary=f"Changed {index}")
    harness.plan = plan
    harness.restarts.clear()

    result = await harness.push()

    assert (result.counts.updated, result.counts.deferred) == (MAX_ROWS_PER_PASS, 5)
    assert harness.restarts == [str(harness.project_id)]


# Crash repair ------------------------------------------------------------------------------------------------------


async def test_a_pending_row_is_looked_up_by_graphico_id_and_taken_instead_of_made_twice() -> None:
    harness = Harness()
    await first_pass(harness)
    item = harness.store.items["TASK::A::3"]
    existing = item.page_id
    item.page_id = item.content = item.content_hash = None  # the crash: the page exists, its row has no page id
    rows = len(harness.gateway.rows[harness.gateway.source().id])

    result = await harness.push()

    [query] = harness.gateway.called("query_data_source")
    assert query.kwargs["where"] == {"property": "Graphico ID", "rich_text": {"equals": "TASK::A::3"}}
    assert harness.store.items["TASK::A::3"].page_id == existing
    assert len(harness.gateway.rows[harness.gateway.source().id]) == rows
    assert (result.counts.created, result.counts.updated) == (0, 1)  # the taken row is brought to the plan
    assert harness.store.items["TASK::A::3"].content_hash


async def test_a_pending_row_with_no_page_in_notion_is_made() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.store.items["TASK::A::3"] = NotionPlanItem(
        project_id=harness.project_id, node_id="TASK::A::3", kind="task", requested_at=T0
    )
    page = harness.gateway.row("TASK::A::3")
    harness.gateway.rows[harness.gateway.source().id].remove(page.id)

    result = await harness.push()

    assert result.counts.created == 1 and len(harness.gateway.called("query_data_source")) == 1
    assert harness.store.items["TASK::A::3"].page_id


# Failures ----------------------------------------------------------------------------------------------------------


async def test_a_row_notion_refuses_fails_that_item_and_the_next_pass_tries_it_again() -> None:
    harness = Harness()
    harness.gateway.fail("create_page", NotionCode.ITEM_INVALID)

    first = await harness.push()

    assert (first.status, first.error_code) == ("partial", None)
    assert (first.counts.failed, first.counts.created) == (1, 5)  # the refused story's tasks wait for it
    assert harness.store.items[STORY_ONE].error_code == NotionCode.ITEM_INVALID.value
    assert "TASK::A::1" not in harness.store.items or harness.store.items["TASK::A::1"].page_id is None
    second = await harness.push()
    assert (second.status, second.counts.failed) == ("succeeded", 0)
    assert harness.store.items[STORY_ONE].page_id and harness.store.items[STORY_ONE].error_code is None


async def test_a_rate_limit_ends_the_pass_with_its_code_and_the_next_pass_continues() -> None:
    harness = Harness()
    harness.gateway.fail("create_page", NotionCode.RATE_LIMITED)

    first = await harness.push()

    assert (first.status, first.error_code) == ("failed", "notion_rate_limited")
    assert first.counts.created == 0 and harness.restarts == []
    second = await harness.push()
    assert (second.status, second.counts.created) == ("succeeded", 8)
    assert len(harness.gateway.called("query_data_source")) == 1  # the story the refused create left pending


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (NotionCode.PERMISSIONS_MISSING, "notion_permissions_missing"),
        (NotionCode.UNREACHABLE, "notion_unreachable"),
        (NotionCode.NOT_A_PAGE, "notion_parent_not_a_page"),
        (NotionCode.GRANT_INVALID, "notion_auth_failed"),
    ],
)
async def test_a_refusal_of_the_database_create_is_one_closed_run_code(code: NotionCode, expected: str) -> None:
    harness = Harness()
    harness.gateway.fail("create_database", code)

    result = await harness.push()

    assert (result.status, result.error_code) == ("failed", expected)
    assert harness.store.saved == [] and harness.gateway.called("create_page") == []


async def test_a_write_that_finds_nothing_in_a_database_that_is_there_is_a_share_taken_back() -> None:
    harness = Harness()
    harness.gateway.fail("create_page", NotionCode.NOT_SHARED)

    result = await harness.push()

    assert result.error_code == "notion_not_shared"
    assert harness.store.resets == 0 and harness.store.target.database_id


def trash_database(harness: Harness) -> None:
    database = harness.gateway.databases[str(harness.store.target.database_id)]
    harness.gateway.databases[database.id] = replace(database, in_trash=True)


@pytest.mark.parametrize("how", ["trashed", "unshared"])
async def test_a_trashed_or_unshared_database_stops_the_pass_before_any_row_is_written_and_is_forgotten(how) -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed")
    if how == "trashed":
        trash_database(harness)
    else:
        harness.gateway.unshare(str(harness.store.target.database_id))
    mark = len(harness.gateway.calls)

    result = await harness.push()

    assert (result.status, result.error_code) == ("failed", "notion_database_gone")
    assert {c.method for c in harness.gateway.calls[mark:]} == {"database"}
    assert harness.store.target.database_id is None and harness.store.items == {} and harness.store.resets == 1


async def test_the_next_pass_after_a_lost_database_makes_a_new_one_with_all_the_rows() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed")
    trash_database(harness)
    await harness.push()

    again = await harness.push()

    assert (again.status, again.counts.created) == ("succeeded", 8)
    assert len(harness.gateway.called("create_database")) == 2 and len(harness.store.saved) == 2


async def test_a_database_that_fails_a_write_and_is_in_the_trash_is_lost_too() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed")
    harness.gateway.fail("page", NotionCode.NOT_SHARED)
    trash_database(harness)
    harness.gateway.fail("database", NotionCode.UNREACHABLE)  # the pass-start check is not what finds it

    first = await harness.push()
    assert first.error_code == "notion_unreachable"
    gone = await harness.push()
    assert gone.error_code == "notion_database_gone" and harness.store.resets == 1


async def test_a_target_with_a_database_but_no_data_source_resolves_it_and_makes_no_second_database() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.store.target = replace(harness.store.target, data_source_id=None)
    harness.plan = edit_task(harness.plan, "TASK::A::3", title="Renamed")

    result = await harness.push()

    assert result.status == "succeeded" and len(harness.gateway.called("create_database")) == 1
    assert harness.store.target.data_source_id == harness.gateway.source().id
    assert result.counts.commented + result.counts.updated == 1


async def test_a_trashed_row_is_settled_when_it_leaves_the_plan_not_failed_in_every_pass() -> None:
    harness = Harness()
    await first_pass(harness)
    harness.gateway.trash("TASK::A::4")
    harness.plan = without_tasks(harness.plan, "TASK::A::4")

    first = await harness.push()
    second = await harness.push()

    assert (first.status, first.counts.removed, first.counts.failed) == ("succeeded", 1, 0)
    assert harness.store.items["TASK::A::4"].error_code == NotionCode.PAGE_GONE.value
    assert second.counts == counts(unchanged=7) and harness.gateway.comments == []


async def test_the_relations_a_crash_left_out_are_added_by_the_next_pass_before_any_row_is_written() -> None:
    harness = Harness()
    harness.gateway.fail("update_data_source", NotionCode.RATE_LIMITED)
    first = await harness.push()
    assert first.error_code == "notion_rate_limited" and harness.gateway.called("create_page") == []

    second = await harness.push()

    assert (second.status, second.counts.created) == ("succeeded", 8)
    assert {"Parent", "Blocked by", "Children", "Blocks"} <= set(harness.gateway.source().properties)
    assert len(harness.gateway.called("create_database")) == 1


async def test_a_member_without_a_connection_ends_the_pass_before_any_notion_call() -> None:
    harness = Harness()
    harness.gateway_error = NotionError(NotionCode.RECONNECT_REQUIRED)

    result = await harness.push()

    assert (result.status, result.error_code) == ("failed", "notion_reconnect_required")
    assert harness.gateway.calls == [] and harness.store.items == {}


async def test_an_unexpected_error_is_a_closed_code_and_a_changed_destination_stops_the_pass() -> None:
    harness = Harness()
    boom = Harness()

    async def broken(_pid: str) -> LoadedPlan:
        raise RuntimeError("secret text")

    crashed = await push_once(str(boom.project_id), replace(boom.deps(), load_plan=broken))
    harness.store.changed = True
    stopped = await harness.push()

    assert (crashed.status, crashed.error_code) == ("failed", UNEXPECTED_ERROR)
    assert (stopped.status, stopped.error_code) == ("skipped", TARGET_CHANGED)


# Skips -------------------------------------------------------------------------------------------------------------


async def test_a_pass_without_an_enabled_target_a_plan_or_with_an_applying_plan_is_skipped() -> None:
    disabled = Harness(enabled=False)
    empty = Harness()
    empty.plan = type(empty.plan)(empty.plan.current.model_copy(update={"plan": None}))
    applying = Harness(make_plan([story("STORY::X::1")], [task("TASK::X::1", "STORY::X::1")], applying=True))

    results = [await h.push() for h in (disabled, empty, applying)]

    assert [(r.status, r.error_code) for r in results] == [
        ("skipped", NOT_CONFIGURED),
        ("skipped", NO_PLAN),
        ("skipped", PLAN_APPLYING),
    ]
    assert all(h.gateway.calls == [] for h in (disabled, empty, applying))


# Pure parts --------------------------------------------------------------------------------------------------------


def test_the_properties_of_a_story_and_a_task_follow_the_schema() -> None:
    values = {
        "summary": "A title",
        "task_id": None,
        "points": 3,
        "labels": ["area-billing", "specflow"],
        "parent": "-",
        "blocked_by": [],
    }

    properties = row_properties(values, "STORY::A", ["page-2"])

    assert properties["Type"] == {"select": {"name": "Story"}}
    assert properties["Work area"] == {"multi_select": [{"name": "billing"}]}
    assert properties["Parent"] == {"relation": []} and properties["Blocked by"] == {"relation": [{"id": "page-2"}]}
    task_values = {**values, "task_id": "TASK::A::1", "parent": "page-1", "points": None}
    task_properties = row_properties(task_values, "TASK::A::1", [])
    assert task_properties["Type"] == {"select": {"name": "Task"}}
    assert task_properties["Parent"] == {"relation": [{"id": "page-1"}]} and task_properties["Points"] == {
        "number": None
    }


def test_the_body_lists_the_brief_and_ends_with_the_plan_item() -> None:
    values = {
        "text": "Why.",
        "release": "R1",
        "phase": None,
        "done_when": ["a", "b"],
        "specs": [{"id": "S1", "name": "Spec one"}],
    }

    assert row_body(values, "TASK::A::1") == (
        "Why.\n\n**Release:** R1\n\n**Done when**\n- [ ] a\n- [ ] b\n\n**Implements:** Spec one (S1)"
        "\n\n_Plan item `TASK::A::1`, kept up to date by Graphico._"
    )
