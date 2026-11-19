"""The close step on answers reconcile changed, and answers to their follow-ups (docs/graph/gap/reconcile.md "Conflict
follow-up", "Orphaned follow-up", "Edge cases"; lifecycle.md "Clarification transitions", "Close check")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded
from gap_answer_support import MEMBER, ORDERS_LINEAGE, PLACE, amend, answer_world, service_of
from gap_close_support import CANCEL_REQUIREMENT, PLACE_HEADING, answer_and_apply, apply_build, update_run
from gap_reconcile_support import WORKED_ANSWER, Reconciled, answer_follow_up, reupload, upload_of
from graph.gap.errors import AnswerInvalidError
from graph.gap.follow_ups import predecessors
from graph.gap.gaps import StoredGap, read_gaps
from graph.gap.models import AnswerInput, FollowUp
from graph.gap.reconcile import gateway_judge

CHANGED_RULE = "Paid orders can be cancelled until the order ships. A 5% fee is kept."


async def conflict() -> Reconciled:
    return await reupload("changed-rule", keep={CANCEL_REQUIREMENT: (ORDERS_LINEAGE, PLACE_HEADING, CHANGED_RULE)})


async def statuses(done: Reconciled) -> list[str]:
    return await done.amendment_statuses()


async def test_keeping_the_upload_resolves_the_follow_up_at_once_as_a_pointer() -> None:
    done = await conflict()
    follow_up = await done.follow_up()
    builds = len(done.rig.builds.requests)

    result = await answer_follow_up(done, follow_up, option_rank=1)

    after = await done.rig.service.get_gap(done.rig.project_id, follow_up.gap_id)
    assert (after.status, after.resolution) == ("resolved", "pointer")
    assert result.immediate and result.placement == "none" and len(done.rig.builds.requests) == builds
    assert (await done.clarification(result.clr_id))["mode"] == "pointer"
    assert (await done.clarification())["status"] == "superseded"
    assert await statuses(done) == ["reverted"]
    grounded = done.rig.world.executor.edge_keys("GROUNDED_IN")
    assert any(key.startswith(f"{result.clr_id}|GROUNDED_IN|") for key in grounded)
    assert f"{result.clr_id}|REPLACES|{done.clr_id}" in done.rig.world.executor.edge_keys("REPLACES")


async def test_restoring_the_earlier_answer_amends_the_owners_line_and_supersedes_the_conflict() -> None:
    done = await conflict()
    follow_up = await done.follow_up()

    result = await answer_follow_up(done, follow_up, option_rank=2)
    await done.rig.service.apply_now(done.rig.project_id, by=MEMBER)

    (row,) = await done.rig.world.amendments.for_clarification(done.rig.project_id, result.clr_id)
    assert (row.operation, row.anchor_text, row.status) == ("replace", CHANGED_RULE, "active")
    assert (await done.clarification())["status"] == "superseded"
    assert await statuses(done) == ["reverted"]
    rebuilt = await apply_build(done.rig.world, build_id="build-4", uploads={ORDERS_LINEAGE: upload_of("changed-rule")})
    orders = next(item.markdown for item in rebuilt.revisions if item.lineage_id == ORDERS_LINEAGE)
    assert WORKED_ANSWER in orders and CHANGED_RULE not in orders


async def test_dropping_an_orphaned_answer_resolves_its_follow_up() -> None:
    done = await reupload("section-deleted", remove=[CANCEL_REQUIREMENT])
    follow_up = await done.follow_up()
    drop = next(option.rank for option in follow_up.options if option.label.startswith("Drop it"))

    await answer_follow_up(done, follow_up, option_rank=drop)

    after = await done.rig.service.get_gap(done.rig.project_id, follow_up.gap_id)
    assert (after.status, after.resolution) == ("resolved", "answered")
    assert (await done.clarification())["status"] == "superseded"
    assert await statuses(done) == ["reverted"]


async def test_moving_an_orphaned_answer_writes_a_clarifications_document_answer() -> None:
    done = await reupload("section-deleted", remove=[CANCEL_REQUIREMENT])
    follow_up = await done.follow_up()

    result = await answer_follow_up(done, follow_up, option_rank=1)
    await done.rig.service.apply_now(done.rig.project_id, by=MEMBER)

    assert result.placement == "document"
    assert (await done.clarification())["status"] == "superseded"
    assert await statuses(done) == ["reverted"]


async def test_dismissing_a_conflict_follow_up_reverts_the_suspended_amendment() -> None:
    done = await conflict()
    follow_up = await done.follow_up()

    await done.rig.service.dismiss_gap(done.rig.project_id, follow_up.gap_id, reason="The upload is right.", by=MEMBER)

    assert await statuses(done) == ["reverted"]
    assert (await done.clarification())["status"] == "conflict"  # kept as history


async def test_a_reconcile_follow_up_is_never_obsolete_and_opens_once() -> None:
    done = await reupload("section-deleted", remove=[CANCEL_REQUIREMENT])
    follow_up = await done.follow_up()

    again = await update_run(done.rig.world, done.rebuilt)

    assert again.counts["close"]["obsolete"] == 0
    still = await done.rig.service.get_gap(done.rig.project_id, follow_up.gap_id)
    assert still.status == "open"  # the question stays for a person
    gaps = await read_gaps(done.rig.world.executor, done.rig.project_id, done.rig.world.ontology)
    assert sum(1 for gap in gaps if any(item.reason == "orphaned" for item in gap.follow_ups)) == 2  # old and new


async def test_an_absorbed_answer_is_grounded_in_the_owners_fragment() -> None:
    done = await reupload("pasted-answer", keep={CANCEL_REQUIREMENT: (ORDERS_LINEAGE, PLACE_HEADING, WORKED_ANSWER)})

    fragment = done.rebuilt.fragment(ORDERS_LINEAGE, PLACE_HEADING)
    assert f"{done.clr_id}|GROUNDED_IN|{fragment}" in done.rig.world.executor.edge_keys("GROUNDED_IN")
    assert done.run.counts["close"]["reconciled"] == {"absorbed": 1}


async def test_a_reanchored_answer_is_rechecked_and_stays_resolved() -> None:
    done = await reupload("reworded-line")

    gap = await done.rig.service.get_gap(done.rig.project_id, done.gap.gap_id)
    assert (gap.status, gap.resolution) == ("resolved", "answered")
    assert [event.event for event in await done.events("rechecked")] == ["rechecked"]
    assert done.run.counts["close"]["reconciled"] == {"rechecked": 1}


async def test_the_follow_up_of_a_never_grounded_answer_replaces_it_when_applied() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    first = await answer_and_apply(rig, gap, option_rank=1)
    world = rig.world
    rows = await world.amendments.for_clarification(rig.project_id, first.clr_id)
    ids = [row.amendment_id for row in rows]
    await world.amendments.set_status(rig.project_id, ids, "pending")  # the builds never pick the text up
    await update_run(world, await apply_build(world))
    await update_run(world, await apply_build(world, build_id="build-3"))
    await world.amendments.set_status(rig.project_id, ids, "active")  # the text is still in the corpus
    rig.service = service_of(world, builds=rig.builds, holds=rig.holds, clock=rig.clock)[0]
    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    (new,) = [item.gap_id for item in after.history.follow_ups if item.direction == "continued_by"]

    await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=new, own_text="Support decides case by case.", answered_by=MEMBER)
    )
    await rig.service.apply_now(rig.project_id, by=MEMBER)

    old = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert old.clarification is None or old.clarification.status != "pending"
    stored = await read_gaps(world.executor, rig.project_id, world.ontology, gap_ids=[gap.gap_id])
    assert [props["status"] for props in stored[0].clarifications] == ["superseded"]
    assert {row.status for row in await world.amendments.for_clarification(rig.project_id, first.clr_id)} == {
        "reverted"
    }
    events = await world.records.events(rig.project_id, gap.gap_id)
    assert any(item.event == "superseded" and item.detail.get("reason") == "not_fixed" for item in events)


async def test_an_option_without_edits_outside_a_reconcile_follow_up_is_refused() -> None:
    done = await reupload("same-file")
    gaps = await read_gaps(done.rig.world.executor, done.rig.project_id, done.rig.world.ontology, statuses=("open",))
    gap = gaps[0]
    options = await done.rig.world.records.options(done.rig.project_id, gap.gap_id)
    await done.rig.world.records.put_options(
        done.rig.project_id, gap.gap_id, [options[0].model_copy(update={"edits": ()}), *options[1:]]
    )

    with pytest.raises(AnswerInvalidError):
        await done.rig.service.answer_gap(
            done.rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER)
        )


def test_predecessors_name_the_clarifications_an_answer_replaces() -> None:
    def gap(gap_id: str, follow_ups: Any = (), clarifications: Any = ()) -> StoredGap:
        return StoredGap(gap_id, {"gap_id": gap_id}, follow_ups=follow_ups, clarifications=clarifications)

    old = gap(
        "GAP::A",
        clarifications=(
            {"clr_id": "CLR::1", "status": "pending", "apply_requested_at": "t"},
            {"clr_id": "CLR::2", "status": "pending"},
            {"clr_id": "CLR::3", "status": "applied"},
        ),
    )
    new = gap("GAP::B", follow_ups=(FollowUp(gap_id="GAP::A", reason="not_fixed", direction="continues"),))

    assert [item.clr_id for item in predecessors(new, {"GAP::A": old})] == ["CLR::1"]
    assert predecessors(new, {}) == ()


async def test_an_upload_in_the_build_that_applies_an_answer_opens_the_conflict_at_once() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    edit = amend(PLACE, Planted.STATED_QUESTION_LINE, WORKED_ANSWER)
    result = await answer_and_apply(rig, gap, own_text=WORKED_ANSWER, edits=[edit])
    gateway = RecordedGapGateway()
    judge = gateway_judge(gateway, rig.world.records, rig.project_id, lambda: datetime.now(UTC))

    rebuilt = await apply_build(rig.world, uploads={ORDERS_LINEAGE: upload_of("changed-rule")}, judge=judge)
    run = await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "follow_up")
    assert run.counts["close"]["reconciled"] == {"conflict": 1}
    (link,) = [item for item in after.history.follow_ups if item.direction == "continued_by"]
    assert link.reason == "conflict"
    stored = await read_gaps(rig.world.executor, rig.project_id, rig.world.ontology, gap_ids=[gap.gap_id])
    assert [props["status"] for props in stored[0].clarifications] == ["conflict"]
    assert result.clr_id == stored[0].clarifications[0]["clr_id"]
    assert_all_recorded(gateway)
