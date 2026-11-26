"""Tier 1: reading an answer from a Linear comment (``api.linear_gaps.answers``, docs/linear/gaps.md "Answers") through
the one path the webhook and the catch-up read share: a member answers by option letter or by their own words (the
answer is recorded as ``via="linear"``, applied, confirmed in a comment and the sub-issue moved to the team's first
completed state), a person who is no member is told how to link once, the channel's own comments, an integration's
and a second delivery change nothing, and what a lost delivery left is read by the catch-up. Fakes:
``linear_gaps_support``."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace

import pytest

from api.linear_gaps.answers import Incoming
from api.linear_gaps.webhook import verify
from db.linear_gaps import GapTargetSaved
from jira_gaps_support import gap, plan_of
from linear.errors import LinearForbiddenError
from linear.models import Comment, Issue, NewIssue
from linear_gaps_support import (
    ACTING,
    ACTING_LINEAR,
    ANA,
    ANA_LINEAR,
    ORG,
    OTHER_ORG,
    PROJECT,
    SIGNING_KEY,
    STRANGER,
    Rig,
    build_rig,
    comment_event,
    delivery,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(monkeypatch)


def incoming(comment: Comment, *, org: str = ORG) -> Incoming:
    return Incoming(comment.id, comment.issue_id, org, comment.user_id, comment.body)


async def posted(rig: Rig, grouping: str = "one", *gaps) -> list[Issue]:
    """The channel on, ``gaps`` (default one) open and posted; the issues it made under its parent."""

    await rig.enable(grouping)
    rig.service.add(*(gaps or (gap("G-1"),)))
    await rig.poster().post_open(str(PROJECT))
    return rig.children()


async def settle(channel) -> None:
    tasks = list(channel._tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


# A member answers ---------------------------------------------------------------------------------------------------


async def test_a_member_answers_by_letter_and_the_answer_is_recorded_applied_confirmed_and_the_issue_completed(
    rig: Rig,
) -> None:
    # Scenario: Ana comments "A" on the sub-issue of G-1.
    #   Linear --Comment create--> LinearAnswers --record_and_apply(via=linear)--> service; confirmation comment; Done
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")

    assert await rig.answers().process(incoming(comment)) == 1

    [answer] = rig.service.answers
    assert (answer.gap_id, answer.option_rank, answer.own_text, answer.via) == ("G-1", 1, None, "linear")
    assert answer.answered_by == str(ANA) and rig.service.assigned == [("G-1", str(ANA))]
    assert rig.service.applied == [str(ANA)]
    [confirmation] = await rig.comments_of(child.id)
    assert confirmation.body.startswith("Graphico: thank you.")
    assert "- recorded A) Keep one cart: applying in one update build." in confirmation.body
    assert (await rig.linear.issue(child.id)).state.type == "completed"
    [row] = await rig.db.list_issues(None, PROJECT)
    assert row.answered_at is not None


async def test_a_member_answers_with_their_own_words(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "Neither:  one cart", "per branch of the shop")

    await rig.answers().process(incoming(comment))

    [answer] = rig.service.answers
    assert (answer.option_rank, answer.own_text) == (None, "Neither: one cart per branch of the shop")
    [confirmation] = await rig.comments_of(child.id)
    assert "- recorded your own answer: applying in one update build." in confirmation.body


async def test_a_letter_with_punctuation_in_any_case_is_the_option(rig: Rig) -> None:
    [child] = await posted(rig)

    await rig.answers().process(incoming(rig.say(child.id, ANA_LINEAR, "b.")))

    assert [(a.option_rank, a.own_text) for a in rig.service.answers] == [(2, None)]


async def test_the_member_is_found_by_their_linear_user_in_the_workspace_of_the_comment(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")

    assert await rig.answers().process(incoming(comment, org=OTHER_ORG)) == 0  # the same user id, another workspace

    assert rig.service.answers == []


async def test_the_acting_member_may_answer_too_and_the_channels_own_comments_are_not_answers(rig: Rig) -> None:
    [child] = await posted(rig)
    await rig.linear.create_comment(child.id, "Graphico: recorded A) something: saved.")
    own = (await rig.comments_of(child.id))[0]

    assert await rig.answers().process(incoming(own)) == 0
    assert rig.service.answers == []

    assert await rig.answers().process(incoming(rig.say(child.id, ACTING_LINEAR, "B"))) == 1
    assert rig.service.answers[0].answered_by == str(ACTING)


async def test_a_comment_of_an_integration_is_not_an_answer(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")

    assert await rig.answers().process(replace(incoming(comment), user_id=None)) == 0

    assert rig.service.answers == []


async def test_a_comment_in_an_issue_the_channel_did_not_post_does_nothing(rig: Rig) -> None:
    await posted(rig)
    other = await rig.linear.create_issue(NewIssue(str(uuid.uuid4()), rig.team.id, "Something else"))
    comment = rig.say(other.id, ANA_LINEAR, "A")
    writes = len(rig.linear.writes)

    assert await rig.answers().process(incoming(comment)) == 0
    assert await rig.answers().process(replace(incoming(comment), issue_id="not-an-id")) == 0

    assert rig.service.answers == [] and len(rig.linear.writes) == writes


async def test_the_same_comment_twice_records_and_confirms_once(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")

    first = await rig.answers().process(incoming(comment))
    second = await rig.answers().process(incoming(comment))

    assert (first, second) == (1, 0)
    assert len(rig.service.answers) == 1 and len(await rig.comments_of(child.id)) == 1


async def test_a_disabled_channel_reads_nothing(rig: Rig) -> None:
    [child] = await posted(rig)
    rig.db.targets[PROJECT] = replace(rig.db.targets[PROJECT], enabled=False)

    assert await rig.answers().process(incoming(rig.say(child.id, ANA_LINEAR, "A"))) == 0

    assert rig.service.answers == []


# A person who is no member ------------------------------------------------------------------------------------------


async def test_a_person_who_is_no_member_is_told_how_to_link_once_and_nothing_is_recorded(rig: Rig) -> None:
    [child] = await posted(rig)
    first = rig.say(child.id, STRANGER, "A")
    second = rig.say(child.id, STRANGER, "I think A is right")

    for comment in (first, second):
        assert await rig.answers().process(incoming(comment)) == 0

    [hint] = await rig.comments_of(child.id)
    assert "not from a member of the project with a connected Linear account" in hint.body
    assert hint.body.startswith("Graphico: ") and rig.service.answers == []
    assert [r.answered_at for r in await rig.db.list_issues(None, PROJECT)] == [None]


async def test_a_second_person_gets_their_own_hint(rig: Rig) -> None:
    [child] = await posted(rig)
    other = rig.linear.add_user("Dee")

    await rig.answers().process(incoming(rig.say(child.id, STRANGER, "A")))
    await rig.answers().process(incoming(rig.say(child.id, other, "A")))

    assert len(await rig.comments_of(child.id)) == 2


# Grouping by area ---------------------------------------------------------------------------------------------------


async def test_numbered_lines_answer_their_questions_and_the_issue_stays_open(rig: Rig) -> None:
    rig.plan = plan_of({"A1": "Checkout"}, {"T1": "A1", "T2": "A1"})
    [child] = await posted(rig, "area", gap("G-1", tasks=["T1"]), gap("G-2", tasks=["T2"]), gap("G-3", tasks=["T1"]))
    comment = rig.say(child.id, ANA_LINEAR, "1: B", "2) my own words", "4: A")  # 4 is no question of the issue

    assert await rig.answers().process(incoming(comment)) == 2

    assert [(a.gap_id, a.option_rank, a.own_text) for a in rig.service.answers] == [
        ("G-1", 2, None),
        ("G-2", None, "my own words"),
    ]
    [confirmation] = await rig.comments_of(child.id)
    assert "- Question 1: recorded B) Allow many carts: applying in one update build." in confirmation.body
    assert "- Question 2: recorded your own answer" in confirmation.body
    assert (await rig.linear.issue(child.id)).state.type != "completed"  # only a sub-issue per gap is completed
    rows = {r.gap_id: r.answered_at is not None for r in await rig.db.list_issues(None, PROJECT)}
    assert rows == {"G-1": True, "G-2": True, "G-3": False}


# Refusals ----------------------------------------------------------------------------------------------------------


async def test_a_contradiction_takes_one_letter_and_stays_open(rig: Rig) -> None:
    [child] = await posted(rig, "one", gap("G-1", kind="contradiction"))

    assert await rig.answers().process(incoming(rig.say(child.id, ANA_LINEAR, "a third way"))) == 0

    [confirmation] = await rig.comments_of(child.id)
    assert "- not recorded, this question takes one option letter." in confirmation.body
    assert rig.service.answers == [] and (await rig.db.list_issues(None, PROJECT))[0].answered_at is None
    assert (await rig.linear.issue(child.id)).state.type != "completed"


async def test_a_question_answered_elsewhere_is_closed_here_and_said_so(rig: Rig) -> None:
    [child] = await posted(rig)
    rig.service.gaps["G-1"].status = "answered"  # answered on the web meanwhile

    assert await rig.answers().process(incoming(rig.say(child.id, ANA_LINEAR, "A"))) == 0

    [confirmation] = await rig.comments_of(child.id)
    assert "- not recorded, this question is answered now." in confirmation.body
    assert (await rig.db.list_issues(None, PROJECT))[0].answered_at is not None


async def test_a_gap_the_service_no_longer_has_is_closed_here(rig: Rig) -> None:
    [child] = await posted(rig)
    del rig.service.gaps["G-1"]

    await rig.answers().process(incoming(rig.say(child.id, ANA_LINEAR, "A")))

    [confirmation] = await rig.comments_of(child.id)
    assert "not recorded, this question is no longer open." in confirmation.body


async def test_a_linear_failure_after_the_record_does_not_undo_it(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")
    rig.linear.fail_next(LinearForbiddenError(), method="create_comment")

    assert await rig.answers().process(incoming(comment)) == 1

    assert len(rig.service.answers) == 1
    assert (await rig.db.list_issues(None, PROJECT))[0].answered_at is not None


async def test_the_answer_is_recorded_even_when_the_bound_member_cannot_confirm(rig: Rig) -> None:
    [child] = await posted(rig)
    comment = rig.say(child.id, ANA_LINEAR, "A")
    rig.connections.status[ACTING] = "expired"

    assert await rig.answers().process(incoming(comment)) == 1

    assert len(rig.service.answers) == 1 and await rig.comments_of(child.id) == []


# The channel: webhook comments and the catch-up read ------------------------------------------------------------


async def test_a_comment_from_the_webhook_is_read_in_the_background_once_per_delivery(rig: Rig) -> None:
    [child] = await posted(rig)
    channel = rig.channel()
    raw, headers = delivery(comment_event(rig.say(child.id, ANA_LINEAR, "A")))
    event = verify(raw, headers["Linear-Signature"], SIGNING_KEY, int(time.time() * 1000))

    channel.receive(event, "d-1")
    channel.receive(event, "d-1")  # Linear delivered it twice
    await settle(channel)

    assert len(rig.service.answers) == 1 and len(await rig.comments_of(child.id)) == 1


async def test_the_catch_up_reads_what_a_lost_delivery_left_and_leaves_the_rest(rig: Rig) -> None:
    # Scenario: Linear gave up on two deliveries (down for hours); the next start reads the open issues' comments.
    first, second = await posted(rig, "one", gap("G-1"), gap("G-2"))
    rig.say(first.id, ANA_LINEAR, "B")
    rig.say(second.id, STRANGER, "A")  # no member: hinted, not recorded

    assert await rig.channel().catch_up(str(PROJECT)) == 1

    assert [(a.gap_id, a.option_rank) for a in rig.service.answers] == [("G-1", 2)]
    assert len(await rig.comments_of(second.id)) == 1  # the hint
    assert await rig.channel().catch_up(str(PROJECT)) == 0  # a second read changes nothing
    assert len(rig.service.answers) == 1 and len(await rig.comments_of(first.id)) == 1


async def test_the_catch_up_reads_only_the_issues_that_still_ask_something_and_not_before_they_were_posted(
    rig: Rig,
) -> None:
    first, second = await posted(rig, "one", gap("G-1"), gap("G-2"))
    await rig.db.mark_answered(None, PROJECT, "G-1", now=rig.linear.now())
    rig.linear.calls.clear()

    await rig.channel().catch_up(str(PROJECT))

    reads = [args for name, args in rig.linear.calls if name == "comments"]
    assert [read[0] for read in reads] == [second.id]
    posted_at = (await rig.db.list_issues(None, PROJECT))[1].posted_at
    assert reads[0][1] is not None and reads[0][1] < posted_at
    assert first.id not in [read[0] for read in reads]


async def test_the_catch_up_runs_at_start_for_every_enabled_target(rig: Rig) -> None:
    [child] = await posted(rig)
    rig.say(child.id, ANA_LINEAR, "A")
    channel = rig.channel()

    channel.start()
    await settle(channel)

    assert len(rig.service.answers) == 1


async def test_saving_the_target_posts_the_questions_then_reads_the_comments_in_them(rig: Rig) -> None:
    [child] = await posted(rig)
    rig.say(child.id, ANA_LINEAR, "A")
    rig.service.add(gap("G-2"))
    channel = rig.channel()

    channel.after_save(str(PROJECT), GapTargetSaved(turned_on=True, destination_changed=False))
    await settle(channel)

    assert len(rig.children()) == 2 and len(rig.service.answers) == 1


async def test_a_save_that_changes_nothing_posts_and_reads_nothing(rig: Rig) -> None:
    await posted(rig)
    channel = rig.channel()
    rig.linear.calls.clear()

    channel.after_save(str(PROJECT), GapTargetSaved(turned_on=False, destination_changed=False))
    await settle(channel)

    assert rig.linear.calls == []


async def test_a_refused_catch_up_keeps_the_closed_code(rig: Rig) -> None:
    await posted(rig)
    rig.linear.fail_next(LinearForbiddenError(), method="comments")

    assert await rig.channel().catch_up(str(PROJECT)) == 0

    assert rig.db.targets[PROJECT].last_error_code == "linear_forbidden"
