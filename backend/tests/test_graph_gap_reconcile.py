"""Unit tests of ``graph.gap.reconcile`` (docs/graph/gap/reconcile.md; FR-GREC-01..08): the four questions on the
shop corpus's re-uploads, the edge cases, the judge cache and ``reconcile_corpus``'s writes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import REUPLOADS, Planted, RecordedGapGateway, assert_all_recorded, corpus_text
from gap_port_fakes import MemoryAmendments, MemoryGapRecords
from graph.core.errors import LLMError
from graph.gap.corpus import apply_amendments, effective_revision_id
from graph.gap.models import AmendmentRow
from graph.gap.reconcile import (
    AnswerDecision,
    LineageText,
    ReconcileInputs,
    SameMeaningAnswer,
    Section,
    content_words,
    gateway_judge,
    judge_messages,
    negated,
    plain,
    reconcile_corpus,
    reconcile_document,
    same_kind,
    sections,
    similarity,
)

AT = datetime(2026, 3, 1, tzinfo=UTC)
LINEAGE = "upload:orders-spec"
PROJECT = "11111111-1111-4111-8111-111111111111"
QUESTION = "Can a customer cancel a paid order?"
ANSWER = "A customer can cancel a paid order within 30 minutes of payment. The full amount is refunded."
MARKER = "> Clarified by GAP::7C2A91E04B3D"
STATED = AnswerDecision(decision="a paid order may be cancelled", in_section="stated")
ORDERS = corpus_text("orders-spec.md")


def row(**values: Any) -> AmendmentRow:
    defaults: dict[str, Any] = {
        "amendment_id": "AMD::1",
        "clr_id": "CLR::1",
        "lineage": LINEAGE,
        "heading_path": ("Orders service specification", "Place order"),
        "anchor_text": QUESTION,
        "anchor_hash": "0" * 64,
        "operation": "replace",
        "text": f"{ANSWER}\n\n{MARKER}",
        "status": "active",
        "last_revision_id": "REV::1",
        "created_at": AT,
        "updated_at": AT,
    }
    return AmendmentRow(**(defaults | values))


def upload(name: str) -> str:
    return next(case for case in REUPLOADS if case.name == name).markdown


def revision(text: str, revision_id: str = "REV::2") -> LineageText:
    return LineageText(LINEAGE, revision_id, text)


class Judge:
    """A scripted same-meaning judge: ``verdicts`` by a word of the section (None: the call fails)."""

    def __init__(self, verdicts: dict[str, str | None], confidence: float = 0.9) -> None:
        self.verdicts = verdicts
        self.confidence = confidence
        self.seen: list[str] = []

    async def __call__(self, amendment: AmendmentRow, section: Section) -> SameMeaningAnswer | None:
        self.seen.append(amendment.amendment_id)
        for word, verdict in self.verdicts.items():
            if word in section.text:
                if verdict is None:
                    return None
                return SameMeaningAnswer(decisions=[STATED], verdict=verdict, confidence=self.confidence)
        return SameMeaningAnswer(verdict="unrelated", confidence=0.9)


PREVIOUS = apply_amendments(ORDERS, [row()]).markdown
JUDGE = {"half an hour": "same", "until the order ships": "different"}


async def reconcile(
    text: str, rows: list[AmendmentRow] | None = None, judge: Any = None, previous: str | None = PREVIOUS
):
    return await reconcile_document(
        PROJECT, revision(text), rows or [row()], previous_text=previous, judge=judge or Judge(JUDGE), now=AT
    )


def test_normalisation_drops_markers_lists_and_emphasis() -> None:
    assert plain(f"- **Paid** orders{chr(0x2019)} *refund*\n{MARKER}") == "paid orders' refund"
    assert plain("> quoted line") == "quoted line"
    assert content_words("Orders are cancelled") == {"order", "cancel"}


def test_similarity_tells_a_reworded_question_from_a_new_rule() -> None:
    reworded = "Can an order that is already paid be cancelled?"
    assert similarity(QUESTION, reworded) >= 0.7
    for other in (
        "Paid orders can be cancelled until the order ships. A 5% fee is kept.",
        "Paid orders may be cancelled for half an hour after payment; the customer gets a full refund.",
        "Payment is captured when the order is placed.",
    ):
        assert similarity(QUESTION, other) < 0.7
    assert similarity("", QUESTION) == 0.0


def test_sections_keep_heading_paths_and_skip_fenced_headings() -> None:
    found = sections("# T\nintro\n## A\none\n```\n# not a heading\n```\n### B\ntwo\n")
    assert [section.path for section in found] == [("t",), ("t", "a"), ("t", "a", "b")]
    assert found[1].lines == ("one", "```", "# not a heading", "```")


@pytest.mark.parametrize(
    ("name", "outcome", "status"),
    [
        ("same-file", "applied", "active"),
        ("reworded-line", "reanchored", "active"),
        ("pasted-answer", "absorbed", "retired"),
        ("own-words", "absorbed", "retired"),
        ("changed-rule", "conflict", "suspended"),
        ("section-deleted", "orphaned", "orphaned"),
    ],
)
async def test_each_reupload_gets_its_outcome(name: str, outcome: str, status: str) -> None:
    result = await reconcile(upload(name))

    (item,) = result.amendments
    (changed,) = result.changed
    assert (item.outcome, changed.status) == (outcome, status)
    assert (MARKER in result.text) == (outcome in {"applied", "reanchored"})
    assert changed.last_revision_id == "REV::2"
    assert result.follow_ups == (("CLR::1",) if status in {"suspended", "orphaned"} else ())


async def test_a_reworded_line_moves_the_anchor_and_a_conflict_anchors_on_the_owners_line() -> None:
    reworded = await reconcile(upload("reworded-line"))
    conflict = await reconcile(upload("changed-rule"))

    assert reworded.changed[0].anchor_text == "Can an order that is already paid be cancelled?"
    assert reworded.amendments[0].anchor is not None
    assert conflict.changed[0].anchor_text == "Paid orders can be cancelled until the order ships. A 5% fee is kept."
    assert conflict.amendments[0].verdict == "different"


async def test_a_low_confidence_same_meaning_counts_as_different() -> None:
    result = await reconcile(upload("own-words"), judge=Judge(JUDGE, confidence=0.4))

    assert result.amendments[0].outcome == "conflict"


async def test_partial_adoption_is_a_conflict_the_judge_sees() -> None:
    text = ORDERS.replace(QUESTION, "A customer can cancel a paid order within 30 minutes of payment.")
    judge = Judge({"within 30 minutes": "different"})

    result = await reconcile(text, judge=judge)

    assert result.amendments[0].outcome == "conflict" and judge.seen == ["AMD::1"]


async def test_a_partial_adoption_recorded_as_same_is_different_by_its_missing_decision() -> None:
    """Extra item of session 10: a real model judged an owner who copied half of the answer as ``same`` in two of
    three runs. The judge lists the answer's decisions first; code reads ``same`` with a missing decision as
    ``different`` (the recorded answer keeps the model's ``same``)."""

    text = ORDERS.replace(QUESTION, "A customer can cancel a paid order within 30 minutes of payment.")
    gateway = RecordedGapGateway()

    result = await reconcile(text, judge=gateway_judge(gateway, None, PROJECT, lambda: AT))

    (outcome,) = result.amendments
    assert (outcome.outcome, outcome.verdict) == ("conflict", "different")
    assert len(gateway.calls_of("gap_judge")) == 1
    assert_all_recorded(gateway)


def test_a_same_verdict_holds_only_when_every_listed_decision_is_stated() -> None:
    stated = AnswerDecision(decision="cancel within 30 minutes", in_section="stated")
    missing = AnswerDecision(decision="full refund", in_section="missing")
    contradicted = AnswerDecision(decision="full refund", in_section="contradicted")

    assert SameMeaningAnswer(decisions=[stated], verdict="same").decided() == "same"
    assert SameMeaningAnswer(verdict="same").decided() == "different"  # a verdict without its list
    assert SameMeaningAnswer(decisions=[stated, missing], verdict="same").decided() == "different"
    assert SameMeaningAnswer(decisions=[contradicted], verdict="same").decided() == "different"
    assert SameMeaningAnswer(decisions=[missing], verdict="unrelated").decided() == "unrelated"


async def test_an_unrelated_section_orphans_the_answer() -> None:
    text = ORDERS.replace(QUESTION, "Orders are shown in the colour of the brand.")

    result = await reconcile(text, judge=Judge({}))

    assert result.amendments[0].outcome == "orphaned"


async def test_a_renamed_section_is_found_by_its_text() -> None:
    renamed = await reconcile(upload("same-file").replace("## Place order", "## Placing an order"))
    changed = await reconcile(upload("changed-rule").replace("## Place order", "## Placing an order"))

    assert renamed.amendments[0].outcome == "applied"
    assert changed.amendments[0].outcome == "conflict"
    assert changed.changed[0].heading_path[-1] == "Placing an order"


async def test_an_answer_pasted_under_another_heading_is_absorbed() -> None:
    text = upload("section-deleted") + f"\n## Cancelling\n{ANSWER}\n"

    result = await reconcile(text)

    assert result.amendments[0].outcome == "absorbed"


async def test_a_failed_judge_leaves_the_answer_undecided_and_applied_then_a_conflict() -> None:
    failing = Judge({"until the order ships": None})

    first = await reconcile(upload("changed-rule"), judge=failing)
    (stored,) = first.changed
    second = await reconcile(upload("changed-rule"), rows=[stored], judge=failing)

    assert first.amendments[0].outcome == "undecided" and first.amendments[0].verdict == "failed"
    assert stored.status == "undecided" and stored.last_revision_id == "REV::1"  # the next build asks again
    assert ANSWER in first.text and "until the order ships" in first.text  # the owner's line is kept
    assert second.amendments[0].outcome == "conflict" and second.changed[0].status == "suspended"


async def test_without_a_judge_nothing_is_stored_and_the_answer_stays_applied() -> None:
    result = await reconcile_document(PROJECT, revision(upload("own-words")), [row()], previous_text=PREVIOUS)

    assert result.amendments[0].outcome == "undecided" and result.changed == ()
    assert ANSWER in result.text


async def test_several_amendments_of_one_section_are_reconciled_one_by_one() -> None:
    second = row(
        amendment_id="AMD::2",
        clr_id="CLR::2",
        operation="insert_after",
        anchor_text="Payment is captured when the order is placed.",
        text=f"Payment is captured by Stripe.\n\n{MARKER}",
    )
    previous = apply_amendments(ORDERS, [row(), second]).markdown

    result = await reconcile(upload("pasted-answer"), rows=[row(), second], previous=previous)

    assert {item.amendment_id: item.outcome for item in result.amendments} == {
        "AMD::1": "absorbed",
        "AMD::2": "applied",
    }
    assert "Payment is captured by Stripe." in result.text


async def test_a_replace_and_an_insert_on_one_anchor_keep_their_order() -> None:
    insert = row(amendment_id="AMD::2", operation="insert_after", text=f"Support is told.\n\n{MARKER}")
    previous = apply_amendments(ORDERS, [row(), insert]).markdown

    result = await reconcile(upload("same-file"), rows=[insert, row()], previous=previous)

    assert [item.amendment_id for item in result.amendments] == ["AMD::1", "AMD::2"]
    assert result.text.index(ANSWER) < result.text.index("Support is told.")


async def test_the_old_section_falls_back_to_the_rows_heading_path() -> None:
    result = await reconcile(upload("reworded-line"), previous=None)

    assert result.amendments[0].outcome == "reanchored"


async def test_the_gateway_judge_is_cached_by_input_and_a_failure_is_not() -> None:
    records = MemoryGapRecords()

    class Gateway:
        calls = 0
        fail = False

        async def structured(self, *_args: Any, **_kwargs: Any) -> SameMeaningAnswer:
            Gateway.calls += 1
            if Gateway.fail:
                raise LLMError("exhausted", prompt="gap.judge")
            return SameMeaningAnswer(decisions=[STATED], verdict="same", confidence=0.95)

    section = sections(upload("own-words"))[2]
    judge = gateway_judge(Gateway(), records, PROJECT, lambda: AT)

    first, second = await judge(row(), section), await judge(row(), section)
    Gateway.fail = True
    failed = await judge(row(text="Other answer."), section)

    assert first == second and Gateway.calls == 2 and failed is None
    messages = judge_messages(row(), section)
    assert MARKER not in messages[1]["content"] and "replaced line" in messages[1]["content"]


async def test_reconcile_corpus_stores_outcomes_and_events() -> None:
    amendments, records = MemoryAmendments(), MemoryGapRecords()
    await amendments.put(PROJECT, [row(), row(amendment_id="AMD::9", clr_id="CLR::9", lineage="upload:gone")])

    async def gap_ids() -> dict[str, str]:
        return {"CLR::1": "GAP::A", "CLR::9": "GAP::B"}

    async def previous(_lineage: str) -> str:
        return PREVIOUS

    inputs = ReconcileInputs(amendments, records, Judge(JUDGE), previous, gap_ids, now=lambda: AT)
    done = await reconcile_corpus(PROJECT, [revision(upload("changed-rule"))], inputs)

    assert {item.amendment_id: item.outcome for item in done.outcomes} == {"AMD::1": "conflict", "AMD::9": "orphaned"}
    assert done.rows == {LINEAGE: ()}
    stored = {item.amendment_id: item.status for item in await amendments.for_clarification(PROJECT, "CLR::1")}
    assert stored == {"AMD::1": "suspended"}
    (event,) = await records.events(PROJECT, "GAP::A")
    assert event.event == "reconciled" and event.detail["outcome"] == "conflict"
    assert event.detail["amendment_id"] == "AMD::1" and event.detail["revision_id"] == "REV::2"
    assert (await records.events(PROJECT, "GAP::B"))[0].detail["outcome"] == "orphaned"


async def test_reconcile_corpus_skips_current_rows_and_a_dry_run_stores_nothing() -> None:
    amendments, records = MemoryAmendments(), MemoryGapRecords()
    current = row(last_revision_id="REV::2")
    await amendments.put(PROJECT, [current])
    inputs = ReconcileInputs(amendments, records)

    same = await reconcile_corpus(PROJECT, [revision(ORDERS)], inputs)
    dry = await reconcile_corpus(PROJECT, [revision(upload("changed-rule"), "REV::3")], inputs, dry_run=True)

    assert same.rows == {LINEAGE: (current,)} and same.outcomes == ()
    assert dry.outcomes[0].outcome == "undecided"
    assert (await amendments.for_clarification(PROJECT, "CLR::1"))[0] == current
    assert await records.events(PROJECT, "GAP::A") == ()


@pytest.mark.parametrize(
    "line",
    [
        "A customer can cancel a paid order.",
        "Customers can not cancel paid orders.",
        "A customer cannot cancel a paid order.",
    ],
)
async def test_an_owner_who_answers_the_question_is_never_re_anchored_over(line: str) -> None:
    judge = Judge({"cancel": "different"})

    result = await reconcile(ORDERS.replace(QUESTION, line), judge=judge)

    assert result.amendments[0].outcome == "conflict" and judge.seen == ["AMD::1"]
    assert line in result.text  # GD3: the owner's rule is what the build reads


def test_a_reworded_anchor_keeps_its_kind_and_polarity() -> None:
    assert same_kind(QUESTION, "Can an order that is already paid be cancelled?")
    assert not same_kind(QUESTION, "A customer can cancel a paid order.")
    assert not same_kind("Orders are cancelled.", "Orders are not cancelled.")
    assert negated("It isn't refunded.") and not negated("It is refunded.")


async def test_an_answer_on_a_lineage_with_answers_is_current_without_an_upload() -> None:
    amendments, records = MemoryAmendments(), MemoryGapRecords()
    other = row(
        amendment_id="AMD::0",
        clr_id="CLR::0",
        operation="insert_after",
        anchor_text=Planted.REVIEW_LIMIT_ORDERS_LINE,
        text=f"Reviews take a day.\n\n{MARKER}",
        last_revision_id="REV::2",
    )
    effective = effective_revision_id("REV::2", [other])
    await amendments.put(PROJECT, [other, row(last_revision_id=effective)])

    done = await reconcile_corpus(PROJECT, [revision(ORDERS)], ReconcileInputs(amendments, records))

    assert done.outcomes == ()
    assert {item.last_revision_id for item in await amendments.for_lineage(PROJECT, LINEAGE)} == {"REV::2"}
    assert await records.events(PROJECT, "GAP::A") == ()


async def test_reconcile_never_writes_back_a_row_an_answer_changed_meanwhile() -> None:
    amendments, records = MemoryAmendments(), MemoryGapRecords()
    await amendments.put(PROJECT, [row()])

    async def previous(_lineage: str) -> str:
        await amendments.set_status(PROJECT, ["AMD::1"], "reverted")  # an apply replaced the answer meanwhile
        return PREVIOUS

    inputs = ReconcileInputs(amendments, records, Judge(JUDGE), previous, now=lambda: AT)
    done = await reconcile_corpus(PROJECT, [revision(upload("changed-rule"))], inputs)

    assert done.outcomes[0].outcome == "conflict"
    assert (await amendments.for_clarification(PROJECT, "CLR::1"))[0].status == "reverted"
