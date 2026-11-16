"""Acceptance tests of graph.gap: Answers (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from gap_acceptance_support import Planted, pending
from gap_answer_support import (
    MEMBER,
    ORDERS_LINEAGE,
    ORDERS_REVIEW,
    PLACE,
    ScriptedEdits,
    amend,
    answer_world,
)
from gap_run_support import CountingInvestigator
from graph.gap import run_gap
from graph.gap.errors import StaleGapError
from graph.gap.models import AnswerInput, NewGap
from graph.gap.prompts import EDITS_PROMPT

ANSWERS = "ANSWERS"
REPLACES = "REPLACES"
CLAIM = "claim-1"


def clarifications(world) -> dict[str, dict]:
    return {props["clr_id"]: props for (label, _), props in world.executor.nodes.items() if label == "Clarification"}


async def test_fr_gans_01_answer_gap_create_clarification_answers_gap_mode() -> None:
    """Given option A of the cancel-paid-order gap, then the clarification exists with mode `option` and the gap is
    `answered`.
    """

    rig = await answer_world()
    gap = await rig.cancel_gap()

    result = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER, updated_at=gap.updated_at)
    )

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert after.status == "answered"
    clarification = after.clarification
    assert clarification is not None and clarification.clr_id == result.clr_id
    assert (clarification.mode, clarification.placement, clarification.status) == ("option", "amend", "pending")
    assert clarification.option_label == gap.options[0].label and clarification.answered_by == MEMBER
    assert f"{result.clr_id}|{ANSWERS}|{gap.gap_id}" in rig.world.executor.edge_keys(ANSWERS)
    assert f"{result.clr_id}|PLACED_IN|{ORDERS_LINEAGE}" in rig.world.executor.edge_keys("PLACED_IN")


async def test_fr_gans_02_own_answer_turned_into_edits_gap_edits() -> None:
    """Given own text, then `preview_edits` returns the target section before and after, and `answer_gap` with those
    edits stores them unchanged.
    """

    text = "A paid order can be cancelled until it ships; the payment is refunded."
    drafted = {
        "placement": "amend",
        "target": {"fragment_id": PLACE, "anchor_quote": Planted.STATED_QUESTION_LINE, "heading": None},
        "operation": "replace",
        "text": text,
    }
    gateway = ScriptedEdits([drafted])
    rig = await answer_world(gateway=gateway)
    gap = await rig.cancel_gap()

    preview = await rig.service.preview_edits(rig.project_id, gap.gap_id, own_text=text)

    assert gateway.prompts == [EDITS_PROMPT] and preview.drafted and preview.fallback is None
    section = preview.sections[0]
    assert section.fragment_id == PLACE and Planted.STATED_QUESTION_LINE in section.before
    assert Planted.STATED_QUESTION_LINE not in section.after and text in section.after
    assert f"> Clarified by {gap.gap_id}" in section.after
    result = await rig.service.answer_gap(
        rig.project_id,
        AnswerInput(gap_id=gap.gap_id, own_text=text, edits=preview.edits, answered_by=MEMBER),
    )
    (row,) = await rig.world.amendments.for_clarification(rig.project_id, result.clr_id)
    assert row.operation == "replace" and row.anchor_text == Planted.STATED_QUESTION_LINE
    assert row.text.startswith(preview.edits[0].text) and row.status == "pending"
    assert len(gateway.prompts) == 1  # the answer stores the previewed edits; no second draft


async def test_fr_gans_03_answer_rejected_stalegaperror_gap_changed_after_caller() -> None:
    """Given a gap updated by a run after it was read, then `answer_gap` raises `StaleGapError`."""

    rig = await answer_world()
    gap = await rig.cancel_gap()
    seen = gap.updated_at
    owner = f"{gap.gap_id}"
    # A later run re-checks the gap and adds a relation: its updated_at moves.
    executor = rig.world.executor
    key = next(key for key in executor.nodes if key[0] == "Gap" and executor.nodes[key]["gap_id"] == owner)
    executor.nodes[key]["updated_at"] = (seen + timedelta(minutes=5)).isoformat(timespec="milliseconds")

    with pytest.raises(StaleGapError):
        await rig.service.answer_gap(
            rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER, updated_at=seen)
        )
    assert (await rig.service.get_gap(rig.project_id, gap.gap_id)).status == "open"
    assert not rig.world.amendments.rows[rig.project_id]


async def test_fr_gans_04_every_edit_is_one_amendment() -> None:
    """Given the review-limit contradiction answered with "500 EUR everywhere", then one amendment replaces the line
    in `orders-spec.md` and none touches `payments-spec.md`.
    """

    rig = await answer_world()
    gap = await rig.review_gap()
    edit = amend(ORDERS_REVIEW, Planted.REVIEW_LIMIT_ORDERS_LINE, "Orders over 500 EUR are reviewed.")

    result = await rig.service.answer_gap(
        rig.project_id,
        AnswerInput(gap_id=gap.gap_id, own_text="500 EUR everywhere", edits=(edit,), answered_by=MEMBER),
    )

    rows = await rig.world.amendments.for_clarification(rig.project_id, result.clr_id)
    assert [(row.lineage, row.operation, row.anchor_text) for row in rows] == [
        (ORDERS_LINEAGE, "replace", Planted.REVIEW_LIMIT_ORDERS_LINE)
    ]
    assert not await rig.world.amendments.for_lineage(rig.project_id, "upload:payments-spec", statuses=("pending",))


@pending("09")
def test_fr_gans_05_contradiction_answered_amend_placement_replace_losing_line() -> None:
    """Given the review-limit answer applied, then the build's graph holds one review-limit rule of 500 EUR."""


@pending("09")
def test_fr_gans_06_answer_no_single_home_go_project_clarifications() -> None:
    """Given an answer with placement `document`, then the next corpus read lists the clarifications document with
    that section.
    """


@pending("09")
def test_fr_gans_07_applied_text_carry_answer_marker_naming_gap() -> None:
    """Given an applied amendment, then the effective text contains the marker line with the gap id."""


async def test_fr_gans_08_answers_applied_after_quiet_period_apply_now() -> None:
    """Given three answers within the quiet period, then one build is requested; given a blocking development gap
    answered, then a build is requested at once.
    """

    rig = await answer_world()
    results = []
    for gap in (await rig.cancel_gap(), await rig.review_gap(), await rig.gap_about(Planted.VAGUE_REQUIREMENT)):
        answer = AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER)
        results.append(await rig.service.answer_gap(rig.project_id, answer))
    quiet = timedelta(seconds=rig.service.quiet_seconds())
    assert quiet == timedelta(seconds=120) and not rig.builds.requests
    assert all(not item.immediate and item.apply_at > item.apply_at - quiet for item in results)

    applied = await rig.service.apply_now(rig.project_id, by="gap-apply")

    assert rig.builds.requests == [(rig.project_id, "gap_answers", False)]
    assert len(applied.gap_ids) == 3 and set(applied.clr_ids) == {item.clr_id for item in results}
    page = await rig.service.list_gaps(rig.project_id, status="applying")
    assert {gap.gap_id for gap in page.gaps} == set(applied.gap_ids)
    for item in results:
        assert {row.status for row in await rig.world.amendments.for_clarification(rig.project_id, item.clr_id)} == {
            "active"
        }

    raised = await rig.service.raise_gap(
        rig.project_id,
        NewGap(
            kind="missing",
            question="Which refund delay must the Orders service show to the customer?",
            about=(Planted.PLACE_ORDER,),
            origin="development",
            blocking=True,
            story_id="STORY::ORDERS_SERVICE::PLACE_ORDER",
            claim_token=CLAIM,
            created_by="agent-1",
        ),
    )
    rig.world.investigator = CountingInvestigator()  # seed-made report: no recorded model call for this site
    await run_gap(rig.world.context(), mode="site", site_ids=[raised.gap_id])
    blocking = await rig.service.get_gap(rig.project_id, raised.gap_id)
    assert blocking.blocking and blocking.options

    result = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=blocking.gap_id, option_rank=1, answered_by=MEMBER)
    )

    assert result.immediate and rig.builds.requests[-1] == (rig.project_id, "gap_answers", True)
    assert (await rig.service.get_gap(rig.project_id, blocking.gap_id)).status == "applying"


async def test_fr_gans_09_answer_changed_before_applied_replace_pending_clarification() -> None:
    """Given both cases, then the graph shows one pending clarification in the first and a `REPLACES` relation in the
    second.
    """

    rig = await answer_world()
    gap = await rig.cancel_gap()
    first = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER)
    )

    second = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=2, answered_by=MEMBER)
    )

    found = clarifications(rig.world)
    assert second.replaced == first.clr_id and set(found) == {second.clr_id}
    assert found[second.clr_id]["status"] == "pending"
    assert not await rig.world.amendments.for_clarification(rig.project_id, first.clr_id)
    events = [event.event for event in await rig.world.records.events(rig.project_id, gap.gap_id)]
    assert "replaced" in events

    # After it is applied: the gap is resolved; a new answer REPLACES the applied clarification.
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    executor = rig.world.executor
    for key, props in executor.nodes.items():
        if key[0] == "Gap" and props["gap_id"] == gap.gap_id:
            props.update(status="resolved", resolution="answered")
        if key[0] == "Clarification" and props["clr_id"] == second.clr_id:
            props["status"] = "applied"

    third = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=3, answered_by=MEMBER)
    )

    assert f"{third.clr_id}|{REPLACES}|{second.clr_id}" in executor.edge_keys(REPLACES)
    assert (await rig.service.get_gap(rig.project_id, gap.gap_id)).status == "resolved"
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    found = clarifications(rig.world)
    assert (found[second.clr_id]["status"], found[third.clr_id]["status"]) == ("superseded", "pending")
    assert {row.status for row in await rig.world.amendments.for_clarification(rig.project_id, second.clr_id)} == {
        "reverted"
    }


@pending("11")
def test_fr_gans_10_ai_answers_listed_review_revert_clarification_mark() -> None:
    """Given an AI answer reverted, then after the build its text is gone from the graph and a follow-up gap
    `FOLLOWS_UP` the old one.
    """


async def test_fr_gans_11_dismiss_gap_require_reason_delete_gap_node() -> None:
    """Given a dismissal, then the node is gone, `gap_suppression` holds the signature and the event log the reason."""

    rig = await answer_world()
    gap = await rig.gap_about(Planted.VAGUE_REQUIREMENT)

    await rig.service.dismiss_gap(rig.project_id, gap.gap_id, reason="not a gap", by=MEMBER)

    assert gap.gap_id not in rig.world.gap_nodes()
    assert not any(gap.gap_id in key for key in rig.world.executor.edge_keys("ABOUT"))
    suppression = rig.world.records.suppressions[rig.project_id][gap.signature]
    assert (suppression.gap_id, suppression.reason, suppression.dismissed_by) == (gap.gap_id, "not a gap", MEMBER)
    (event,) = [e for e in await rig.world.records.events(rig.project_id, gap.gap_id) if e.event == "dismissed"]
    assert event.detail["reason"] == "not a gap" and event.actor == MEMBER
    assert not await rig.world.records.options(rig.project_id, gap.gap_id)


async def test_fr_gans_12_raise_gap_origin_person_create_open_gap() -> None:
    """Given a person's question about two ids, then the gap exists at once and has options after the site run."""

    rig = await answer_world()
    new = NewGap(
        kind="missing",
        question="Must a refund of a cancelled order go back to the card it was paid with?",
        about=(Planted.PLACE_ORDER, Planted.REFUND_INTERFACE),
        origin="person",
        created_by=MEMBER,
    )

    raised = await rig.service.raise_gap(rig.project_id, new)

    gap = await rig.service.get_gap(rig.project_id, raised.gap_id)
    assert (gap.status, gap.origin, gap.route, gap.severity) == ("open", "person", "ask", "major")
    assert {node.spec_id for node in gap.about} == {Planted.PLACE_ORDER, Planted.REFUND_INTERFACE}
    assert not gap.options
    rig.world.investigator = CountingInvestigator()  # seed-made report: no recorded model call for this site
    result = await run_gap(rig.world.context(), mode="site", site_ids=[raised.gap_id])
    assert result.counts["sites"]["investigated"] == 1
    gap = await rig.service.get_gap(rig.project_id, raised.gap_id)
    assert len(gap.options) >= 2 and sum(option.recommended for option in gap.options) == 1
    assert gap.question == new.question and gap.status == "open"


async def test_fr_gans_13_any_project_member_able_answer_dismiss_revert() -> None:
    """Given a member who is not the owner, then the answer succeeds and `answered_by` names them."""

    rig = await answer_world()
    gap = await rig.cancel_gap()

    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert after.clarification is not None and after.clarification.answered_by == MEMBER
    (event,) = [e for e in await rig.world.records.events(rig.project_id, gap.gap_id) if e.event == "answered"]
    assert event.actor == MEMBER
    # The route side (any member, not only the owner; a non-member is refused): tests/test_api_gap_routes.py.
