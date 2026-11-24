"""Edge cases of the Jira sync pass: ranking a created issue into place, the fields a To Do overwrite sends, a
deleted Epic, an item that failed after its create, and blank texts."""

from __future__ import annotations

import pytest

from api.jira.desired import EPIC
from jira.reasons import Reason
from jira_sync_support import EPIC_KEY, Harness, edit_task, make_plan, story, task, with_plan

S1, S2, S3 = "STORY::A::ONE", "STORY::A::TWO", "STORY::A::THREE"
M1, M2, M3 = "STORY::A::1", "STORY::A::2", "STORY::A::3"  # ids that sort in this order: M2 belongs in the middle
T1, T2, T3, T4 = (f"TASK::A::{n}" for n in range(1, 5))


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


async def synced(harness: Harness) -> Harness:
    result = await harness.sync()
    assert (result.status, result.error_code) == ("succeeded", None)
    return harness


def jira_order(h: Harness, scope: str, nodes) -> list[str]:
    """``nodes`` in the order the fake Jira ranks them under ``scope`` (a node id)."""

    ranked = h.gateway.rank_order[h.key(scope)]
    return sorted(nodes, key=lambda node: ranked.index(h.key(node)))


# --- an issue created in the middle of a scope is ranked into place


async def test_a_story_created_between_two_synced_stories_is_ranked_into_place(monkeypatch):
    # Scenario: M1 and M3 are synced; the plan gains M2, ordered between them. Jira appends M2 at the bottom, so
    # exactly one rank call must follow and Jira's order must equal the plan's.
    h = Harness(monkeypatch, make_plan([story(M1), story(M3)], [task(T1, M1), task(T3, M3)]))
    await synced(h)
    h.set_plan(with_plan(h.plan, stories=[story(M2)], tasks=[task(T2, M2)]))
    result = await h.sync()
    assert (result.status, result.counts.created, result.counts.ranked) == ("succeeded", 2, 1)
    assert len(h.gateway.called("rank")) == 1
    assert jira_order(h, EPIC, (M1, M2, M3)) == [M1, M2, M3]
    assert [h.row(node).position for node in (M1, M2, M3)] == [0, 1, 2]
    assert (await h.sync()).counts.unchanged == 7


async def test_a_sub_task_created_between_two_synced_siblings_is_ranked_into_place(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1)], [task(T1), task(T3)]))
    await synced(h)
    h.set_plan(with_plan(h.plan, tasks=[task(T2)]))
    result = await h.sync()
    assert (result.counts.created, result.counts.ranked) == (1, 1)
    assert jira_order(h, S1, (T1, T2, T3)) == [T1, T2, T3]


async def test_a_first_sync_makes_no_rank_call(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1), story(S2)], [task(T1), task(T2, S2)]))
    await synced(h)
    assert h.gateway.called("rank") == []


# --- a To Do overwrite sends only the fields that differ


async def test_a_change_that_is_not_in_the_description_leaves_the_description_alone(monkeypatch):
    # Scenario: a person edits a To Do Sub-task's description in Jira; the plan then only adds a wait (a link) and
    # changes a complexity (points). Neither is a description input, so no description is sent.
    h = Harness(monkeypatch, make_plan([story(S1)], [task(T1), task(T2), task(T3)]))
    await synced(h)
    h.set_plan(with_plan(edit_task(h.plan, T3, complexity="high"), waits=[(T2, T1)]))
    result = await h.sync()
    assert result.counts.links_added == 1
    sent = [call["fields"] for call in h.gateway.called("edit_issue")]
    assert sent and all("description" not in fields for fields in sent)


async def test_a_change_of_a_description_input_rewrites_the_description(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1)], [task(T1), task(T2)]))
    await synced(h)
    h.set_plan(edit_task(h.plan, T1, description="New words."))
    await h.sync()
    (edit,) = [call for call in h.gateway.called("edit_issue") if call["key"] == h.key(T1)]
    assert set(edit["fields"]) == {"description"}


# --- the Epic that Jira no longer finds is a run error


async def test_clearing_the_epic_key_after_the_created_epic_was_deleted_is_a_run_error_not_a_new_epic(h):
    await synced(h)
    h.gateway.add_issue({"summary": "Owner epic", "issuetype": {"name": "Epic"}}, key="OWN-1")
    h.db.set_target(h.project_id, epic_key="OWN-1")
    h.gateway.delete(h.key(EPIC))
    await h.sync()  # the created Epic is removed from the plan and found deleted in Jira
    assert h.row(EPIC).error_code == "jira_not_found"
    h.db.set_target(h.project_id, epic_key=None)
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_not_found")
    assert [call["node_id"] for call in h.gateway.called("create_issue")].count(EPIC) == 1
    assert h.gateway.comments.get(h.key(EPIC), []) == []


async def test_a_configured_epic_deleted_in_jira_fails_the_run_before_a_story_is_created(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1)], [task(T1)]), epic_key=EPIC_KEY)
    await synced(h)
    h.gateway.delete(EPIC_KEY)
    h.set_plan(with_plan(h.plan, stories=[story(S2)], tasks=[task(T2, S2)]))
    created = len(h.gateway.called("create_issue"))
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_not_found")
    assert len(h.gateway.called("create_issue")) == created


async def test_a_new_epic_key_that_jira_does_not_find_fails_the_run_before_the_stories_move(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1)], [task(T1)]))
    await synced(h)
    h.db.set_target(h.project_id, epic_key="GONE-1")
    result = await h.sync()
    assert (result.status, result.error_code) == ("failed", "jira_not_found")
    assert h.gateway.called("edit_issue") == []


async def test_the_epic_is_read_once_per_pass_and_not_after_it_was_just_created(monkeypatch):
    h = Harness(monkeypatch, make_plan([story(S1), story(S2)], [task(T1), task(T2, S2)]))
    await synced(h)
    assert h.gateway.called("issue_states") == []  # the Epic of this pass was created in it: no read
    configured = Harness(monkeypatch, make_plan([story(S1), story(S2)], [task(T1), task(T2, S2)]), epic_key=EPIC_KEY)
    await synced(configured)
    assert [call["keys"] for call in configured.gateway.called("issue_states")] == [[EPIC_KEY]]


# --- an item that failed after its create is not applied again in the same pass


async def test_an_item_that_failed_after_its_create_is_counted_and_not_applied_again(monkeypatch):
    # Scenario: the Epic is created and keyed, then its remote link answers 400. The same pass must not edit it or
    # post the remote link again; it still counts as created; the next pass applies it in full.
    h = Harness(monkeypatch, public_url="https://specflow.example.com")
    h.gateway.fail("upsert_remote_link", Reason.INVALID_FIELD, status=400)
    result = await h.sync()
    assert (result.status, result.counts.created, result.counts.failed) == ("partial", 9, 1)
    assert h.row(EPIC).jira_key and h.row(EPIC).content is None
    assert len(h.gateway.called("upsert_remote_link")) == 9
    assert [call for call in h.gateway.called("edit_issue") if call["key"] == h.key(EPIC)] == []
    result = await h.sync()
    assert (result.status, result.counts.created) == ("succeeded", 0)
    assert h.row(EPIC).content is not None and h.row(EPIC).error_code is None


# --- a blank title and a blank done-when text never reach Jira


async def test_a_blank_title_falls_back_to_the_node_id_and_a_blank_done_when_is_dropped(monkeypatch):
    blank = task(
        T1, title="   ", done_when=({"text": "  ", "source": "planner"}, {"text": "real", "source": "planner"})
    )
    h = Harness(monkeypatch, make_plan([story(S1, title=" ")], [blank]))
    await synced(h)
    summaries = {call["node_id"]: call["fields"]["summary"] for call in h.gateway.called("create_issue")}
    assert summaries[S1] == S1 and summaries[T1] == T1
    assert h.row(T1).content["done_when"] == ["real"]
