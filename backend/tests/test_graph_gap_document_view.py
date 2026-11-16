"""The reads behind the interactive Gaps screen and the document view (docs/graph/gap/answers.md "The question card";
reconcile.md "Download with answers"; FR-GWEB-04, FR-GWEB-05): the inbox order by reach, the witness lines with their
document and section, and the answers each document holds that its source file does not yet.

The corpus world is one full gap run with the recorded loop (``gap_answer_support``).
"""

from __future__ import annotations

from gap_acceptance_support import Planted
from gap_answer_support import MEMBER, answer_world, service_of
from gap_port_fakes import MemoryPlanView
from graph.gap.corpus import clarifications_lineage
from graph.gap.models import AnswerInput, TaskRef

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


async def test_fr_gweb_05_document_answers_list_applied_amendments_and_the_clarifications_document() -> None:
    rig = await answer_world()
    cancel, review = await rig.cancel_gap(), await rig.review_gap()
    amended = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=cancel.gap_id, option_rank=1, answered_by=MEMBER)
    )
    placed = await rig.service.answer_gap(
        rig.project_id,
        AnswerInput(gap_id=review.gap_id, own_text="500 EUR everywhere.", placement="document", answered_by=MEMBER),
    )

    pending = await rig.service.document_answers(rig.project_id)
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    applied = await rig.service.document_answers(rig.project_id)

    assert pending.lineages == () and pending.clarifications == ()  # nothing sent to a build yet
    assert applied.clarifications_lineage == clarifications_lineage(rig.project_id)
    (orders,) = applied.lineages
    assert orders.lineage == ORDERS_LINEAGE
    (answer,) = orders.answers
    assert (answer.clr_id, answer.gap_id, answer.title) == (amended.clr_id, cancel.gap_id, cancel.title)
    assert (answer.status, answer.amendments) == ("active", len(amended.amendments))
    (section,) = applied.clarifications
    assert (section.clr_id, section.gap_id, section.status) == (placed.clr_id, review.gap_id, "pending")
