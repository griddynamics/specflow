"""Who resolves a gap (docs/graph/gap/answers.md "Who answers", data-model.md "Relational store").

A gap has at most one assignee, a member of the project; any member assigns or clears it, and the host checks that
the assignee is a member. The assignee is a ``gap_assignment`` row (``GapRecords``), never a graph property: only the
inbox and the agent's list read it, and assigning does not move the gap's ``updated_at`` (an answer form that is open
does not go stale).

- ``NO_ASSIGNEE``: the ``assigned_to`` filter value of the gaps nobody is assigned to.
- ``follow_assignments``: what the events a step logs do to assignments. A follow-up gap (event ``created`` with
  ``follows_up``) takes the assignee of the gap it follows up (event ``assigned`` with ``from_gap``, the step's actor);
  a gap that leaves the graph (``dismissed``, ``obsolete``) loses its row.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from graph.gap.models import AssignmentRow, EventRow
from graph.gap.ports import GapRecords

NO_ASSIGNEE: Final = "none"
ASSIGNED: Final = "assigned"
UNASSIGNED: Final = "unassigned"
LEFT_GRAPH: Final = frozenset({"dismissed", "obsolete"})


async def follow_assignments(records: GapRecords, project_id: str, events: Sequence[EventRow]) -> None:
    """Carry assignments over to the follow-ups among ``events`` and drop those of the gaps that left the graph (one
    read of the project's assignments when a follow-up was created)."""

    follow_ups = [
        (event.gap_id, str(event.detail["follows_up"]), event)
        for event in events
        if event.event == "created" and event.gap_id and event.detail.get("follows_up")
    ]
    gone = sorted({event.gap_id for event in events if event.event in LEFT_GRAPH and event.gap_id})
    current = await records.assignments(project_id) if follow_ups else {}
    carried: list[EventRow] = []
    for new, old, event in follow_ups:
        row = current.get(old)
        if row is None:
            continue
        await records.assign(
            project_id, AssignmentRow(gap_id=new, user_id=row.user_id, assigned_by=event.actor, assigned_at=event.at)
        )
        carried.append(
            EventRow(
                gap_id=new,
                at=event.at,
                actor=event.actor,
                event=ASSIGNED,
                detail={"user_id": row.user_id, "from_gap": old},
            )
        )
    for gap_id in gone:
        await records.unassign(project_id, gap_id)
    if carried:
        await records.log(project_id, carried)
