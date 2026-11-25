"""Tier 1: posting the open questions to Jira (``api.jira_gaps.poster``, docs/jira-sync/gaps.md "Posting"): one Story
per project created once and reused, a sub-task per question or per work area with numbered questions, lettered options,
every question posted once (also after a restart), and a refused Jira call kept as a closed code. Fakes: the Jira of
``jira_gaps_support``, the gap service and the rows; no Jira, no database."""

from __future__ import annotations

import pytest

from api.jira_gaps import text
from db.jira_gaps import GapTargetView
from jira.gateway import JiraError, JiraProject
from jira.reasons import Reason
from jira_gaps_support import ACTING, PROJECT, Rig, build_rig, gap, plan_of


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(monkeypatch)


def flat(doc: dict) -> str:
    """The text of an ADF document, paragraphs and list items in order."""

    return text.comment_text(doc)


async def target(rig: Rig) -> GapTargetView:
    found = await rig.db.get_target(None, PROJECT)
    assert found is not None
    return found


# One sub-task per gap ----------------------------------------------------------------------------------------------


async def test_the_first_post_creates_the_story_once_and_a_subtask_per_question(rig: Rig) -> None:
    # Scenario: two open questions, grouping "one"; a second gap run changes nothing.
    #   gap runner --send--> poster --create Story--> Jira; poster --create Sub-task x2--> Jira; rows written
    await rig.enable("one")
    rig.service.add(gap("G-1"), gap("G-2"))
    poster = rig.poster()

    assert await poster.post_open(str(PROJECT)) == 2
    assert await poster.post_open(str(PROJECT)) == 0  # posted once per gap

    stories = rig.jira.created("story")
    subtasks = [c for c in rig.jira.created() if c["fields"].get("parent")]
    assert len(stories) == 1 and stories[0]["fields"]["summary"] == "Open questions · Shop"
    assert stories[0]["fields"]["issuetype"] == {"id": "10"}
    assert [c["node_id"] for c in subtasks] == ["gaps:G-1", "gaps:G-2"]
    assert all(c["fields"]["parent"] == {"key": stories[0]["key"]} for c in subtasks)
    assert all(c["fields"]["issuetype"] == {"id": "11"} for c in subtasks)
    assert (await target(rig)).story_key == stories[0]["key"]
    rows = await rig.db.list_issues(None, PROJECT)
    assert [(r.gap_id, r.number, r.answered_at) for r in rows] == [("G-1", 1, None), ("G-2", 1, None)]


async def test_a_question_is_a_lettered_list_with_how_to_answer(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1", title="Cart rule"))

    await rig.poster().post_open(str(PROJECT))

    [subtask] = [c for c in rig.jira.created() if c["fields"].get("parent")]
    body = flat(subtask["fields"]["description"])
    assert subtask["fields"]["summary"] == "Cart rule"
    assert "Which cart rule applies to G-1?" in body and "Why it matters: The documents disagree." in body
    assert "**A)** Keep one cart (recommended) - One cart per shopper" in body
    assert "**B)** Allow many carts" in body
    assert "Answer in a comment: one option letter, or your own words." in body


async def test_a_later_gap_run_posts_only_the_new_questions_under_the_same_story(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    rig.service.add(gap("G-2"))

    channel = rig.channel()
    await channel.send(str(PROJECT), None)  # the notice of the gap runner

    assert len(rig.jira.created("story")) == 1
    assert [c["node_id"] for c in rig.jira.created() if c["fields"].get("parent")] == ["gaps:G-1", "gaps:G-2"]


async def test_a_restarted_process_does_not_post_a_question_again(rig: Rig) -> None:
    # Everything it knows is in the rows: a new poster (a restart) reads them and posts nothing.
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    created = len(rig.jira.calls)

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert len([c for c in rig.jira.calls[created:] if c[0] in {"create", "comment"}]) == 0


async def test_an_answered_question_is_not_posted_again_when_it_is_open_again(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    await rig.db.mark_answered(None, PROJECT, "G-1", now=rig.clock())

    assert await rig.poster().post_open(str(PROJECT)) == 0


# One sub-task per work area ------------------------------------------------------------------------------------


async def test_questions_of_one_work_area_share_a_subtask_with_numbered_questions(rig: Rig) -> None:
    # Scenario: three questions, two reach tasks of area A1 and one has no plan task (General).
    await rig.enable("area")
    rig.plan = plan_of({"A1": "Checkout", "A2": "Search"}, {"T1": "A1", "T2": "A1", "T3": "A2"})
    rig.service.add(gap("G-1", tasks=["T1"]), gap("G-2", tasks=["T2"]), gap("G-3"))

    assert await rig.poster().post_open(str(PROJECT)) == 3

    subtasks = [c for c in rig.jira.created() if c["fields"].get("parent")]
    assert [c["fields"]["summary"] for c in subtasks] == ["Checkout · open questions", "General · open questions"]
    body = flat(subtasks[0]["fields"]["description"])
    assert "1. Cart question G-1" in body and "2. Cart question G-2" in body
    assert "one line per question: the number, a colon" in body
    rows = {r.gap_id: (r.issue_key, r.number) for r in await rig.db.list_issues(None, PROJECT)}
    assert rows["G-1"] == (subtasks[0]["key"], 1) and rows["G-2"] == (subtasks[0]["key"], 2)
    assert rows["G-3"] == (subtasks[1]["key"], 1)


async def test_a_new_question_of_a_known_area_is_a_comment_with_the_next_number(rig: Rig) -> None:
    await rig.enable("area")
    rig.plan = plan_of({"A1": "Checkout"}, {"T1": "A1", "T2": "A1"})
    rig.service.add(gap("G-1", tasks=["T1"]))
    await rig.poster().post_open(str(PROJECT))
    rig.service.add(gap("G-2", tasks=["T2"]))

    assert await rig.poster().post_open(str(PROJECT)) == 1

    [subtask] = [c for c in rig.jira.created() if c["fields"].get("parent")]
    [comment] = rig.jira.comments_by_acting(subtask["key"])
    assert "2. Cart question G-2" in flat(comment.doc)
    rows = {r.gap_id: (r.issue_key, r.number) for r in await rig.db.list_issues(None, PROJECT)}
    assert rows["G-2"] == (subtask["key"], 2)


# Nothing to post, or Jira refuses ----------------------------------------------------------------------------------


async def test_a_disabled_or_missing_target_posts_nothing(rig: Rig) -> None:
    rig.service.add(gap("G-1"))
    assert await rig.poster().post_open(str(PROJECT)) == 0  # no target
    await rig.enable()
    await rig.db.save_target(None, PROJECT, jira_project="SHOP", grouping="one", enabled=False, now=rig.clock())

    assert await rig.poster().post_open(str(PROJECT)) == 0
    assert rig.jira.calls == []


async def test_a_refused_create_keeps_the_closed_code_and_the_question_for_the_next_run(rig: Rig) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    rig.jira.write_error = JiraError(Reason.FORBIDDEN)

    assert await rig.poster().post_open(str(PROJECT)) == 0
    assert (await target(rig)).last_error_code == "jira_forbidden"
    assert await rig.db.list_issues(None, PROJECT) == []

    rig.jira.write_error = None
    assert await rig.poster().post_open(str(PROJECT)) == 1
    assert (await target(rig)).last_error_code is None


@pytest.mark.parametrize(("status", "code"), [(None, "jira_not_connected"), ("expired", "jira_reconnect_required")])
async def test_without_a_usable_acting_account_nothing_is_sent_and_the_code_is_kept(
    rig: Rig, status: str | None, code: str
) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    if status is None:
        del rig.connections.status[ACTING]
    else:
        rig.connections.status[ACTING] = status

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert (await target(rig)).last_error_code == code
    assert rig.jira.calls == []


async def test_a_project_without_a_story_or_subtask_type_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))

    async def project(_key: str) -> object:
        return JiraProject("Shop", "1", None, None)

    monkeypatch.setattr(rig.jira, "project", project)

    assert await rig.poster().post_open(str(PROJECT)) == 0
    assert (await target(rig)).last_error_code == "jira_issue_types_missing"


async def test_the_notice_never_raises_when_the_service_fails(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await rig.enable()

    async def broken(*_: object, **__: object) -> None:
        raise OSError("down")

    monkeypatch.setattr(rig.service, "list_gaps", broken)

    await rig.poster().send(str(PROJECT), None)  # contained: the gap run is never failed by Jira
