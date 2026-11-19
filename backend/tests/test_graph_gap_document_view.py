"""The reads behind the interactive Gaps screen (docs/graph/gap/answers.md "The question card"; FR-GWEB-04): the inbox
order by reach and the witness lines with their document and section.

The corpus world is one full gap run with the recorded loop (``gap_answer_support``).
"""

from __future__ import annotations

from gap_acceptance_support import Planted
from gap_answer_support import answer_world, service_of
from gap_port_fakes import MemoryPlanView
from graph.gap.models import TaskRef

ORDERS_LINEAGE = "upload:orders-spec"


async def test_fr_gweb_04_the_inbox_orders_a_severity_by_the_plan_tasks_behind_a_gap() -> None:
    rig = await answer_world()
    plain = (await rig.service.list_gaps(rig.project_id, limit=200)).gaps
    review = await rig.review_gap()
    peers = [view.gap_id for view in plain if view.severity == review.severity]
    assert len(peers) > 1 and peers[0] != review.gap_id  # newest first without a plan
    tasks = [TaskRef(task_id=f"TASK::{n}", story_id="STORY::A", status="pending") for n in range(2)]
    planned, *_ = service_of(rig.world, plan=MemoryPlanView({Planted.REVIEW_LIMIT_ORDERS: tasks}))

    ranked = await planned.list_gaps(rig.project_id, limit=200)
    first = await planned.list_gaps(rig.project_id, limit=1)
    rest = await planned.list_gaps(rig.project_id, limit=200, cursor=first.next)

    ordered = [view.gap_id for view in ranked.gaps if view.severity == review.severity]
    assert ordered[0] == review.gap_id and sorted(ordered) == sorted(peers)
    assert [view.gap_id for view in (*first.gaps, *rest.gaps)] == [view.gap_id for view in ranked.gaps]


async def test_fr_gweb_04_a_gap_names_the_document_and_section_of_each_witness_line() -> None:
    rig = await answer_world()

    gap = await rig.cancel_gap()
    listed = next(
        view for view in (await rig.service.list_gaps(rig.project_id, limit=200)).gaps if view.gap_id == gap.gap_id
    )

    (line,) = [item for item in gap.witness if item.quote == Planted.STATED_QUESTION_LINE]
    assert line.document_id == ORDERS_LINEAGE
    assert line.document_title and line.document_path
    assert line.heading_path and line.heading_path[-1] == "Place order"
    assert {item.document_id for item in listed.witness} == {ORDERS_LINEAGE}
    assert all(item.heading_path == () and item.document_title is None for item in listed.witness)
