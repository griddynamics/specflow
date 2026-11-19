"""The review list and ``revert_clarification`` of ``GapService`` (docs/graph/gap/answers.md "Review list", "Revert";
lifecycle.md "Gap transitions", "Clarification transitions"; FR-GANS-10, FR-GANS-13)."""

from __future__ import annotations

import pytest

from gap_acceptance_support import Planted
from gap_answer_support import MEMBER, answer_world, service_of
from gap_close_support import apply_build, update_run
from graph.core.errors import ConfigurationError, GraphWriteError
from graph.gap.errors import AnswerInvalidError, GapNotFoundError, GapStateError
from graph.gap.models import AnswerInput
from graph.gap.prompts import RELEVANCE_PROMPT


async def ai_rig():
    """The corpus after a full run with AI answers on: the CSV export gap is answered by AI (pending)."""

    rig = await answer_world(ai_answers=True)
    (listed,) = (await rig.service.ai_answers(rig.project_id)).answers
    return rig, listed


async def statuses(rig, clr: str) -> set[str]:
    return {row.status for row in await rig.world.amendments.for_clarification(rig.project_id, clr)}


async def test_the_review_list_holds_ai_answers_newest_first_within_its_limit() -> None:
    rig, listed = await ai_rig()
    person = await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=person.gap_id, option_rank=1, answered_by=MEMBER))

    answers = (await rig.service.ai_answers(rig.project_id)).answers

    assert [item.gap_id for item in answers] == [listed.gap_id]  # a person's answer is not listed
    assert listed.gap_status == "answered" and listed.title and listed.question.endswith("?")
    assert listed.follow_up is None and listed.clarification.mode == "ai"
    with pytest.raises(AnswerInvalidError) as refused:
        await rig.service.ai_answers(rig.project_id, limit=0)
    assert refused.value.field == "limit"


async def test_a_pending_ai_answer_reverted_reopens_its_gap_for_a_person() -> None:
    rig, listed = await ai_rig()
    clr = listed.clarification.clr_id

    result = await rig.service.revert_clarification(rig.project_id, clr, by=MEMBER, reason="  Ask the owner.  ")

    assert (result.gap_id, result.follow_up, result.build) == (listed.gap_id, False, None)
    assert rig.builds.requests == []  # the text never reached the corpus
    gap = await rig.service.get_gap(rig.project_id, listed.gap_id)
    assert (gap.status, gap.route, gap.clarification) == ("open", "ask", None)
    assert len(gap.options) == 2 and await statuses(rig, clr) == {"reverted"}
    (reverted,) = (await rig.service.ai_answers(rig.project_id)).answers
    assert reverted.clarification.status == "reverted" and reverted.follow_up is None
    (event,) = [item for item in await rig.world.records.events(rig.project_id, gap.gap_id) if item.event == "reverted"]
    assert event.detail["reason"] == "Ask the owner." and event.detail["answered_by_ai"] is True
    with pytest.raises(GapStateError) as again:
        await rig.service.revert_clarification(rig.project_id, clr, by=MEMBER, reason="Twice.")
    assert again.value.status == "reverted"


async def test_an_applying_answer_reverted_requests_a_build_without_its_text() -> None:
    rig, listed = await ai_rig()
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    clr = listed.clarification.clr_id
    assert await statuses(rig, clr) == {"active"}

    result = await rig.service.revert_clarification(rig.project_id, clr, by=MEMBER, reason="Wrong.")

    assert not result.follow_up and result.build == "build-2"
    assert rig.builds.requests[-1] == (rig.project_id, "gap_revert", False)
    assert (await rig.service.get_gap(rig.project_id, listed.gap_id)).status == "open"
    assert await statuses(rig, clr) == {"reverted"}


async def test_a_revert_needs_a_known_revertible_clarification_and_a_reason() -> None:
    rig, listed = await ai_rig()

    with pytest.raises(GapNotFoundError) as unknown:
        await rig.service.revert_clarification(rig.project_id, "CLR::000000000000", by=MEMBER, reason="No.")
    assert unknown.value.clr_id == "CLR::000000000000"
    with pytest.raises(AnswerInvalidError) as empty:
        await rig.service.revert_clarification(rig.project_id, listed.clarification.clr_id, by=MEMBER, reason=" ")
    assert (empty.value.field, empty.value.reason) == ("reason", "length")
    person = await rig.cancel_gap()
    first = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=person.gap_id, option_rank=1, answered_by=MEMBER)
    )
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    for key, props in rig.world.executor.nodes.items():  # as the close check leaves it: resolved, applied
        if key[0] == "Gap" and props["gap_id"] == person.gap_id:
            props.update(status="resolved", resolution="answered")
        if key[0] == "Clarification" and props["clr_id"] == first.clr_id:
            props["status"] = "applied"
    again = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=person.gap_id, option_rank=2, answered_by=MEMBER)
    )
    with pytest.raises(GapStateError):  # a pending re-answer of a resolved gap is replaced, not reverted
        await rig.service.revert_clarification(rig.project_id, again.clr_id, by=MEMBER, reason="No.")


async def test_a_revert_that_needs_a_build_refuses_without_a_build_port_and_a_failed_write_sets_rows_back() -> None:
    rig, listed = await ai_rig()
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    clr = listed.clarification.clr_id
    portless, _, _, _ = service_of(rig.world, builds=None)

    with pytest.raises(ConfigurationError):
        await portless.revert_clarification(rig.project_id, clr, by=MEMBER, reason="No.")
    rig.world.executor.fail_on = "upsert_node_Clarification"
    with pytest.raises(GraphWriteError):
        await rig.service.revert_clarification(rig.project_id, clr, by=MEMBER, reason="No.")

    assert await statuses(rig, clr) == {"active"}
    gap = await rig.service.get_gap(rig.project_id, listed.gap_id)
    assert gap.status == "applying" and {node.spec_id for node in gap.about} == {Planted.OWNERLESS_CSV_EXPORT}


async def test_a_persons_answer_takes_the_ai_route_off_the_gap() -> None:
    rig, listed = await ai_rig()

    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=listed.gap_id, option_rank=2, answered_by=MEMBER))

    gap = await rig.service.get_gap(rig.project_id, listed.gap_id)
    assert (gap.route, gap.clarification.mode if gap.clarification else None) == ("ask", "option")


def resolve(rig, gap_id: str, clr: str) -> None:
    """As the close check leaves a fixed gap: resolved, its clarification applied."""

    for key, props in rig.world.executor.nodes.items():
        if key[0] == "Gap" and props["gap_id"] == gap_id:
            props.update(status="resolved", resolution="answered")
        if key[0] == "Clarification" and props["clr_id"] == clr:
            props["status"] = "applied"


async def test_after_a_revert_the_follow_up_is_answered_not_the_old_gap() -> None:
    rig, listed = await ai_rig()
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    resolve(rig, listed.gap_id, listed.clarification.clr_id)

    result = await rig.service.revert_clarification(rig.project_id, listed.clarification.clr_id, by=MEMBER, reason="A")

    with pytest.raises(GapStateError):  # its only applied answer is reverted: the question lives in the follow-up
        await rig.service.answer_gap(
            rig.project_id, AnswerInput(gap_id=listed.gap_id, option_rank=1, answered_by=MEMBER)
        )
    answered = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=result.gap_id, option_rank=1, answered_by=MEMBER)
    )
    assert answered.gap_id == result.gap_id
    old, new = rig.world.gap_nodes()[listed.gap_id], rig.world.gap_nodes()[result.gap_id]
    assert new["signature"] != old["signature"] and new["route"] == "ask"


async def test_a_reopened_gap_with_a_reverted_answer_is_never_obsolete() -> None:
    rig, listed = await ai_rig()
    spec = {"sections": [{"section": "S1", "relevance": "spec", "signal": "states behaviour"}]}
    rig.world.gateway.handle(RELEVANCE_PROMPT, lambda _request: spec)
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    await rig.service.revert_clarification(rig.project_id, listed.clarification.clr_id, by=MEMBER, reason="No.")
    criteria = {Planted.OWNERLESS_CSV_EXPORT: {"acceptance_criteria": str(listed.clarification.text)}}

    # The build already in flight read the answer: the detector is silent on the gap, which still waits for a person.
    await update_run(rig.world, await apply_build(rig.world, attributes=criteria))

    gap = await rig.service.get_gap(rig.project_id, listed.gap_id)
    assert (gap.status, gap.route) == ("open", "ask")
