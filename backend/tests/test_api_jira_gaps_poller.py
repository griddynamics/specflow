"""Tier 1: reading the answers back (``api.jira_gaps.poller``, docs/jira-sync/gaps.md "Answers"): a comment of a project
member (matched by Atlassian account id) answers the question with ``via="jira"``, assigns it, applies it and is
confirmed in a comment; the acting account never answers itself; someone unknown gets one "how to link" comment; one
search per interval; nothing is sent while the binding is expired; every effect holds across a restart. Fakes: the Jira,
the gap service and the rows of ``jira_gaps_support``; no Jira, no database.

Sequence of one poll::

    poller -> acting_gateway (bound member, active?) -> Jira: ONE search (children of the Story, updated window)
        -> per comment: author = acting? skip | member? -> gap service: answer_gap(via jira), assign_gap, apply_now
                                               | stranger? -> Jira: one hint comment (mentions them)
        -> Jira: one confirmation comment -> rows: answered_at -> cursor
"""

from __future__ import annotations

import uuid

import pytest

from api.jira_gaps import text
from db.jira_gaps import GapIssueView
from graph.gap import AnswerInvalidError
from jira.gateway import JiraError
from jira.reasons import Reason
from jira_gaps_support import (
    ACTING,
    ACTING_ACCOUNT,
    ANA,
    ANA_ACCOUNT,
    BO,
    BO_ACCOUNT,
    PROJECT,
    STRANGER,
    Rig,
    build_rig,
    gap,
    plan_of,
)

PID = str(PROJECT)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(monkeypatch)


async def posted(rig: Rig, grouping: str = "one", *gaps: object) -> list[str]:
    """Enable the channel, post the gaps and return the keys of the sub-tasks."""

    await rig.enable(grouping)
    rig.service.add(*(gaps or (gap("G-1"),)))
    await rig.poster().post_open(PID)
    return [c["key"] for c in rig.jira.created() if c["fields"].get("parent")]


async def rows(rig: Rig) -> dict[str, GapIssueView]:
    return {row.gap_id: row for row in await rig.db.list_issues(None, PROJECT)}


def confirmations(rig: Rig, key: str) -> list[str]:
    """The text of what the acting account commented after posting (the sub-task's description is not a comment)."""

    return [text.comment_text(c.doc) for c in rig.jira.comments_by_acting(key)]


# An answer ---------------------------------------------------------------------------------------------------------


async def test_a_members_letter_answers_the_question_as_them_via_jira_and_is_confirmed(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "B")

    assert await rig.poller().poll_once(PID) == 1

    [answer] = rig.service.answers
    assert (answer.gap_id, answer.option_rank, answer.own_text) == ("G-1", 2, None)
    assert (answer.answered_by, answer.via) == (str(ANA), "jira")
    assert rig.service.assigned == [("G-1", str(ANA))]  # nobody held it: the answerer does
    assert rig.service.applied == [str(ANA)]
    [confirmed] = confirmations(rig, key)
    assert "recorded B) Allow many carts: applying in one update build." in confirmed
    assert (await rows(rig))["G-1"].answered_at is not None
    assert ANA_ACCOUNT in str(rig.jira.comments_by_acting(key)[0].doc)  # the person is mentioned


async def test_own_words_are_the_answer_and_a_letter_with_a_full_stop_is_still_a_letter(rig: Rig) -> None:
    keys = await posted(rig, "one", gap("G-1"), gap("G-2"))
    rig.jira.say(keys[0], ANA_ACCOUNT, "We keep one cart, merged at login.")
    rig.jira.say(keys[1], BO_ACCOUNT, "a.")

    assert await rig.poller().poll_once(PID) == 2

    by_gap = {a.gap_id: a for a in rig.service.answers}
    assert by_gap["G-1"].own_text == "We keep one cart, merged at login." and by_gap["G-1"].option_rank is None
    assert by_gap["G-2"].option_rank == 1 and by_gap["G-2"].answered_by == str(BO)
    assert "recorded your own answer" in confirmations(rig, keys[0])[0]


async def test_a_deeply_nested_comment_is_no_answer_and_does_not_stop_the_poll(rig: Rig) -> None:
    # A comment nested deeper than the reader can walk is skipped: the others of the target are still read and the
    # cursor moves, so one person cannot stop the target.
    keys = await posted(rig, "one", gap("G-1"), gap("G-2"))
    deep: dict[str, object] = {"type": "paragraph", "content": [{"type": "text", "text": "A"}]}
    for _ in range(2000):
        deep = {"type": "blockquote", "content": [deep]}
    rig.jira._add(keys[0], ANA_ACCOUNT, {"type": "doc", "version": 1, "content": [deep]})
    rig.jira.say(keys[1], BO_ACCOUNT, "A")

    assert await rig.poller().poll_once(PID) == 1

    assert [a.gap_id for a in rig.service.answers] == ["G-2"]
    assert (await rows(rig))["G-1"].answered_at is None
    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.poll_cursor is not None and target.last_polled_at is not None
    assert text.comment_text({"type": "doc", "version": 1, "content": [deep]}) == ""
    assert not text.is_hint_for({"type": "doc", "version": 1, "content": [deep]}, ANA_ACCOUNT)


async def test_an_assigned_gap_keeps_its_assignee(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.service.gaps["G-1"].assigned_to = str(BO)
    rig.jira.say(key, ANA_ACCOUNT, "A")

    await rig.poller().poll_once(PID)

    assert rig.service.assigned == []


async def test_a_question_is_answered_once_across_polls_and_restarts(rig: Rig) -> None:
    # The comment stays in Jira; a second poll, and a poll of a restarted process, record nothing again.
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "A")
    await rig.poller().poll_once(PID)
    calls = len(rig.jira.calls)

    assert await rig.poller().poll_once(PID) == 0  # a new poller: a restart
    assert await rig.poller().poll_once(PID) == 0

    assert len(rig.service.answers) == 1 and len(rig.service.applied) == 1
    assert len(rig.jira.calls) == calls  # nothing was posted again


async def test_a_second_comment_on_an_answered_question_is_not_read(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "A")
    await rig.poller().poll_once(PID)
    rig.jira.say(key, BO_ACCOUNT, "B")

    assert await rig.poller().poll_once(PID) == 0

    assert len(rig.service.answers) == 1


# Work areas ----------------------------------------------------------------------------------------------------


async def test_in_an_area_subtask_each_numbered_line_answers_its_question(rig: Rig) -> None:
    rig.plan = plan_of({"A1": "Checkout"}, {"T1": "A1", "T2": "A1", "T3": "A1"})
    gaps = [gap("G-1", tasks=["T1"]), gap("G-2", tasks=["T2"]), gap("G-3", tasks=["T3"])]
    [key] = await posted(rig, "area", *gaps)
    rig.jira.say(key, ANA_ACCOUNT, "Thanks for asking, my answers:", "1: B", "2: A")
    rig.jira.say(key, BO_ACCOUNT, "just talking, no number")

    assert await rig.poller().poll_once(PID) == 2

    by_gap = {a.gap_id: a.option_rank for a in rig.service.answers}
    assert by_gap == {"G-1": 2, "G-2": 1}
    assert (await rows(rig))["G-3"].answered_at is None
    [confirmed] = confirmations(rig, key)
    assert "Question 1: recorded B)" in confirmed and "Question 2: recorded A)" in confirmed
    rig.jira.say(key, ANA_ACCOUNT, "3) We merge carts at login")
    assert await rig.poller().poll_once(PID) == 1
    assert rig.service.answers[-1].own_text == "We merge carts at login"


async def test_an_area_answer_for_a_number_that_does_not_exist_or_is_answered_is_ignored(rig: Rig) -> None:
    rig.plan = plan_of({"A1": "Checkout"}, {"T1": "A1"})
    [key] = await posted(rig, "area", gap("G-1", tasks=["T1"]))
    rig.jira.say(key, ANA_ACCOUNT, "7: A")

    assert await rig.poller().poll_once(PID) == 0
    assert rig.service.answers == []


# Who may answer ---------------------------------------------------------------------------------------------------


async def test_the_acting_accounts_own_comments_are_never_read_as_answers(rig: Rig) -> None:
    # The confirmation of an answer, and a comment the acting person typed, say "A": neither answers.
    keys = await posted(rig, "one", gap("G-1"), gap("G-2"))
    rig.jira.say(keys[0], ANA_ACCOUNT, "A")
    rig.jira.say(keys[1], ACTING_ACCOUNT, "A")  # as if the connecting person typed it, or Graphico's own comment

    assert await rig.poller().poll_once(PID) == 1
    assert await rig.poller().poll_once(PID) == 0  # the confirmation of G-1 ("recorded A) ...") is not an answer

    assert [a.gap_id for a in rig.service.answers] == ["G-1"]
    assert (await rows(rig))["G-2"].answered_at is None


async def test_an_unknown_commenter_gets_one_how_to_link_comment_and_nothing_is_recorded(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, STRANGER, "A")
    await rig.poller().poll_once(PID)
    rig.jira.say(key, STRANGER, "A again, still not linked")

    await rig.poller().poll_once(PID)
    await rig.poller().poll_once(PID)

    assert rig.service.answers == []
    [hint] = confirmations(rig, key)
    assert text.HINT_LEAD in hint and "connect your Atlassian account under Integrations" in hint
    assert STRANGER in str(rig.jira.comments_by_acting(key)[0].doc)  # it mentions the person
    assert (await rows(rig))["G-1"].answered_at is None


async def test_each_stranger_gets_their_own_hint(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, STRANGER, "A")
    rig.jira.say(key, "acct-other", "B")

    await rig.poller().poll_once(PID)

    assert len(confirmations(rig, key)) == 2


async def test_a_member_whose_connection_is_gone_is_a_stranger_who_can_link_again(rig: Rig) -> None:
    [key] = await posted(rig)
    del rig.db.accounts[BO_ACCOUNT]  # bo disconnected Atlassian: the account id matches no member
    rig.jira.say(key, BO_ACCOUNT, "A")

    await rig.poller().poll_once(PID)

    assert rig.service.answers == [] and text.HINT_LEAD in confirmations(rig, key)[0]


# Refusals --------------------------------------------------------------------------------------------------------


async def test_own_words_on_a_contradiction_are_refused_once_and_a_letter_is_taken(rig: Rig) -> None:
    [key] = await posted(rig, "one", gap("G-1", kind="contradiction"))
    rig.jira.say(key, ANA_ACCOUNT, "Neither, merge them.")

    assert await rig.poller().poll_once(PID) == 0
    assert await rig.poller().poll_once(PID) == 0  # the refusal is not repeated: the acting account commented after it

    assert rig.service.answers == []
    [refusal] = confirmations(rig, key)
    assert "not recorded, this question takes one option letter" in refusal
    rig.jira.say(key, ANA_ACCOUNT, "B")
    assert await rig.poller().poll_once(PID) == 1
    assert rig.service.answers[0].option_rank == 2


async def test_a_question_answered_elsewhere_is_closed_and_the_person_is_told(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.service.gaps["G-1"].status = "answered"  # answered in Slack meanwhile
    rig.jira.say(key, ANA_ACCOUNT, "A")

    assert await rig.poller().poll_once(PID) == 0

    assert (await rows(rig))["G-1"].answered_at is not None  # closed: never read again
    assert "not recorded, this question is answered now" in confirmations(rig, key)[0]
    assert rig.service.answers == []


async def test_a_gap_that_is_gone_is_closed(rig: Rig) -> None:
    [key] = await posted(rig)
    del rig.service.gaps["G-1"]
    rig.jira.say(key, ANA_ACCOUNT, "A")

    await rig.poller().poll_once(PID)

    assert (await rows(rig))["G-1"].answered_at is not None


async def test_a_refused_answer_of_the_service_is_said_and_the_question_stays_open(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    [key] = await posted(rig)

    async def refuse(*_: object) -> None:
        raise AnswerInvalidError(field="own_text", reason="too_long")

    monkeypatch.setattr(rig.service, "answer_gap", refuse)
    rig.jira.say(key, ANA_ACCOUNT, "A")

    await rig.poller().poll_once(PID)

    assert "not recorded, the answer was refused" in confirmations(rig, key)[0]
    assert (await rows(rig))["G-1"].answered_at is None


# Bounded and stopped ---------------------------------------------------------------------------------------------


async def test_one_search_per_target_per_poll_however_many_subtasks(rig: Rig) -> None:
    await posted(rig, "one", *(gap(f"G-{n}") for n in range(1, 6)))

    await rig.poller().poll_once(PID)
    await rig.poller().poll_once(PID)

    assert len(rig.jira.searches) == 2 and {parent for parent, _ in rig.jira.searches} == {"SHOP-1"}


async def test_the_window_is_everything_at_first_and_then_only_what_changed_since_the_cursor(rig: Rig) -> None:
    await posted(rig)
    poller = rig.poller()
    await poller.poll_once(PID)
    await poller.poll_once(PID)

    assert rig.jira.searches[0][1] is None
    assert isinstance(rig.jira.searches[1][1], int) and rig.jira.searches[1][1] >= 1
    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.poll_cursor is not None and target.last_polled_at is not None


async def test_an_expired_binding_stops_the_poll_before_any_jira_call_and_resumes_after_reconnect(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "A")
    rig.connections.status[ACTING] = "expired"
    sent = len(rig.jira.calls)

    assert await rig.poller().poll_once(PID) == 0
    assert await rig.poller().tick() == 0

    assert rig.jira.searches == [] and len(rig.jira.calls) == sent  # nothing reached Jira
    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.last_error_code == "jira_reconnect_required"
    rig.connections.status[ACTING] = "active"  # the member reconnected
    assert await rig.poller().tick() == 1
    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.last_error_code is None


async def test_a_project_with_no_bound_member_polls_nothing(rig: Rig) -> None:
    await posted(rig)
    del rig.connections.status[ACTING]

    assert await rig.poller().poll_once(PID) == 0

    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.last_error_code == "jira_not_connected"
    assert rig.jira.searches == []


async def test_a_bound_member_who_lost_access_to_the_project_answers_nothing_and_the_code_is_kept(rig: Rig) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "A")
    rig.jira.read_error = JiraError(Reason.FORBIDDEN)

    assert await rig.poller().poll_once(PID) == 0

    assert rig.service.answers == []
    target = await rig.db.get_target(None, PROJECT)
    assert target is not None and target.last_error_code == "jira_forbidden"
    rig.jira.read_error = None
    assert await rig.poller().poll_once(PID) == 1  # the comment was kept in Jira: nothing was lost


async def test_a_disabled_target_or_one_without_open_questions_is_not_searched(rig: Rig) -> None:
    await posted(rig)
    await rig.db.mark_answered(None, PROJECT, "G-1", now=rig.clock())
    assert await rig.poller().poll_once(PID) == 0
    assert await rig.poller().poll_once(str(uuid.uuid4())) == 0  # no target at all
    assert rig.jira.searches == []


async def test_a_failing_project_never_stops_the_others_in_a_round(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    await posted(rig)
    poller = rig.poller()

    async def broken(_project_id: str) -> int:
        raise OSError("storage down")

    monkeypatch.setattr(poller, "poll_once", broken)

    assert await poller.tick() == 0  # contained and logged by class only


async def test_the_comment_text_is_never_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    [key] = await posted(rig)
    rig.jira.say(key, ANA_ACCOUNT, "SECRET-CANARY-7731 we keep one cart")

    with caplog.at_level("DEBUG"):
        await rig.poller().poll_once(PID)

    assert "SECRET-CANARY-7731" not in caplog.text
    assert "SECRET-CANARY-7731" not in str(confirmations(rig, key))  # and it is not quoted back
