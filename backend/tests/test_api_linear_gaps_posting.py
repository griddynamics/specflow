"""Tier 1: posting the open questions to Linear (``api.linear_gaps.poster``, docs/linear/gaps.md "Posting"): one parent
issue per project created once and reused, a sub-issue per question or per work area with numbered questions,
lettered options, severity as priority, the label, every question posted once (also after a restart or a crash
between the create and its row), and a refused Linear call kept as a closed code. Fakes: the ``FakeLinear``
workspace, the gap service and the rows; no Linear, no database."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from api.linear_gaps.poster import OPEN_MAX
from db.linear_gaps import GapTargetView
from jira_gaps_support import gap, plan_of
from linear import ids
from linear.errors import LinearForbiddenError, LinearUnauthorizedError
from linear_gaps_support import ACTING, PROJECT, Rig, build_rig, severe


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(monkeypatch)


async def target(rig: Rig) -> GapTargetView:
    found = await rig.db.get_target(None, PROJECT)
    assert found is not None
    return found


# One sub-issue per gap ---------------------------------------------------------------------------------------------


async def test_the_first_post_creates_the_parent_once_and_a_sub_issue_per_question(rig: Rig) -> None:
    # Scenario: two open questions, grouping "one"; a second gap run changes nothing.
    #   gap runner --send--> poster --create parent--> Linear; poster --create sub-issue x2--> Linear; rows written
    await rig.enable("one")
    rig.service.add(gap("G-1"), gap("G-2"))
    poster = rig.poster()

    assert await poster.post_open(str(PROJECT)) == 2
    assert await poster.post_open(str(PROJECT)) == 0  # posted once per gap

    [parent] = [i for i in rig.issues() if i.parent_id is None]
    children = rig.children()
    assert parent.title == "Open questions · Shop" and parent.id == str(ids.node_uuid(PROJECT, ids.KIND_PARENT, "gaps"))
    assert [c.id for c in children] == [str(ids.node_uuid(PROJECT, ids.KIND_GAP, g)) for g in ("G-1", "G-2")]
    assert all(c.parent_id == parent.id and c.team_id == rig.team.id for c in children)
    found = await target(rig)
    assert (found.parent_issue_identifier, str(found.parent_issue_id)) == (parent.identifier, parent.id)
    rows = await rig.db.list_issues(None, PROJECT)
    assert [(r.gap_id, r.number, r.answered_at) for r in rows] == [("G-1", 1, None), ("G-2", 1, None)]


async def test_only_the_questions_for_people_are_asked_for(rig: Rig) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))

    await rig.poster().post_open(str(PROJECT))

    assert rig.service.listed and all(f["route"] == ["ask"] and f["status"] == ["open"] for f in rig.service.listed)


async def test_a_question_is_a_lettered_list_with_how_to_answer_and_the_gap_label(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1", title="Cart rule"))

    await rig.poster().post_open(str(PROJECT))

    [child] = rig.children()
    body = child.description or ""
    assert child.title == "Cart rule"
    assert "Which cart rule applies to G-1?" in body and "Why it matters: The documents disagree." in body
    assert "- **A)** Keep one cart (recommended) - One cart per shopper" in body
    assert "- **B)** Allow many carts" in body
    assert body.endswith("Reply in a comment with a letter or your own answer.")
    assert [label.name for label in child.labels] == ["graphico-gap"]


async def test_the_label_the_team_already_has_is_used_not_made_again(rig: Rig) -> None:
    await rig.enable()
    made = await rig.linear.create_label(rig.team.id, "7b2f6f9a-0f5e-4c1c-9d51-6a4f4d0d9a01", "graphico-gap")
    rig.service.add(gap("G-1"), gap("G-2"))

    await rig.poster().post_open(str(PROJECT))

    assert rig.linear.count("create_label") == 1
    assert {label.id for child in rig.children() for label in child.labels} == {made.id}


@pytest.mark.parametrize(("severity", "priority"), [("blocker", 1), ("major", 2), ("minor", 4)])
async def test_the_severity_of_a_question_is_the_priority_of_its_issue(rig: Rig, severity: str, priority: int) -> None:
    await rig.enable()
    rig.service.add(severe("G-1", severity))

    await rig.poster().post_open(str(PROJECT))

    assert [child.priority for child in rig.children()] == [priority]


async def test_the_issues_go_into_the_chosen_linear_project(rig: Rig) -> None:
    board = rig.linear.add_project(rig.team.id, "Shop board")
    await rig.enable(linear_project_id=board.id)
    rig.service.add(gap("G-1"))

    await rig.poster().post_open(str(PROJECT))

    assert {issue.project_id for issue in rig.issues()} == {board.id}


async def test_a_later_gap_run_posts_only_the_new_questions_under_the_same_parent(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    rig.service.add(gap("G-2"))

    await rig.channel().send(str(PROJECT), None)  # the notice of the gap runner

    assert len([i for i in rig.issues() if i.parent_id is None]) == 1
    assert len(rig.children()) == 2


async def test_a_restarted_process_does_not_post_a_question_again(rig: Rig) -> None:
    # Everything it knows is in the rows: a new poster (a restart) reads them and posts nothing.
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    writes = len(rig.linear.writes)

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert len(rig.linear.writes) == writes


async def test_a_crash_between_the_create_and_its_row_does_not_post_the_question_twice(rig: Rig) -> None:
    # The sub-issue exists in Linear, its row was never written: the client id finds it again.
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    rig.db.issues.clear()
    count = len(rig.issues())

    assert await rig.poster().post_open(str(PROJECT)) == 1

    assert len(rig.issues()) == count
    [row] = await rig.db.list_issues(None, PROJECT)
    assert str(row.issue_id) == rig.children()[0].id and row.identifier == rig.children()[0].identifier


async def test_an_answered_question_is_not_posted_again_when_it_is_open_again(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    await rig.poster().post_open(str(PROJECT))
    await rig.db.mark_answered(None, PROJECT, "G-1", now=rig.linear.now())

    assert await rig.poster().post_open(str(PROJECT)) == 0


async def test_fifty_questions_are_posted_at_once_and_the_rest_after_the_next_run(rig: Rig) -> None:
    await rig.enable("one")
    rig.service.add(*(gap(f"G-{n:03d}") for n in range(OPEN_MAX + 2)))

    assert await rig.poster().post_open(str(PROJECT)) == OPEN_MAX
    assert await rig.poster().post_open(str(PROJECT)) == 2

    rows = await rig.db.list_issues(None, PROJECT)
    assert len(rows) == OPEN_MAX + 2 and len(rig.children()) == OPEN_MAX + 2


async def test_a_destination_changed_during_the_pass_gets_nothing_of_the_old_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.enable("one")
    rig.service.add(gap("G-1"))
    other = rig.linear.add_team("OPS")
    create = rig.linear.create_issue

    async def another_team_meanwhile(issue):  # the owner saves another team after the parent was made
        made = await create(issue)
        rig.db.targets[PROJECT] = replace(rig.db.targets[PROJECT], team_id=other.id, team_key="OPS")
        return made

    monkeypatch.setattr(rig.linear, "create_issue", another_team_meanwhile)

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert (await target(rig)).parent_issue_id is None and await rig.db.list_issues(None, PROJECT) == []


# One sub-issue per work area ------------------------------------------------------------------------------------


async def post_by_area(rig: Rig, tasks: dict[str, str], *gaps: Any) -> int:
    """The channel on with grouping ``area`` over a plan with the work areas A1 (Checkout) and A2 (Search) and the
    tasks ``tasks`` (id to area); ``gaps`` are open and posted. How many were posted."""

    await rig.enable("area")
    rig.plan = plan_of({"A1": "Checkout", "A2": "Search"}, tasks)
    rig.service.add(*gaps)
    return await rig.poster().post_open(str(PROJECT))


async def test_questions_of_one_work_area_share_an_issue_with_numbered_questions(rig: Rig) -> None:
    # Scenario: three questions, two reach tasks of area A1 and one has no plan task (General).
    gaps = (severe("G-1", "major", tasks=["T1"]), severe("G-2", "blocker", tasks=["T2"]), severe("G-3", "minor"))

    assert await post_by_area(rig, {"T1": "A1", "T2": "A1", "T3": "A2"}, *gaps) == 3

    first, general = rig.children()
    assert [first.title, general.title] == ["Checkout · open questions", "General · open questions"]
    body = first.description or ""
    assert "**1. Cart question G-1**" in body and "**2. Cart question G-2**" in body
    assert "one line per question: the number, a colon" in body
    assert (first.priority, general.priority) == (1, 4)  # the most severe question of the issue
    rows = {r.gap_id: (str(r.issue_id), r.number) for r in await rig.db.list_issues(None, PROJECT)}
    assert rows["G-1"] == (first.id, 1) and rows["G-2"] == (first.id, 2) and rows["G-3"] == (general.id, 1)


async def test_a_new_question_of_a_known_area_is_a_comment_with_the_next_number(rig: Rig) -> None:
    await post_by_area(rig, {"T1": "A1", "T2": "A1"}, gap("G-1", tasks=["T1"]))
    rig.service.add(gap("G-2", tasks=["T2"]))

    assert await rig.poster().post_open(str(PROJECT)) == 1

    [child] = rig.children()
    [comment] = await rig.comments_of(child.id)
    assert comment.body.startswith("Graphico: ") and "**2. Cart question G-2**" in comment.body
    rows = {r.gap_id: (str(r.issue_id), r.number) for r in await rig.db.list_issues(None, PROJECT)}
    assert rows["G-2"] == (child.id, 2)


# Nothing to post, or Linear refuses ------------------------------------------------------------------------------


async def test_a_disabled_or_missing_target_posts_nothing(rig: Rig) -> None:
    rig.service.add(gap("G-1"))
    assert await rig.poster().post_open(str(PROJECT)) == 0  # no target
    await rig.enable()
    await rig.db.save_target(
        None,
        PROJECT,
        team_id=rig.team.id,
        team_key="ENG",
        linear_project_id=None,
        grouping="one",
        enabled=False,
        now=rig.linear.now(),
    )

    assert await rig.poster().post_open(str(PROJECT)) == 0
    assert rig.linear.calls == []


async def test_a_refused_create_keeps_the_closed_code_and_the_question_for_the_next_run(rig: Rig) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    rig.linear.fail_next(LinearForbiddenError(), method="create_issue")

    assert await rig.poster().post_open(str(PROJECT)) == 0
    assert (await target(rig)).last_error_code == "linear_forbidden"
    assert await rig.db.list_issues(None, PROJECT) == []

    assert await rig.poster().post_open(str(PROJECT)) == 1
    assert (await target(rig)).last_error_code is None


@pytest.mark.parametrize(("status", "code"), [(None, "linear_not_connected"), ("expired", "linear_reconnect_required")])
async def test_without_a_usable_bound_member_nothing_is_sent_and_the_reason_is_kept(
    rig: Rig, status: str | None, code: str
) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    if status is None:
        del rig.connections.status[ACTING]
    else:
        rig.connections.status[ACTING] = status

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert (await target(rig)).last_error_code == code and rig.linear.calls == []


async def test_a_rejected_token_during_a_pass_is_a_reconnect_not_a_crash(rig: Rig) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    rig.linear.fail_next(LinearUnauthorizedError())

    assert await rig.poster().post_open(str(PROJECT)) == 0

    assert (await target(rig)).last_error_code == "linear_reconnect_required"


async def test_gap_runs_going_off_stops_the_notice_quietly(rig: Rig) -> None:
    await rig.enable()
    rig.service.add(gap("G-1"))
    rig.runtime.gaps = None

    await rig.channel().send(str(PROJECT), None)

    assert rig.linear.calls == []
