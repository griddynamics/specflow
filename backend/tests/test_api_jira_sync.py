"""The sync engine on the fake Jira and the fake repository: FR-JIRA-MAP-01..11, UPD-01..09, REL-01..03, 05, RUN-06,
RUN-08 (engine side), NFR-JIRA-02, NFR-JIRA-04. Every pass runs with ``on_call`` asserting no open transaction."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from api.jira import content as rules
from api.jira.desired import EPIC
from api.jira.rank import plan_moves
from api.jira.sync import SEARCH_INDEX_LAG_SECONDS, sync_once
from graph.plan.models import CurrentPlan
from graph.plan.reader import LoadedPlan
from jira.reasons import Reason
from jira_sync_support import (
    EPIC_KEY,
    Harness,
    comment_items,
    comment_lead,
    edit_story,
    edit_task,
    make_plan,
    story,
    task,
    with_plan,
)

S1, S2, S3 = "STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"
T1, T2, T3, T4, T5 = (f"TASK::A::{n}" for n in range(1, 6))
IN_PROGRESS = ("indeterminate", "In Progress")
ALLOWED_CALLS = {
    "project",
    "story_points_field",
    "create_issue",
    "edit_issue",
    "issue_states",
    "find_created",
    "add_comment",
    "add_blocks_link",
    "delete_link",
    "rank",
    "upsert_remote_link",
}
WRITES = {"add_pending", "update_issue", "set_positions"}


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


async def synced(harness: Harness) -> Harness:
    result = await harness.sync()
    assert (result.status, result.error_code) == ("succeeded", None)
    return harness


def names(harness: Harness, mark: int = 0) -> list[str]:
    return harness.calls_since(mark)


def edits(harness: Harness, node: str) -> list[dict]:
    return [call for call in harness.gateway.called("edit_issue") if call["key"] == harness.key(node)]


def comments(harness: Harness, node: str) -> list[dict]:
    return harness.gateway.comments.get(harness.key(node), [])


async def test_the_harness_rejects_a_jira_call_made_inside_a_transaction(h):
    async with h.db.transaction():
        with pytest.raises(AssertionError, match="transaction is open"):
            await h.gateway.myself()


# --- the first sync (FR-JIRA-MAP-01..07)


async def test_the_first_sync_creates_the_epic_stories_and_sub_tasks_without_rank_calls(h):
    result = await h.sync()
    assert (result.status, result.error_code) == ("succeeded", None)
    counts = result.counts
    assert (counts.created, counts.links_added, counts.ranked, counts.failed, counts.unchanged) == (9, 1, 0, 0, 0)
    assert h.gateway.called("rank") == [] and set(names(h)) <= ALLOWED_CALLS
    created = [call["node_id"] for call in h.gateway.called("create_issue")]
    assert created == [EPIC, S1, T1, T2, S3, T5, S2, T3, T4]  # each story, then its sub-tasks
    assert names(h)[:2] == ["project", "story_points_field"]

    bodies = {call["node_id"]: call["fields"] for call in h.gateway.called("create_issue")}
    assert set(bodies[EPIC]) == {"project", "issuetype", "summary", "labels"}
    assert bodies[EPIC]["summary"] == "Shop" and bodies[EPIC]["labels"] == ["specflow"]
    for node in (S1, S2, S3, T1, T2, T3, T4, T5):
        assert set(bodies[node]) == {
            "project",
            "issuetype",
            "summary",
            "labels",
            "parent",
            "description",
            "customfield_90001",
        }
        assert bodies[node]["project"] == {"key": "PRJ"} and bodies[node]["labels"] == ["area-a", "specflow"]
    assert [bodies[node]["issuetype"]["id"] for node in (EPIC, S1, T1)] == ["10000", "10001", "10003"]
    assert {bodies[node]["parent"]["key"] for node in (S1, S2, S3)} == {h.key(EPIC)}
    assert bodies[T1]["parent"]["key"] == h.key(S1) and bodies[T4]["parent"]["key"] == h.key(S2)
    assert [bodies[node]["customfield_90001"] for node in (T1, T2, T3, S1, S2, S3)] == [1, 3, 5, 4, 8, 3]
    assert h.gateway.called("add_blocks_link") == [{"key": h.key(T2), "prerequisite": h.key(T1), "waiter": h.key(T2)}]
    for node in (EPIC, S1, T1):
        row = h.row(node)
        assert row.content_hash == rules.digest(row.content) and row.synced_at is not None and row.removed_at is None
    assert h.row(T2).content["blocked_by"] == [T1] and h.row(S1).content["parent"] == h.key(EPIC)
    assert [h.row(node).position for node in (S1, S3, S2)] == [0, 1, 2]


async def test_a_task_description_has_the_done_when_task_list_and_the_phase(h):
    await synced(h)
    blocks = h.gateway.called("create_issue")[2]["fields"]["description"]["content"]
    assert [block["type"] for block in blocks] == ["heading", "taskList", "paragraph", "paragraph"]
    (item,) = blocks[1]["content"]
    assert item["attrs"]["state"] == "TODO" and item["content"][0]["text"] == "it works"
    assert blocks[2]["content"][0]["text"] == "Phase: logic"


async def test_the_description_of_a_story_ends_with_its_id(h):
    await synced(h)
    story_body = h.gateway.called("create_issue")[1]["fields"]["description"]
    last = story_body["content"][-1]["content"]
    assert [part["text"] for part in last] == ["Specflow: ", S1] and last[1]["marks"] == [{"type": "code"}]


async def test_a_second_pass_makes_no_gateway_call_and_no_write(h):
    await synced(h)
    marks = len(h.gateway.calls), len(h.db.calls)
    result = await h.sync()
    assert (result.status, result.counts.unchanged) == ("succeeded", 9)
    assert h.gateway.calls[marks[0] :] == []
    assert {name for name, _ in h.db.calls[marks[1] :]} == {"get_target", "list_issues"}
    assert all(not comments(h, node) for node in (S1, T1))


async def test_a_change_to_a_field_the_content_does_not_hold_makes_no_call(h):
    await synced(h)
    mark = len(h.gateway.calls)
    plan = edit_task(h.plan, T3, status="in_progress", claimed_by="agent", note="working", attempts=2)
    h.set_plan(plan)
    assert (await h.sync()).counts.unchanged == 9
    assert names(h, mark) == []


async def test_every_map_row_write_first_checks_the_target(h):
    await synced(h)
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))
    await h.sync()
    seen = [name for name, _ in h.db.calls]
    writes = [i for i, name in enumerate(seen) if name in WRITES]
    assert writes and all(seen[i - 1] == "target_unchanged" for i in writes)
    assert {session for name, session in h.db.calls if name == "target_unchanged"} == {None}


# --- D5: overwrite in To Do, comment otherwise (FR-JIRA-UPD-01..04)


async def test_a_to_do_issue_is_overwritten_without_a_comment(h):
    await synced(h)
    mark = len(h.gateway.calls)
    h.set_plan(edit_task(h.plan, T3, title="Renamed", complexity="low"))
    result = await h.sync()
    assert (result.status, result.counts.updated, result.counts.commented) == ("succeeded", 2, 0)
    assert h.gateway.called("add_comment") == []
    (edit,) = edits(h, T3)
    assert edit["fields"]["summary"] == "Renamed" and edit["fields"]["customfield_90001"] == 1
    assert "description" not in edit["fields"] and edit["add"] == [] and edit["remove"] == []
    assert h.gateway.issues[h.key(T3)].fields["summary"] == "Renamed"
    (story_edit,) = edits(h, S2)
    assert story_edit["fields"] == {"customfield_90001": 4}
    assert names(h, mark).count("issue_states") == 1
    assert h.row(T3).content["summary"] == "Renamed" and h.row(T3).content["points"] == 1


async def test_an_issue_past_to_do_gets_one_comment_with_exactly_the_changed_items(h):
    await synced(h)
    h.gateway.set_category(h.key(T3), *IN_PROGRESS)
    h.set_plan(edit_task(h.plan, T3, title="Renamed", complexity="low"))
    mark = len(h.gateway.comments.get(h.key(T3), []))
    await h.sync()
    assert edits(h, T3) == []
    (doc,) = comments(h, T3)[mark:]
    assert comment_lead(doc) == (
        f"Specflow plan update for {T3}. "
        'This issue is in status "In Progress", so Specflow left its fields as they are.'
    )
    assert comment_items(doc) == [f'Summary: "Task {T3}" → "Renamed"', "Story points: 5 → 1"]
    assert h.gateway.issues[h.key(T3)].fields["summary"] == f"Task {T3}"
    assert h.row(T3).content["summary"] == "Renamed"
    assert (await h.sync()).counts.unchanged == 9 and len(comments(h, T3)) == 1


@pytest.mark.parametrize(
    ("category", "status", "overwritten"),
    [("new", "Backlog", True), ("indeterminate", "In Review", False), ("done", "Done", False)],
)
async def test_d5_across_status_categories(h, category, status, overwritten):
    await synced(h)
    h.gateway.set_category(h.key(T4), category, status)
    h.set_plan(edit_task(h.plan, T4, title="Changed"))
    await h.sync()
    assert bool(edits(h, T4)) is overwritten
    assert bool(comments(h, T4)) is not overwritten
    if not overwritten:
        assert f'"{status}"' in comment_lead(comments(h, T4)[0])


async def test_a_mapping_version_bump_posts_no_comment_and_refreshes_the_content(h, monkeypatch):
    await synced(h)
    h.gateway.set_category(h.key(T3), *IN_PROGRESS)
    monkeypatch.setattr(rules, "MAPPING_VERSION", 2)
    await h.sync()
    assert h.gateway.called("add_comment") == []
    assert h.row(T3).content["v"] == 2 and edits(h, T3) == []
    assert "description" in edits(h, T1)[0]["fields"] and h.row(T1).content["v"] == 2
    assert edits(h, EPIC) == []
    mark = len(h.gateway.calls)
    assert (await h.sync()).counts.unchanged == 9 and names(h, mark) == []


def redone(plan, task_id: str, **values):
    """The task superseded by its second revision, which differs only by ``values`` (and the id)."""

    old = next(item for item in plan.current.tasks if item.task_id == task_id)
    new = old.model_copy(update={"task_id": f"{task_id}_R2", "revision": 2, **values})
    return with_plan(edit_task(plan, task_id, status="superseded"), tasks=[new])


async def test_a_redone_task_keeps_its_jira_issue(h):
    await synced(h)
    key = h.key(T1)
    h.set_plan(redone(h.plan, T1, complexity="low"))
    mark = len(h.gateway.calls)
    result = await h.sync()
    assert result.counts.created == 0 and h.key(T1) == key and h.row(T1).content["task_id"] == "TASK::A::1_R2"
    assert "create_issue" not in names(h, mark) and edits(h, T1)
    description = edits(h, T1)[0]["fields"]["description"]
    assert description["content"][-1]["content"][1]["text"] == "TASK::A::1_R2"
    assert h.row(T1).removed_at is None and result.counts.removed == 0


async def test_a_redone_task_past_to_do_is_listed_in_a_comment(h):
    await synced(h)
    h.gateway.set_category(h.key(T1), *IN_PROGRESS)
    h.set_plan(redone(h.plan, T1, complexity="medium"))
    await h.sync()
    (doc,) = comments(h, T1)
    assert comment_items(doc) == [f"Task id: {T1} → TASK::A::1_R2", "Story points: 1 → 3"]


# --- removed, revived, superseded (FR-JIRA-UPD-05, 06, 09)


async def test_a_task_removed_in_to_do_gets_a_label_and_a_comment_elsewhere_only_a_comment(h):
    await synced(h)
    h.gateway.set_category(h.key(T4), *IN_PROGRESS)
    plan = edit_task(edit_task(h.plan, T3, status="retired"), T4, status="superseded")
    h.set_plan(plan)
    result = await h.sync()
    assert (result.status, result.counts.removed) == ("succeeded", 2)
    (label_edit,) = edits(h, T3)
    assert label_edit["add"] == [rules.REMOVED_LABEL] and label_edit["fields"] == {}
    assert h.gateway.issues[h.key(T3)].labels >= {rules.REMOVED_LABEL}
    assert (
        comment_lead(comments(h, T3)[0])
        == f"Specflow plan update for {T3}. This item was removed from the Specflow plan."
    )
    assert comment_items(comments(h, T3)[0]) == []
    assert edits(h, T4) == [] and len(comments(h, T4)) == 1
    assert rules.REMOVED_LABEL not in h.gateway.issues[h.key(T4)].labels
    assert h.row(T3).removed_at is not None and h.row(T4).removed_at is not None
    mark = len(h.gateway.calls)
    again = await h.sync()
    assert again.counts.removed == 0 and names(h, mark) == []


async def test_a_removed_item_is_never_deleted_or_transitioned(h):
    await synced(h)
    h.set_plan(make_plan([], []))
    await h.sync()
    assert set(names(h)) <= ALLOWED_CALLS
    assert all(not issue.deleted for issue in h.gateway.issues.values())


async def test_a_revived_item_loses_the_label_in_to_do_and_gets_a_comment_elsewhere(h):
    await synced(h)
    h.gateway.set_category(h.key(T4), *IN_PROGRESS)
    original = h.plan
    h.set_plan(edit_task(edit_task(original, T3, status="retired"), T4, status="retired"))
    await h.sync()
    h.set_plan(original)
    result = await h.sync()
    assert result.counts.removed == 0 and h.row(T3).removed_at is None and h.row(T4).removed_at is None
    assert edits(h, T3)[-1]["remove"] == [rules.REMOVED_LABEL]
    assert rules.REMOVED_LABEL not in h.gateway.issues[h.key(T3)].labels
    assert comment_lead(comments(h, T3)[-1]).endswith("This item is back in the Specflow plan.")
    assert comment_lead(comments(h, T4)[-1]).endswith("This item is back in the Specflow plan.")
    assert len(comments(h, T4)) == 2 and len(edits(h, T4)) == 0
    mark = len(h.gateway.calls)
    assert (await h.sync()).counts.unchanged == 9 and names(h, mark) == []


async def test_a_retired_story_removes_itself_and_its_live_tasks(h):
    await synced(h)
    h.set_plan(edit_story(h.plan, S2, status="retired"))
    result = await h.sync()
    assert result.counts.removed == 3
    for node in (S2, T3, T4):
        assert h.row(node).removed_at is not None
        assert edits(h, node)[0]["add"] == [rules.REMOVED_LABEL]
    assert h.row(S1).removed_at is None and h.row(T1).removed_at is None


async def test_a_live_task_of_a_retired_story_is_handled_as_removed(h):
    await synced(h)
    h.set_plan(edit_story(h.plan, S3, status="retired"))
    assert (await h.sync()).counts.removed == 2 and h.row(T5).removed_at is not None


# --- the Epic (FR-JIRA-MAP-01, 10)


async def test_a_configured_epic_is_never_created_edited_or_commented(monkeypatch):
    h = Harness(monkeypatch, epic_key=EPIC_KEY)
    await synced(h)
    assert EPIC not in {call["node_id"] for call in h.gateway.called("create_issue")}
    assert {h.gateway.called("create_issue")[0]["fields"]["parent"]["key"]} == {EPIC_KEY}
    h.set_plan(edit_task(edit_story(h.plan, S1, title="New"), T3, title="X"))
    await h.sync()
    h.set_plan(make_plan([], []))
    await h.sync()
    for method in ("edit_issue", "add_comment", "upsert_remote_link"):
        assert EPIC_KEY not in [call.get("key") for call in h.gateway.called(method)]
    assert EPIC not in {node for _, node in h.db.issues}


async def test_setting_the_epic_key_later_removes_the_created_epic_and_clearing_it_revives_it(h):
    await synced(h)
    created = h.key(EPIC)
    h.gateway.add_issue({"summary": "Owner epic", "issuetype": {"name": "Epic"}}, key="OWN-1")
    h.db.set_target(h.project_id, epic_key="OWN-1")
    result = await h.sync()
    assert result.counts.removed == 1 and h.row(EPIC).removed_at is not None
    assert edits(h, EPIC)[0]["add"] == [rules.REMOVED_LABEL]
    stories = [edits(h, node)[0]["fields"]["parent"] for node in (S1, S2, S3)]
    assert stories == [{"key": "OWN-1"}] * 3
    assert all(h.gateway.issues[h.key(node)].fields["parent"] == {"key": "OWN-1"} for node in (S1, S2, S3))
    assert not [
        call for method in ("edit_issue", "add_comment") for call in h.gateway.called(method) if call["key"] == "OWN-1"
    ]
    assert h.row(S1).content["parent"] == "OWN-1"

    h.db.set_target(h.project_id, epic_key=None)
    result = await h.sync()
    assert result.counts.created == 0 and h.key(EPIC) == created and h.row(EPIC).removed_at is None
    assert edits(h, EPIC)[-1]["remove"] == [rules.REMOVED_LABEL]
    assert comment_lead(comments(h, EPIC)[-1]).endswith("This item is back in the Specflow plan.")
    assert h.gateway.issues[h.key(S1)].fields["parent"] == {"key": created}
    assert (await h.sync()).counts.unchanged == 9


# --- links (FR-JIRA-MAP-06)


def cross_story_plan():
    stories = [story("STORY::A::ONE"), story("STORY::A::TWO")]
    tasks = [task(T1), task(T2, S2), task(T3, S2)]
    return make_plan(stories, tasks, waits=[(T1, T2), (T3, T2)])


async def test_task_and_derived_story_links_are_created_after_every_issue_exists(monkeypatch):
    h = Harness(monkeypatch, cross_story_plan())
    await synced(h)
    names_in_order = names(h)
    assert max(i for i, name in enumerate(names_in_order) if name == "create_issue") < min(
        i for i, name in enumerate(names_in_order) if name == "add_blocks_link"
    )
    links = {(call["prerequisite"], call["waiter"]) for call in h.gateway.called("add_blocks_link")}
    assert links == {(h.key(T2), h.key(T1)), (h.key(T2), h.key(T3)), (h.key(S2), h.key(S1))}
    assert h.row(S1).content["blocked_by"] == [S2] and h.row(T3).content["blocked_by"] == [T2]
    assert h.gateway.called("rank") == []


def link_id(h: Harness, prerequisite: str, waiter: str) -> str:
    return next(i for i, pair in h.gateway.links.items() if pair == (h.key(prerequisite), h.key(waiter)))


async def test_a_to_do_waiter_loses_only_the_links_specflow_created(monkeypatch):
    h = Harness(monkeypatch, cross_story_plan())
    await synced(h)
    h.gateway.links["777"] = (h.key(T3), h.key(T1))
    ours = {link_id(h, T2, T1), link_id(h, S2, S1)}
    kept = link_id(h, T2, T3)
    h.set_plan(with_plan(h.plan, unwait=[(T1, T2)]))
    result = await h.sync()
    assert result.counts.links_removed == 2
    assert {call["link_id"] for call in h.gateway.called("delete_link")} == ours
    assert set(h.gateway.links) == {"777", kept}
    assert h.row(T1).content["blocked_by"] == [] and h.row(S1).content["blocked_by"] == []
    assert h.gateway.called("add_comment") == []


async def test_a_link_change_of_a_waiter_past_to_do_is_listed_in_its_comment_only(monkeypatch):
    h = Harness(monkeypatch, cross_story_plan())
    await synced(h)
    h.gateway.set_category(h.key(T1), *IN_PROGRESS)
    task_link, story_link = link_id(h, T2, T1), link_id(h, S2, S1)
    h.set_plan(with_plan(h.plan, unwait=[(T1, T2)]))
    await h.sync()
    assert h.gateway.called("delete_link") == [{"link_id": story_link}]
    assert task_link in h.gateway.links
    assert comment_items(comments(h, T1)[0]) == [f"Blocked by: removed {h.key(T2)}"]
    assert h.row(T1).content["blocked_by"] == []


async def test_a_new_link_of_a_to_do_waiter_is_added(monkeypatch):
    h = Harness(monkeypatch, cross_story_plan())
    await synced(h)
    h.set_plan(with_plan(h.plan, waits=[(T1, T3)]))
    result = await h.sync()
    assert result.counts.links_added == 1
    assert h.gateway.called("add_blocks_link")[-1]["prerequisite"] == h.key(T3)
    assert h.row(T1).content["blocked_by"] == [T2, T3]


async def test_a_link_that_reached_jira_before_a_crash_is_not_added_twice(monkeypatch):
    h = Harness(monkeypatch)
    h.gateway.fail("add_blocks_link", Reason.UNREACHABLE, after_effect=True)
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_unreachable")
    assert len(h.gateway.links) == 1 and h.row(T2).content["blocked_by"] == []
    mark = len(h.gateway.calls)
    assert (await h.sync()).status == "succeeded"
    assert "add_blocks_link" not in names(h, mark) and len(h.gateway.links) == 1
    assert h.row(T2).content["blocked_by"] == [T1]


# --- rank (FR-JIRA-MAP-07)


def our_order(h: Harness, nodes):
    """``nodes`` in the order the fake Jira ranks them under the Epic."""

    ranked = h.gateway.rank_order[h.key(EPIC)]
    return sorted(nodes, key=lambda node: ranked.index(h.key(node)))


async def test_a_new_prerequisite_reorders_with_the_minimum_of_moves(monkeypatch):
    stories = [story(S1), story(S2), story(S3)]
    h = Harness(monkeypatch, make_plan(stories, [task(T1), task(T2, S2), task(T3, S3)]))
    await synced(h)
    h.set_plan(with_plan(h.plan, waits=[(T1, T2)]))
    mark = len(h.gateway.calls)
    result = await h.sync()
    assert result.counts.ranked == 1 and names(h, mark).count("rank") == 1
    by_position = sorted((S1, S2, S3), key=lambda node: h.row(node).position)
    assert by_position[-1] != S2 and by_position.index(S1) > by_position.index(S2)
    assert our_order(h, (S1, S2, S3)) == by_position
    assert (await h.sync()).counts.unchanged == 7


def two_stories(monkeypatch):
    plan = make_plan([story(S1), story(S2)], [task(T1), task(T2, S2)])
    return Harness(monkeypatch, plan)


async def drifted(harness: Harness):
    """The stored order is the reverse of the plan's: the same two Stories, unchanged, must swap."""

    await synced(harness)
    harness.row(S1).position, harness.row(S2).position = 1, 0
    (move,) = plan_moves([S2, S1], [S1, S2])
    return move


async def test_a_swap_of_two_unchanged_stories_is_one_rank_call(monkeypatch):
    h = two_stories(monkeypatch)
    move = await drifted(h)
    mark = len(h.gateway.calls)
    result = await h.sync()
    assert result.counts.ranked == 1 and names(h, mark) == ["issue_states", "rank"]
    assert h.gateway.called("issue_states")[0]["keys"] == [h.key(move.keys[0])]
    assert h.gateway.called("rank")[0]["keys"] == [h.key(move.keys[0])]
    assert [h.row(node).position for node in (S1, S2)] == [0, 1]


async def test_a_swapped_story_past_to_do_is_not_ranked_and_its_status_comes_from_the_one_search(monkeypatch):
    h = two_stories(monkeypatch)
    move = await drifted(h)
    h.gateway.set_category(h.key(move.keys[0]), *IN_PROGRESS)
    mark = len(h.gateway.calls)
    result = await h.sync()
    assert result.counts.ranked == 0 and names(h, mark) == ["issue_states"]
    assert h.gateway.called("issue_states")[0]["keys"] == [h.key(move.keys[0])]
    assert comments(h, move.keys[0]) == []
    assert [h.row(node).position for node in (S1, S2)] == [h.row(S1).position, h.row(S2).position] == [1, 0]
    mark = len(h.gateway.calls)
    assert (await h.sync()).counts.unchanged == 5 and names(h, mark) == ["issue_states"]


async def test_an_issue_past_to_do_leaves_the_to_do_issues_in_plan_order_and_the_next_pass_closes_the_rest(
    monkeypatch,
):
    # Scenario: Jira holds D, C, A, B; the plan wants A, B, C, D; C is In Progress. C stays where it is, the To Do
    # issues end as A, B, D, and the stored positions are what Jira holds, so once C is back in To Do it is moved.
    ids = {name: f"STORY::A::{name}" for name in "ABCD"}
    a, b, c, d = (ids[name] for name in "ABCD")
    h = Harness(monkeypatch, make_plan([story(n) for n in ids.values()], [task(T1)]))
    await synced(h)
    for index, node in enumerate((d, c, a, b)):
        h.row(node).position = index
    h.gateway.rank_order[h.key(EPIC)] = [h.key(node) for node in (d, c, a, b)]
    h.gateway.set_category(h.key(c), *IN_PROGRESS)
    assert (await h.sync()).status == "succeeded"
    assert our_order(h, (a, b, c, d)) == [c, a, b, d]
    assert [node for node in our_order(h, (a, b, c, d)) if node != c] == [a, b, d]
    assert sorted((a, b, c, d), key=lambda node: h.row(node).position) == [c, a, b, d]
    h.gateway.set_category(h.key(c), "new", "To Do")
    assert (await h.sync()).counts.ranked == 1
    assert our_order(h, (a, b, c, d)) == [a, b, c, d]


async def test_a_rank_call_refused_with_400_marks_the_item_and_is_retried(monkeypatch):
    h = two_stories(monkeypatch)
    move = await drifted(h)
    h.gateway.fail("rank", Reason.INVALID_FIELD, status=400)
    result = await h.sync()
    assert (result.status, result.counts.failed, result.counts.ranked) == ("partial", 1, 0)
    assert h.row(move.keys[0]).error_code == "jira_invalid_field"
    result = await h.sync()
    assert (result.status, result.counts.ranked) == ("succeeded", 1)
    assert h.row(move.keys[0]).error_code is None


# --- pending rows and crash recovery (FR-JIRA-REL-01, REL-05)


async def test_a_crash_after_the_create_adopts_the_issue_without_a_duplicate(h):
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_unreachable")
    assert h.row(EPIC).jira_key is None and h.row(EPIC).requested_at is not None
    result = await h.sync()
    assert (result.status, result.counts.created) == ("succeeded", 8)
    assert [issue.fields["summary"] for issue in h.gateway.issues.values()].count("Shop") == 1
    (created,) = [key for key, issue in h.gateway.issues.items() if issue.fields["summary"] == "Shop"]
    assert h.row(EPIC).jira_key == created
    find = h.gateway.called("find_created")
    assert find == [{"project_key": "PRJ", "project_id": str(h.project_id), "minutes": 7}]
    assert h.gateway.issues[h.key(EPIC)].properties["specflow"] == {"project_id": str(h.project_id), "node_id": EPIC}
    assert h.row(EPIC).content is not None and h.gateway.issues[h.key(S1)].fields["parent"] == {"key": h.key(EPIC)}


async def test_the_recovery_window_covers_the_oldest_request(h):
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    await h.sync()
    h.clock = h.clock.replace(minute=h.clock.minute + 7, second=30)
    await h.sync()
    assert h.gateway.called("find_created")[0]["minutes"] == 7 + 1 + 5


async def test_recovery_waits_out_the_search_index_lag_instead_of_duplicating(h):
    h.gateway.index_lag = timedelta(seconds=SEARCH_INDEX_LAG_SECONDS)
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    await h.sync()
    result = await h.sync()
    assert (result.status, result.counts.created) == ("succeeded", 8)
    assert [issue.fields["summary"] for issue in h.gateway.issues.values()].count("Shop") == 1
    assert 0 < sum(h.slept) <= SEARCH_INDEX_LAG_SECONDS


async def test_recovery_does_not_wait_for_a_row_past_the_lag(h):
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    await h.sync()
    h.clock += timedelta(seconds=SEARCH_INDEX_LAG_SECONDS)
    await h.sync()
    assert h.slept == []


async def test_a_create_refused_with_400_is_not_recovered_but_a_later_crashed_create_is(h):
    h.gateway.fail("create_issue", Reason.INVALID_FIELD, status=400)
    assert (await h.sync()).status == "partial"
    assert h.row(EPIC).jira_key is None and h.row(EPIC).error_code == "jira_invalid_field"
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    assert (await h.sync()).status == "failed"
    assert h.slept == [] and h.gateway.called("find_created") == []
    assert h.row(EPIC).error_code is None
    assert (await h.sync()).status == "succeeded"
    assert len(h.gateway.called("find_created")) == 1
    assert [issue.fields["summary"] for issue in h.gateway.issues.values()].count("Shop") == 1


async def test_two_projects_on_one_jira_project_each_adopt_only_their_own_pending_epic(monkeypatch):
    first = Harness(monkeypatch)
    second = Harness(monkeypatch, share=first)
    first.gateway.fail("create_issue", Reason.UNREACHABLE, times=2, after_effect=True)
    assert (await first.sync()).status == "failed" and (await second.sync()).status == "failed"
    assert first.row(EPIC).jira_key is None and second.row(EPIC).jira_key is None
    assert (await first.sync()).status == "succeeded" and (await second.sync()).status == "succeeded"
    epics = {
        issue.properties["specflow"]["project_id"]: key
        for key, issue in first.gateway.issues.items()
        if issue.properties["specflow"]["node_id"] == EPIC
    }
    assert len(epics) == 2
    assert first.key(EPIC) == epics[str(first.project_id)] and second.key(EPIC) == epics[str(second.project_id)]
    assert first.key(EPIC) != second.key(EPIC)
    summaries = [issue.fields["summary"] for issue in first.gateway.issues.values()]
    assert summaries.count("Shop") == 2


async def test_a_pending_row_without_a_match_creates_again(h):
    h.gateway.fail("create_issue", Reason.UNREACHABLE)
    assert (await h.sync()).status == "failed" and h.row(EPIC).jira_key is None
    assert (await h.sync()).counts.created == 9
    assert len([i for i in h.gateway.issues.values() if i.fields["summary"] == "Shop"]) == 1


async def test_a_crash_after_a_comment_repeats_it_once(h):
    await synced(h)
    h.gateway.set_category(h.key(T3), *IN_PROGRESS)
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))
    h.gateway.fail("add_comment", Reason.UNREACHABLE, key=h.key(T3), after_effect=True)
    assert (await h.sync()).status == "failed" and len(comments(h, T3)) == 1
    assert h.row(T3).content["summary"] != "Renamed"
    assert (await h.sync()).status == "succeeded" and len(comments(h, T3)) == 2
    mark = len(h.gateway.calls)
    await h.sync()
    assert len(comments(h, T3)) == 2 and names(h, mark) == []


async def test_a_row_with_a_key_and_no_content_is_applied_in_full_with_a_remote_link(monkeypatch):
    h = Harness(monkeypatch, public_url="https://app.example")
    h.gateway.fail("upsert_remote_link", Reason.UNREACHABLE, times=1)
    assert (await h.sync()).status == "failed"
    assert h.row(EPIC).jira_key and h.row(EPIC).content is None
    assert (await h.sync()).status == "succeeded"
    assert h.row(EPIC).content is not None
    assert (h.key(EPIC), f"specflow:{h.project_id}:{EPIC}") in h.gateway.remote_links
    assert edits(h, EPIC)[0]["fields"]["summary"] == "Shop" and edits(h, EPIC)[0]["add"] == [rules.OWNED_LABEL]


# --- item and run errors (FR-JIRA-REL-02, REL-03, UPD-08, sync.md#failure-handling)


async def test_a_400_on_one_edit_marks_the_item_and_the_pass_is_partial(h):
    await synced(h)
    h.set_plan(edit_task(edit_task(h.plan, T3, title="Rejected"), T4, title="Accepted"))
    h.gateway.fail("edit_issue", Reason.INVALID_FIELD, status=400, key=h.key(T3), times=1)
    result = await h.sync()
    assert (result.status, result.counts.failed, result.counts.updated) == ("partial", 1, 1)
    assert h.row(T3).error_code == "jira_invalid_field" and h.row(T3).content["summary"] != "Rejected"
    assert h.row(T4).content["summary"] == "Accepted" and h.row(T4).error_code is None
    assert (await h.sync()).status == "succeeded"
    assert h.row(T3).error_code is None and h.row(T3).content["summary"] == "Rejected"


async def test_a_400_on_one_create_leaves_the_others_created_and_the_pass_partial(h):
    await synced(h)
    h.set_plan(with_plan(h.plan, tasks=[task("TASK::A::6"), task("TASK::A::7")]))
    h.gateway.fail("create_issue", Reason.INVALID_FIELD, status=400)
    result = await h.sync()
    assert (result.status, result.counts.failed, result.counts.created) == ("partial", 1, 1)
    failed, created = h.row("TASK::A::6"), h.row("TASK::A::7")
    assert failed.jira_key is None and failed.error_code == "jira_invalid_field" and created.jira_key
    assert (await h.sync()).counts.created == 1 and h.row("TASK::A::6").error_code is None


async def test_the_children_of_an_epic_that_cannot_be_created_are_not_created(h):
    h.gateway.fail("create_issue", Reason.INVALID_FIELD, status=400)
    result = await h.sync()
    assert (result.status, result.counts.created) == ("partial", 0)
    assert [call["node_id"] for call in h.gateway.called("create_issue")] == [EPIC]


async def test_a_mapped_issue_deleted_in_jira_is_removed_without_failing_the_pass(h):
    await synced(h)
    h.gateway.delete(h.key(T3))
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))
    result = await h.sync()
    assert (result.status, result.counts.removed, result.counts.failed) == ("succeeded", 1, 0)
    row = h.row(T3)
    assert row.removed_at is not None and row.error_code == "jira_not_found"
    mark = len(h.gateway.calls)
    h.set_plan(edit_task(h.plan, T3, title="Renamed again"))
    again = await h.sync()
    assert (again.status, again.counts.removed) == ("succeeded", 0) and names(h, mark) == []


async def test_an_edit_that_answers_404_means_the_issue_was_deleted(h):
    await synced(h)
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))
    h.gateway.fail("edit_issue", Reason.NOT_FOUND, status=404, key=h.key(T3))
    result = await h.sync()
    assert (result.status, result.counts.removed) == ("succeeded", 1) and h.row(T3).error_code == "jira_not_found"


async def test_a_create_that_answers_404_marks_the_parent_story_deleted_and_stops_creating_its_tasks(h):
    await synced(h)
    h.set_plan(with_plan(h.plan, tasks=[task("TASK::A::6", S2)]))
    h.gateway.fail("create_issue", Reason.NOT_FOUND, status=404)
    result = await h.sync()
    assert (result.status, result.counts.removed, result.counts.created) == ("succeeded", 1, 0)
    assert h.row(S2).error_code == "jira_not_found" and h.row(S2).removed_at is not None
    mark = len(h.gateway.calls)
    await h.sync()
    assert names(h, mark) == []


async def test_a_link_that_answers_404_deletes_the_missing_prerequisite_not_the_waiter(monkeypatch):
    plan = make_plan([story(S1), story(S2)], [task(T1), task(T2, S2), task(T3)], waits=[(T1, T2)])
    h = Harness(monkeypatch, plan)
    await synced(h)
    h.gateway.delete(h.key(T2))
    h.set_plan(with_plan(h.plan, waits=[(T3, T2)]))
    result = await h.sync()
    assert (result.status, result.counts.removed, result.counts.failed) == ("succeeded", 1, 0)
    assert h.row(T2).error_code == "jira_not_found" and h.row(T3).error_code is None
    assert h.row(T3).content["blocked_by"] == []


async def test_the_created_epic_deleted_in_jira_is_a_run_error(h):
    await synced(h)
    h.gateway.delete(h.key(EPIC))
    h.db.set_target(h.project_id, project_name="Renamed")
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_not_found")


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (Reason.AUTH_FAILED, "jira_auth_failed"),
        (Reason.FORBIDDEN, "jira_forbidden"),
        (Reason.NOT_FOUND, "jira_not_found"),
        (Reason.UNREACHABLE, "jira_unreachable"),
        (Reason.UNEXPECTED, "jira_unexpected"),
        (Reason.INVALID_FIELD, "jira_invalid_field"),
        (Reason.NOT_CONNECTED, "jira_not_connected"),  # oauth: the acting connection was deleted during the pass
        (Reason.RECONNECT_REQUIRED, "jira_reconnect_required"),  # oauth: its refresh token died during the pass
    ],
)
async def test_a_run_error_stops_the_pass_with_its_code_and_no_further_call(h, reason, code):
    h.gateway.fail("project", reason, times=None)
    result = await h.sync()
    expected = "jira_unexpected" if reason is Reason.INVALID_FIELD else code
    assert (result.status, result.error_code) == ("failed", expected)
    assert names(h) == ["project"]


async def test_a_401_in_the_middle_of_a_pass_stops_it(h):
    h.gateway.fail("create_issue", Reason.AUTH_FAILED, status=401, times=None)
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_auth_failed")
    assert names(h)[-1] == "create_issue" and names(h).count("create_issue") == 1


async def test_an_unexpected_exception_is_a_code_and_a_logged_class_name_only(h, caplog):
    async def broken(_project_id):
        raise RuntimeError("secret plan text")

    deps = h.deps()
    object.__setattr__(deps, "load_plan", broken)
    with caplog.at_level(logging.INFO):
        result = await sync_once(str(h.project_id), deps)
    assert (result.status, result.error_code) == ("failed", "unexpected_error")
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "api.jira.sync.failed"]
    assert record.error_type == "RuntimeError" and "secret plan text" not in str(vars(record))


# --- skipped passes (FR-JIRA-RUN-06)


async def test_no_plan_and_a_plan_being_written_end_skipped_without_a_jira_call(h):
    h.set_plan(LoadedPlan(CurrentPlan(plan=None)))
    result = await h.sync()
    assert (result.status, result.error_code) == ("skipped", "no_plan")
    h.set_plan(make_plan([story(S1)], [task(T1)], applying=True))
    result = await h.sync()
    assert (result.status, result.error_code) == ("skipped", "plan_applying")
    assert h.gateway.calls == [] and h.db.issues == {}


@pytest.mark.parametrize("change", ["disabled", "gone"])
async def test_a_disabled_or_missing_target_ends_skipped(h, change):
    if change == "disabled":
        h.db.set_target(h.project_id, enabled=False)
    else:
        del h.db.targets[h.project_id]
    result = await h.sync()
    assert (result.status, result.error_code) == ("skipped", "jira_not_configured") and h.gateway.calls == []


# --- the target change (FR-JIRA-RUN-08)


@pytest.mark.parametrize(("column", "value"), [("jira_project", "OTHER"), ("epic_key", "OTHER-5")])
async def test_a_target_change_during_a_pass_stops_it_at_once(h, column, value):
    fired = []

    async def change_target() -> None:
        if not fired:
            fired.append(True)
            h.db.set_target(h.project_id, **{column: value})

    h.gateway.holds["create_issue"] = change_target
    result = await h.sync()
    assert (result.status, result.error_code) == ("skipped", "target_changed")
    assert names(h)[-1] == "create_issue" and names(h).count("create_issue") == 1
    assert h.row(EPIC).jira_key is None and h.row(EPIC).content is None
    assert [name for name, _ in h.db.calls].count("update_issue") == 0
    mark = len(h.gateway.calls)
    assert h.db.calls[-1][0] == "target_unchanged" and names(h, mark) == []


async def test_the_target_is_checked_before_a_change_of_an_existing_row(h):
    await synced(h)
    h.set_plan(edit_task(h.plan, T3, title="Renamed"))

    async def change_target() -> None:
        h.db.set_target(h.project_id, jira_project="OTHER")

    h.gateway.holds["edit_issue"] = change_target
    result = await h.sync()
    assert (result.status, result.error_code) == ("skipped", "target_changed")
    assert h.row(T3).content["summary"] != "Renamed"


# --- fields, labels, points, remote links (FR-JIRA-MAP-03..06, 08, 11)


async def test_without_a_story_points_field_no_points_are_sent_but_they_stay_in_the_content(monkeypatch):
    h = Harness(monkeypatch)
    h.gateway.points_field = None
    await synced(h)
    assert all(
        not [name for name in call["fields"] if name.startswith("customfield")]
        for call in h.gateway.called("create_issue")
    )
    assert h.row(T3).content["points"] == 5
    h.set_plan(edit_task(h.plan, T3, complexity="low"))
    await h.sync()
    assert edits(h, T3) == [] and h.row(T3).content["points"] == 1  # nothing to send: stored, no Jira edit


async def test_the_remote_link_is_made_only_with_a_public_url(monkeypatch):
    without = Harness(monkeypatch)
    await synced(without)
    assert without.gateway.called("upsert_remote_link") == [] and without.gateway.remote_links == {}
    with_url = Harness(monkeypatch, public_url="https://app.example/")
    await synced(with_url)
    assert len(with_url.gateway.remote_links) == 9
    url, title = with_url.gateway.remote_links[(with_url.key(T1), f"specflow:{with_url.project_id}:{T1}")]
    assert (url, title) == (f"https://app.example/projects/{with_url.project_id}/plan", "Specflow plan")
    mark = len(with_url.gateway.calls)
    await with_url.sync()
    assert names(with_url, mark) == []


async def test_a_label_a_person_added_is_kept_when_the_area_changes(h):
    await synced(h)
    h.gateway.issues[h.key(T3)].labels.add("human")
    h.set_plan(edit_task(h.plan, T3, area_id="AREA::B"))
    await h.sync()
    (edit,) = edits(h, T3)
    assert edit["add"] == ["area-b"] and edit["remove"] == ["area-a"]
    assert h.gateway.issues[h.key(T3)].labels == {"human", "area-b", "specflow"}


async def test_the_field_limits_hold_in_the_create_body_and_the_stored_content(monkeypatch):
    plan = make_plan(
        [story(S1, title="  A\n\n  long   " + "s" * 300, summary="t" * 40_000, area_id="AREA::" + "q" * 300)],
        [task(T1, title="T\t" + "x" * 300, area_id="AREA::__")],
    )
    h = Harness(monkeypatch, plan)
    await synced(h)
    bodies = {call["node_id"]: call["fields"] for call in h.gateway.called("create_issue")}
    assert bodies[S1]["summary"].startswith("A long s") and bodies[S1]["summary"].endswith("…")
    assert len(bodies[S1]["summary"]) == 255 and len(bodies[T1]["summary"]) == 255
    assert bodies[S1]["labels"] == ["area-" + "q" * 200, "specflow"] and bodies[T1]["labels"] == ["specflow"]
    description = bodies[S1]["description"]["content"][0]["content"][0]["text"]
    assert len(description) == 30_000 and description.endswith("…")
    assert h.row(S1).content["summary"] == bodies[S1]["summary"] and len(h.row(S1).content["text"]) == 30_000


# --- NFR-JIRA-02, 04 and the closed set of calls


async def test_the_calls_of_a_busy_scenario_are_all_in_the_allowed_set(monkeypatch):
    h = Harness(monkeypatch, cross_story_plan(), public_url="https://app.example")
    await synced(h)
    h.gateway.set_category(h.key(T3), *IN_PROGRESS)
    h.set_plan(with_plan(edit_task(h.plan, T3, title="N"), unwait=[(T1, T2)], waits=[(T1, T3)]))
    await h.sync()
    h.set_plan(make_plan([], []))
    await h.sync()
    assert set(names(h)) <= ALLOWED_CALLS and set(names(h)) >= {"edit_issue", "add_comment", "delete_link"}
    assert not [issue for issue in h.gateway.issues.values() if issue.deleted]
    sent = [call["fields"] for method in ("create_issue", "edit_issue") for call in h.gateway.called(method)]
    assert not [name for fields in sent for name in fields if name in {"status", "assignee", "reporter", "priority"}]


async def test_logs_carry_ids_and_codes_never_plan_text(monkeypatch, caplog):
    canary = "CANARY-SUMMARY-90210"
    plan = make_plan([story(S1)], [task(T1, title=canary, description=canary)])
    h = Harness(monkeypatch, plan)
    await synced(h)
    h.set_plan(edit_task(h.plan, T1, title=canary + " 2", description=canary + " 3"))
    h.gateway.fail("edit_issue", Reason.INVALID_FIELD, status=400, key=h.key(T1))
    with caplog.at_level(logging.INFO):
        assert (await h.sync()).status == "partial"
    (failed,) = [r for r in caplog.records if getattr(r, "event", None) == "api.jira.sync.item_failed"]
    assert (failed.project_id, failed.node_id, failed.jira_key, failed.error_code) == (
        str(h.project_id),
        T1,
        h.key(T1),
        "jira_invalid_field",
    )
    assert canary not in "\n".join(f"{r.getMessage()} {vars(r)}" for r in caplog.records)


async def test_a_recovered_create_is_logged_with_ids(monkeypatch, caplog):
    h = Harness(monkeypatch)
    h.gateway.fail("create_issue", Reason.UNREACHABLE, after_effect=True)
    await h.sync()
    with caplog.at_level(logging.INFO):
        await h.sync()
    (recovered,) = [r for r in caplog.records if getattr(r, "event", None) == "api.jira.sync.recovered"]
    assert (recovered.project_id, recovered.node_id, recovered.jira_key) == (str(h.project_id), EPIC, h.key(EPIC))


# --- the call count of a 60-issue first sync


def generated_plan():
    stories = [story(f"STORY::A::S{n:02}") for n in range(6)]
    tasks, waits = [], []
    for n in range(6):
        for m in range(9):
            tasks.append(task(f"TASK::A::S{n}T{m}", f"STORY::A::S{n:02}", complexity=("low", "medium", "high")[m % 3]))
            if m:
                waits.append((f"TASK::A::S{n}T{m}", f"TASK::A::S{n}T{m - 1}"))
        if n:
            waits.append((f"TASK::A::S{n}T0", f"TASK::A::S{n - 1}T8"))
    return make_plan(stories, tasks[:53], waits=waits)


async def test_a_sixty_issue_first_sync_stays_within_four_calls_per_issue(monkeypatch):
    h = Harness(monkeypatch, generated_plan(), public_url="https://app.example")
    await synced(h)
    assert len(h.gateway.issues) == 60
    counts = {name: names(h).count(name) for name in sorted(set(names(h)))}
    assert counts == {
        "add_blocks_link": 57,
        "create_issue": 60,
        "project": 1,
        "story_points_field": 1,
        "upsert_remote_link": 60,
    }
    assert sum(counts.values()) == 179 and sum(counts.values()) <= 4 * 60
    assert (await h.sync()).counts.unchanged == 60 and len(h.gateway.calls) == 179
