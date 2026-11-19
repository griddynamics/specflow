"""Acceptance tests of graph.gap: Close check and lifecycle (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import Planted, corpus_text
from gap_answer_support import MEMBER, ORDERS_LINEAGE, answer_world
from gap_close_support import (
    CANCEL_REQUIREMENT,
    PLACE_HEADING,
    apply_build,
    cancel_answered_and_built,
    update_run,
)


async def test_fr_gcls_01_after_build_gap_run_check_applying_gap() -> None:
    """Given the cancel-paid-order answer applied, then the gap is `resolved` and the clarification is `GROUNDED_IN`
    the amended fragment.
    """

    rig, gap, result, rebuilt = await cancel_answered_and_built(1)

    await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert after.status == "resolved" and after.clarification is not None
    assert after.clarification.status == "applied"
    fragment = rebuilt.fragment(ORDERS_LINEAGE, PLACE_HEADING)
    assert f"{result.clr_id}|GROUNDED_IN|{fragment}" in rig.world.executor.edge_keys("GROUNDED_IN")


async def test_fr_gcls_02_gap_still_detected_after_answer_was_applied() -> None:
    """Given option C of the cancel-paid-order gap (a fee with no rule), then a follow-up gap opens."""

    rig, gap, _, rebuilt = await cancel_answered_and_built(3)

    run = await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "follow_up")
    assert after.history is not None
    (follow_up,) = [item for item in after.history.follow_ups if item.direction == "continued_by"]
    assert follow_up.reason == "not_fixed" and run.counts["close"]["follow_ups"] == 1
    opened = await rig.service.get_gap(rig.project_id, follow_up.gap_id)
    assert opened.status == "open" and opened.question == gap.question


async def test_fr_gcls_03_node_open_gap_about_left_graph_detector() -> None:
    """Given the question line deleted by a document edit, then the gap node is gone and the event log says obsolete."""

    rig = await answer_world()
    gap = await rig.cancel_gap()
    upload = corpus_text("orders-spec.md").replace(f"{Planted.STATED_QUESTION_LINE}\n", "")
    rebuilt = await apply_build(rig.world, remove=[Planted.STATED_QUESTION], uploads={ORDERS_LINEAGE: upload})

    await update_run(rig.world, rebuilt)

    assert gap.gap_id not in rig.world.gap_nodes()
    events = await rig.world.records.events(rig.project_id, gap.gap_id)
    assert [event.event for event in events][-1:] == ["obsolete"] or "obsolete" in {e.event for e in events}


async def test_fr_gcls_04_about_impacts_follow_rekeyed_merged_nodes_change() -> None:
    """Given a merge of A into B, then a gap about A is about B after the run."""

    rig = await answer_world()
    gap = await rig.review_gap()
    survivor = "REQ::FR::REVIEW_QUEUE_DECISION"
    rebuilt = await apply_build(rig.world, merge={Planted.REVIEW_LIMIT_ORDERS: survivor})

    await update_run(rig.world, rebuilt)

    about = {node.spec_id for node in (await rig.service.get_gap(rig.project_id, gap.gap_id)).about}
    assert survivor in about and Planted.REVIEW_LIMIT_ORDERS not in about


async def test_fr_gcls_05_resolved_gap_and_clarification_stay_in_graph() -> None:
    """Given a resolved gap, then a walk from the new requirement through its fragment reaches the clarification and
    the gap.
    """

    rig, gap, result, rebuilt = await cancel_answered_and_built(1)
    await update_run(rig.world, rebuilt)

    (node,) = [item for item in await rig.world.reader.nodes(rig.project_id) if item.spec_id == CANCEL_REQUIREMENT]
    fragment = node.provenance[0].fragment_id
    keys = rig.world.executor.edge_keys
    clarifications = {key.split("|")[0] for key in keys("GROUNDED_IN") if key.endswith(f"|{fragment}")}
    gaps = {key.split("|")[-1] for key in keys("ANSWERS") if key.split("|")[0] in clarifications}
    assert clarifications == {result.clr_id} and gaps == {gap.gap_id}
    assert rig.world.gap_nodes()[gap.gap_id]["status"] == "resolved"


async def test_fr_gcls_06_gap_clarification_transition_written_event_log_actor() -> None:
    """Given the full cancel-paid-order flow, then the log lists detected, answered, applying, resolved with actors."""

    rig, gap, _, rebuilt = await cancel_answered_and_built(1)
    await update_run(rig.world, rebuilt)

    logged = {(event.event, event.actor) for event in await rig.world.records.events(rig.project_id, gap.gap_id)}

    assert {("created", "gap-run"), ("answered", MEMBER), ("applying", MEMBER), ("resolved", "gap-run")} <= logged
