"""Gap assignment (docs/graph/gap/answers.md "Who answers"; FR-GWEB-06): one member per gap and any member assigns;
``list_gaps`` filters by assignee; every change is an event; a follow-up takes the assignee of the gap it follows up;
a gap that leaves the graph loses its row.

The corpus world is one full gap run with the recorded loop (``gap_answer_support``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_answer_support import MEMBER, answer_world
from gap_port_fakes import MemoryGapRecords
from graph.gap.assignment import follow_assignments
from graph.gap.errors import GapNotFoundError, GapStateError
from graph.gap.models import AssignmentRow, EventRow, GapView

OWNER = "user-owner-1"
AT = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def assignment_events(gap: GapView) -> list[tuple[str, str, dict[str, Any]]]:
    assert gap.history is not None
    return [
        (event.event, event.actor, dict(event.detail))
        for event in gap.history.events
        if event.event in {"assigned", "unassigned"}
    ]


async def test_fr_gweb_06_a_member_assigns_a_gap_and_the_list_filters_by_assignee() -> None:
    """Given two members, when one assigns a gap to the other, then the gap lists under that member, not under
    "nobody", and its history names who assigned it to whom."""

    rig = await answer_world()
    cancel, review = await rig.cancel_gap(), await rig.review_gap()

    assigned = await rig.service.assign_gap(rig.project_id, cancel.gap_id, MEMBER, by=OWNER)
    again = await rig.service.assign_gap(rig.project_id, cancel.gap_id, MEMBER, by=OWNER)  # no change: no event
    theirs = await rig.service.list_gaps(rig.project_id, assigned_to=MEMBER, limit=200)
    nobody = await rig.service.list_gaps(rig.project_id, assigned_to="none", limit=200)
    every = await rig.service.list_gaps(rig.project_id, limit=200)

    assert assigned.assigned_to == again.assigned_to == MEMBER
    assert [gap.gap_id for gap in theirs.gaps] == [cancel.gap_id]
    assert {gap.gap_id for gap in nobody.gaps} == {gap.gap_id for gap in every.gaps} - {cancel.gap_id}
    assert review.gap_id in {gap.gap_id for gap in nobody.gaps}
    assert {gap.gap_id: gap.assigned_to for gap in every.gaps}[cancel.gap_id] == MEMBER
    assert assignment_events(again) == [("assigned", OWNER, {"user_id": MEMBER, "previous": None})]


async def test_reassigning_and_clearing_log_the_previous_assignee() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()

    await rig.service.assign_gap(rig.project_id, gap.gap_id, OWNER, by=OWNER)
    await rig.service.assign_gap(rig.project_id, gap.gap_id, MEMBER, by=MEMBER)
    cleared = await rig.service.assign_gap(rig.project_id, gap.gap_id, None, by=MEMBER)

    assert cleared.assigned_to is None
    assert assignment_events(cleared) == [
        ("assigned", OWNER, {"user_id": OWNER, "previous": None}),
        ("assigned", MEMBER, {"user_id": MEMBER, "previous": OWNER}),
        ("unassigned", MEMBER, {"previous": MEMBER}),
    ]
    assert await rig.world.records.assignments(rig.project_id) == {}


async def test_a_resolved_or_unknown_gap_is_not_assigned() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    for key, props in rig.world.executor.nodes.items():
        if key[0] == "Gap" and props["gap_id"] == gap.gap_id:
            props["status"] = "resolved"

    with pytest.raises(GapStateError):
        await rig.service.assign_gap(rig.project_id, gap.gap_id, MEMBER, by=OWNER)
    with pytest.raises(GapNotFoundError):
        await rig.service.assign_gap(rig.project_id, "GAP::000000000000", MEMBER, by=OWNER)
    assert await rig.world.records.assignments(rig.project_id) == {}


async def test_a_dismissed_gap_loses_its_assignee() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await rig.service.assign_gap(rig.project_id, gap.gap_id, MEMBER, by=OWNER)

    await rig.service.dismiss_gap(rig.project_id, gap.gap_id, reason="not a gap", by=MEMBER)

    assert gap.gap_id not in await rig.world.records.assignments(rig.project_id)


async def test_the_follow_up_of_a_reverted_answer_takes_the_assignee() -> None:
    rig = await answer_world(ai_answers=True)
    (listed,) = (await rig.service.ai_answers(rig.project_id)).answers
    await rig.service.assign_gap(rig.project_id, listed.gap_id, MEMBER, by=OWNER)
    await rig.service.apply_now(rig.project_id, by="gap-apply")
    for key, props in rig.world.executor.nodes.items():  # as the close check leaves a fixed gap
        if key[0] == "Gap" and props["gap_id"] == listed.gap_id:
            props.update(status="resolved", resolution="answered")
        if key[0] == "Clarification" and props["clr_id"] == listed.clarification.clr_id:
            props["status"] = "applied"

    result = await rig.service.revert_clarification(
        rig.project_id, listed.clarification.clr_id, by=OWNER, reason="wrong"
    )

    follow_up = await rig.service.get_gap(rig.project_id, result.gap_id)
    old = await rig.service.get_gap(rig.project_id, listed.gap_id)
    assert result.follow_up and follow_up.assigned_to == old.assigned_to == MEMBER
    assert assignment_events(follow_up) == [("assigned", OWNER, {"user_id": MEMBER, "from_gap": listed.gap_id})]


async def test_follow_assignments_carries_follow_ups_and_drops_gaps_that_left_the_graph() -> None:
    records = MemoryGapRecords()
    for gap_id in ("GAP::OLD", "GAP::GONE", "GAP::DISMISSED"):
        await records.assign("p", AssignmentRow(gap_id=gap_id, user_id=MEMBER, assigned_by=OWNER, assigned_at=AT))
    events = [
        EventRow(gap_id="GAP::NEW", at=AT, actor="gap-run", event="created", detail={"follows_up": "GAP::OLD"}),
        EventRow(gap_id="GAP::OTHER", at=AT, actor="gap-run", event="created", detail={"follows_up": "GAP::FREE"}),
        EventRow(gap_id="GAP::GONE", at=AT, actor="gap-run", event="obsolete"),
        EventRow(gap_id="GAP::DISMISSED", at=AT, actor=MEMBER, event="dismissed"),
        EventRow(gap_id="GAP::PLAIN", at=AT, actor="gap-run", event="created"),
    ]

    await follow_assignments(records, "p", events)

    current = await records.assignments("p")
    assert {gap_id: row.user_id for gap_id, row in current.items()} == {"GAP::OLD": MEMBER, "GAP::NEW": MEMBER}
    assert current["GAP::NEW"].assigned_by == "gap-run"
    assert [(row.gap_id, row.event, dict(row.detail)) for row in records.event_rows["p"]] == [
        ("GAP::NEW", "assigned", {"user_id": MEMBER, "from_gap": "GAP::OLD"})
    ]
