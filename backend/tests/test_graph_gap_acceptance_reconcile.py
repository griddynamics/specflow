"""Acceptance tests of graph.gap: Reconcile (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. The answer is the worked
example of reconcile.md (a ``replace`` of "Can a customer cancel a paid order?"), applied and closed; each re-upload of
``orders-spec.md`` is then built with reconcile in the corpus read (``gap_reconcile_support``) and the gap run follows.
The same-meaning judge answers from ``recordings/gap_judge.json``.
"""

from __future__ import annotations

from gap_acceptance_support import REUPLOADS, Planted, RecordedGapGateway, assert_all_recorded
from gap_answer_support import ORDERS_LINEAGE
from gap_close_support import CANCEL_REQUIREMENT, PLACE_HEADING
from gap_reconcile_support import WORKED_ANSWER, answer_follow_up, reupload, upload_of

OWN_WORDS = "Paid orders may be cancelled for half an hour after payment; the customer gets a full refund."
CHANGED_RULE = "Paid orders can be cancelled until the order ships. A 5% fee is kept."
MARKER_START = "> Clarified by "


async def test_fr_grec_01_new_revision_lineage_active_amendments_arrives_corpus() -> None:
    """Given a re-upload of `orders-spec.md`, then reconcile runs before the build and every amendment has an
    outcome.
    """

    done = await reupload("reworded-line")

    (event,) = await done.events()
    assert event.detail["outcome"] == "reanchored" and event.detail["revision_id"] == "REV::UPLOADED"
    assert WORKED_ANSWER in done.orders()  # the build read the reconciled text
    assert await done.amendment_statuses() == ["active"]


async def test_fr_grec_02_amendment_whose_anchor_found_exactly_applied_again() -> None:
    """Given the original file synced again, then the outcome is `applied` and the build reports no change in that
    section.
    """

    done = await reupload("same-file")

    (event,) = await done.events()
    assert event.detail["outcome"] == "applied"
    assert ORDERS_LINEAGE not in done.rebuilt.report.fragments_changed
    assert (await done.clarification())["status"] == "applied"


async def test_fr_grec_03_amendment_whose_anchor_found_small_edits_under() -> None:
    """Given the question line reworded, then the outcome is `reanchored` and the answer is applied."""

    done = await reupload("reworded-line")

    (event,) = await done.events()
    assert event.detail["outcome"] == "reanchored"
    assert event.detail["new_anchor"] == "Can an order that is already paid be cancelled?"
    assert WORKED_ANSWER in done.orders() and "already paid" not in done.orders()
    assert ORDERS_LINEAGE not in done.rebuilt.report.fragments_changed  # the build reads the same effective text


async def test_fr_grec_04_new_revision_contains_answer_text_judge_call() -> None:
    """Given the owner pasted the answer, and separately rewrote it in their own words, then both outcomes are
    `absorbed` and the build reads the owner's text.
    """

    pasted = await reupload("pasted-answer", keep={CANCEL_REQUIREMENT: (ORDERS_LINEAGE, PLACE_HEADING, WORKED_ANSWER)})
    gateway = RecordedGapGateway()
    own = await reupload(
        "own-words", gateway=gateway, keep={CANCEL_REQUIREMENT: (ORDERS_LINEAGE, PLACE_HEADING, OWN_WORDS)}
    )

    for done, line in ((pasted, WORKED_ANSWER), (own, OWN_WORDS)):
        (event,) = await done.events()
        assert event.detail["outcome"] == "absorbed"
        assert done.orders() == upload_of("pasted-answer" if done is pasted else "own-words")
        assert line in done.orders() and MARKER_START not in done.orders()
        assert await done.amendment_statuses() == ["retired"]
        assert (await done.clarification())["status"] == "absorbed"
    assert (await own.events())[0].detail["verdict"] == "same"
    assert len(gateway.calls_of("gap_judge")) == 1
    assert_all_recorded(gateway)


async def test_fr_grec_05_section_says_something_else_build_read_upload() -> None:
    """Given the owner changed the rule, then the build reads the upload and a follow-up gap `FOLLOWS_UP` the old one
    with the options keep the upload and restore the earlier answer, and takes a new answer as own text.
    """

    gateway = RecordedGapGateway()
    done = await reupload(
        "changed-rule", gateway=gateway, keep={CANCEL_REQUIREMENT: (ORDERS_LINEAGE, PLACE_HEADING, CHANGED_RULE)}
    )

    assert done.orders() == upload_of("changed-rule")  # GD3 = A: the upload wins
    assert await done.amendment_statuses() == ["suspended"]
    assert (await done.clarification())["status"] == "conflict"
    follow_up = await done.follow_up()
    assert follow_up.kind == "contradiction" and follow_up.status == "open" and follow_up.route == "ask"
    assert [(link.gap_id, link.reason) for link in follow_up.history.follow_ups] == [(done.gap.gap_id, "conflict")]
    assert [(option.label, option.recommended) for option in follow_up.options] == [
        ("Keep the upload", True),
        ("Restore the earlier answer", False),
    ]
    restore = follow_up.options[1].edits[0]
    assert (restore.operation, restore.target.anchor_quote, restore.text) == ("replace", CHANGED_RULE, WORKED_ANSWER)
    assert [witness.quote for witness in follow_up.witness] == [CHANGED_RULE]
    assert CANCEL_REQUIREMENT in {node.spec_id for node in follow_up.about}
    # the third choice, a new answer, is the card's own-text answer
    result = await answer_follow_up(done, follow_up, own_text="Paid orders can be cancelled until they ship.")
    assert result.placement == "document"
    assert_all_recorded(gateway)


async def test_fr_grec_06_anchor_section_are_gone_clarification_become_orphaned() -> None:
    """Given the section deleted, then the follow-up gap exists with the move option recommended."""

    done = await reupload("section-deleted", remove=[CANCEL_REQUIREMENT])

    assert (await done.clarification())["status"] == "orphaned"
    follow_up = await done.follow_up()
    assert [(link.gap_id, link.reason) for link in follow_up.history.follow_ups] == [(done.gap.gap_id, "orphaned")]
    recommended = [option for option in follow_up.options if option.recommended]
    assert [option.label for option in recommended] == ["Move it to the clarifications document"]
    assert recommended[0].edits[0].placement == "document" and recommended[0].edits[0].text == WORKED_ANSWER
    assert "Drop it: the section was removed on purpose" in {option.label for option in follow_up.options}
    assert follow_up.kind == done.gap.kind and follow_up.severity == done.gap.severity


async def test_fr_grec_07_reconcile_outcome_written_gap_event_log() -> None:
    """Given each outcome above, then an event names the amendment, the revision and the outcome."""

    seen = {}
    for case in REUPLOADS:
        done = await reupload(case.name)
        (amendment,) = await done.rig.world.amendments.for_clarification(done.rig.project_id, done.clr_id)
        (event,) = await done.events()
        assert event.detail["amendment_id"] == amendment.amendment_id and event.clr_id == done.clr_id
        assert event.detail["revision_id"] == "REV::UPLOADED" and event.detail["lineage"] == ORDERS_LINEAGE
        seen[case.name] = event.detail["outcome"]

    assert seen == {case.name: case.outcome for case in REUPLOADS}


async def test_fr_grec_08_failed_judge_call_leave_amendment_undecided_applied() -> None:
    """Given a failing judge, then the outcome is `undecided` and the build still reads the amended text."""

    gateway = RecordedGapGateway(fail_roles={"gap_judge"})
    done = await reupload("changed-rule", gateway=gateway)

    (event,) = await done.events()
    assert event.detail["outcome"] == "undecided" and event.detail["verdict"] == "failed"
    assert WORKED_ANSWER in done.orders() and CHANGED_RULE in done.orders()
    assert await done.amendment_statuses() == ["undecided"]
    assert (await done.clarification())["status"] == "applied"
    assert Planted.STATED_QUESTION_LINE not in done.orders()
