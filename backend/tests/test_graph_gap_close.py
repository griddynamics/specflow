"""The close check, the corpus side of answers and dismissals that stay quiet (docs/graph/gap/lifecycle.md "Close
check", "Dismissals and suppression"; reconcile.md "Applying amendments"; answers.md "Clarifications document";
FR-GCOR-01..04, FR-GCLS-01..06, FR-GANS-05..07, FR-GDET-07, FR-GANS-11).

Update builds are simulated (``gap_close_support.apply_build``): the corpus is read through the host's corpus reader
code (``with_answers``) and only changed sections get new fragments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import Planted, corpus_text
from gap_answer_support import MEMBER, ORDERS_LINEAGE, answer_world
from gap_close_support import (
    PLACE_HEADING,
    answer_and_apply,
    apply_build,
    cancel_answered_and_built,
    effective_corpus,
    option_text,
    sections,
    seed_investigator,
    update_run,
)
from graph.gap import run_gap
from graph.gap.answers import marker
from graph.gap.corpus import (
    AppliedText,
    DocumentClarification,
    apply_amendments,
    clarifications_lineage,
    clarifications_markdown,
    effective_revision_id,
    in_document,
)
from graph.gap.gaps import StoredGap, read_gaps
from graph.gap.identity import gap_id
from graph.gap.models import AmendmentRow, AnswerInput
from graph.gap.settings import GapSettings
from graph.gap.steps.close import answer_text, follow_up_signature, still_fires
from graph.gap.steps.detect import Seed, detect
from graph.gap.steps.record import more_seed_signatures, seed_signatures
from graph.gap.steps.relevance import RelevanceOutcome
from graph.gap.steps.sites import Site
from graph.gap.steps.snapshot import read_snapshot

AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
ORDERS = "orders-spec.md"


def _row(amendment_id: str, anchor: str, text: str, operation: str = "replace", **fields: Any) -> AmendmentRow:
    values: dict[str, Any] = {
        "amendment_id": amendment_id,
        "clr_id": "CLR::1",
        "lineage": ORDERS_LINEAGE,
        "heading_path": ("Orders service specification", PLACE_HEADING),
        "anchor_text": anchor,
        "anchor_hash": "0" * 64,
        "operation": operation,
        "text": text,
        "status": "active",
        "created_at": AT,
        "updated_at": AT,
    }
    return AmendmentRow.model_validate(values | fields)


def _orders(revisions: Any) -> Any:
    return next(revision for revision in revisions if revision.lineage_id == ORDERS_LINEAGE)


# Applying amendments (pure)


def test_apply_amendments_replaces_inserts_and_keeps_headings() -> None:
    text = corpus_text(ORDERS)
    rows = [
        _row("AMD::2", Planted.STATED_QUESTION_LINE, "Inserted after.", "insert_after"),
        _row("AMD::1", Planted.STATED_QUESTION_LINE, "Replaced.\n\n> Clarified by GAP::X"),
        _row("AMD::3", "Customers can see their past orders.", "Also kept.", "insert_after"),
        _row("AMD::4", "No such line.", "Lost."),
        _row("AMD::5", Planted.STATED_QUESTION_LINE, "Ignored: pending.", status="pending"),
    ]

    applied = apply_amendments(text, rows)

    assert "Can a customer cancel a paid order?" not in applied.markdown
    lines = applied.markdown.split("\n")
    at = lines.index("Replaced.")
    assert lines[at : at + 5] == ["Replaced.", "", "> Clarified by GAP::X", "", "Inserted after."]
    assert "Customers can see their past orders.\n\nAlso kept." in applied.markdown
    assert [row.amendment_id for row in applied.applied] == ["AMD::1", "AMD::2", "AMD::3"]
    assert [row.amendment_id for row in applied.unanchored] == ["AMD::4"]
    assert [line for line in lines if line.startswith("#")] == [
        line for line in text.split("\n") if line.startswith("#")
    ]


def test_apply_amendments_prefers_the_line_under_its_heading_and_skips_code() -> None:
    text = "# T\n\n## A\nsame line\n\n## B\n```\n## not a heading\n```\nsame line\n"
    row = _row("AMD::1", "same line", "changed", heading_path=("T", "B"))
    second = _row("AMD::2", "same line", "again", heading_path=("T", "B"))

    applied = apply_amendments(text, [row, second])

    assert applied.markdown.split("\n")[3] == "same line" and "changed" in applied.markdown
    assert [item.amendment_id for item in applied.unanchored] == ["AMD::2"]  # a second replace of one line
    assert apply_amendments(text, []) == AppliedText(text)
    assert apply_amendments(text, [_row("AMD::9", "", "x")]).unanchored  # an empty anchor finds nothing


def test_effective_revision_id_changes_only_with_the_applied_amendments() -> None:
    rows = (_row("AMD::1", "a", "x"), _row("AMD::2", "b", "y"))

    assert effective_revision_id("REV::1", ()) == "REV::1"
    assert effective_revision_id("REV::1", rows) == effective_revision_id("REV::1", tuple(reversed(rows)))
    assert effective_revision_id("REV::1", rows) != effective_revision_id("REV::2", rows)
    assert effective_revision_id("REV::1", rows[:1]) != effective_revision_id("REV::1", rows)


def test_clarifications_markdown_orders_sections_and_names_the_gap() -> None:
    first = DocumentClarification("GAP::B", "CLR::B", "Second", "Why b?", "Answer b.", "2026-01-02T00:00:00")
    second = DocumentClarification("GAP::A", "CLR::A", "First\nline", "Why a?", "Answer a.", "2026-01-01T00:00:00")

    text = clarifications_markdown([first, second])

    assert clarifications_markdown([]) == ""
    assert [section.heading_path[-1] for section in sections(text)] == ["First line (GAP::A)", "Second (GAP::B)"]
    assert "Answer a.\n\nAnswers: Why a?" in text
    assert clarifications_lineage("p1") == clarifications_lineage("p1") != clarifications_lineage("p2")
    assert in_document({"placement": "document", "status": "applied"})
    assert in_document({"placement": "document", "status": "pending", "apply_requested_at": "x"})
    assert not in_document({"placement": "document", "status": "pending"})
    assert not in_document({"placement": "amend", "status": "applied"})


# Corpus integration (FR-GCOR)


async def test_corpus_reads_the_amended_text_with_its_marker_and_keeps_the_stored_document() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    stored = corpus_text(ORDERS)
    before = _orders(await effective_corpus(rig.world))

    await answer_and_apply(rig, gap, option_rank=1)
    first, second = _orders(await effective_corpus(rig.world)), _orders(await effective_corpus(rig.world))

    assert before.markdown == stored and before.revision_id == "REV::ORDERS_SPEC::1"
    assert option_text(gap, 1) in first.markdown and Planted.STATED_QUESTION_LINE not in first.markdown
    assert marker(gap.gap_id) in first.markdown.split("\n")
    assert corpus_text(ORDERS) == stored
    assert first.revision_id == second.revision_id != before.revision_id


async def test_a_pending_answer_is_not_read_until_it_is_applied() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()

    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))

    assert _orders(await effective_corpus(rig.world)).markdown == corpus_text(ORDERS)


# The close check (FR-GCLS)


async def test_an_applied_answer_resolves_its_gap_grounded_in_the_amended_fragment() -> None:
    rig, gap, result, rebuilt = await cancel_answered_and_built(1)

    run = await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "answered")
    assert after.clarification is not None and after.clarification.status == "applied"
    assert after.clarification.applied_build_id == "build-2"
    fragment = rebuilt.fragment(ORDERS_LINEAGE, PLACE_HEADING)
    assert rig.world.executor.edge_keys("GROUNDED_IN") == {f"{result.clr_id}|GROUNDED_IN|{fragment}"}
    assert run.counts["close"] == {"resolved": 1, "follow_ups": 0, "waiting": 0, "obsolete": 0, "cites_removed": 1}
    assert not after.witness  # the question line left the corpus; its quote is in the event log
    events = await rig.world.records.events(rig.project_id, gap.gap_id)
    (removed,) = [event for event in events if event.event == "cite_removed"]
    assert removed.detail["quote"] == Planted.STATED_QUESTION_LINE


async def test_an_answer_the_build_did_not_pick_up_waits_one_build_then_follows_up() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await answer_and_apply(rig, gap, option_rank=1)
    world = rig.world
    rows = await world.amendments.for_lineage(rig.project_id, ORDERS_LINEAGE)
    await world.amendments.set_status(rig.project_id, [row.amendment_id for row in rows], "pending")
    unchanged = await apply_build(world)  # the build ran before the answer reached the corpus

    first = await update_run(world, unchanged)
    waiting = await rig.service.get_gap(rig.project_id, gap.gap_id)
    second = await update_run(world, await apply_build(world, build_id="build-3"))

    assert first.counts["close"]["waiting"] == 1 and waiting.status == "applying"
    assert second.counts["close"]["follow_ups"] == 1
    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "follow_up")
    assert after.clarification is not None and after.clarification.status == "pending"


async def test_an_answer_that_does_not_fix_the_gap_opens_an_investigated_follow_up() -> None:
    rig, gap, result, rebuilt = await cancel_answered_and_built(3)

    run = await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "follow_up")
    assert after.clarification is not None and after.clarification.status == "applied"
    assert run.counts["close"]["follow_ups"] == 1
    assert after.history is not None
    (new,) = [item.gap_id for item in after.history.follow_ups if item.direction == "continued_by"]
    follow = await rig.service.get_gap(rig.project_id, new)
    assert (follow.status, follow.origin, follow.question) == ("open", gap.origin, gap.question)
    assert follow.history is not None
    assert [(item.gap_id, item.reason) for item in follow.history.follow_ups] == [(gap.gap_id, "not_fixed")]
    assert {node.spec_id for node in follow.about} >= {Planted.STATED_QUESTION}
    assert len(follow.options) >= 2  # investigated in the same run
    old = StoredGap(gap_id=gap.gap_id, props=rig.world.gap_nodes()[gap.gap_id])
    assert follow.gap_id == gap_id(follow_up_signature(old, "not_fixed"))
    assert rig.world.gap_nodes()[new]["signature"] != gap.signature and result.clr_id


async def test_a_document_answer_is_grounded_in_its_section_and_placed_in_the_document() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    text = "Support decides each cancellation of a paid order."
    result = await answer_and_apply(rig, gap, own_text=text, placement="document")
    lineage = clarifications_lineage(rig.project_id)
    # The build reads the document; its answered-elsewhere check drops the question node.
    rebuilt = await apply_build(rig.world, remove=[Planted.STATED_QUESTION])

    await update_run(rig.world, rebuilt)

    after = await rig.service.get_gap(rig.project_id, gap.gap_id)
    fragment = rebuilt.fragment(lineage, f"{gap.title} ({gap.gap_id})")
    assert f"{result.clr_id}|GROUNDED_IN|{fragment}" in rig.world.executor.edge_keys("GROUNDED_IN")
    assert f"{result.clr_id}|PLACED_IN|{lineage}" in rig.world.executor.edge_keys("PLACED_IN")
    assert (after.status, after.resolution) == ("resolved", "answered")


# The detector re-check


async def test_still_fires_reads_the_detector_and_its_rules() -> None:
    rig = await answer_world()
    world = rig.world
    gap_nodes = await rig.service.list_gaps(rig.project_id)
    stored = {gap.gap_id: gap for gap in await read_gaps(world.executor, rig.project_id, world.ontology)}
    snapshot = await read_snapshot(world.reader, rig.project_id, world.ontology)
    seeds = detect(snapshot, rules=world.rules, relevance=RelevanceOutcome(sections={})).seeds
    cancel = stored[(await rig.cancel_gap()).gap_id]
    raised = StoredGap(gap_id="GAP::R", props={**cancel.props, "origin": "person"}, about=cancel.about)
    unknown = StoredGap(gap_id="GAP::U", props={**cancel.props, "detector": "XX-NONE"}, about=cancel.about)
    quoted = StoredGap(gap_id="GAP::Q", props=cancel.props, cites=cancel.cites)

    assert all(still_fires(stored[view.gap_id], seeds, snapshot, world.rules) for view in gap_nodes.gaps)
    assert still_fires(raised, seeds, snapshot, world.rules) is None
    assert still_fires(unknown, seeds, snapshot, world.rules) is None
    assert still_fires(quoted, seeds, snapshot, world.rules) is True  # by its quoted line alone
    assert still_fires(quoted, (), snapshot, world.rules) is False
    assert answer_text(f"A.\n\n{marker('GAP::X')}", "GAP::X") == "a."


# Dismissals stay quiet (FR-GDET-07, FR-GANS-11)


async def test_a_dismissed_gap_stays_quiet_when_detection_runs_again_on_the_unchanged_corpus() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    seeds = rig.world.gap_nodes()[gap.gap_id]["seed_signatures"]

    await rig.service.dismiss_gap(rig.project_id, gap.gap_id, reason="not a gap", by=MEMBER)
    seed_investigator(rig.world)
    run = await run_gap(rig.world.context(settings=GapSettings(ignore_cache=True)), mode="full")

    suppressions = rig.world.records.suppressions[rig.project_id]
    assert seeds and set(seeds) <= set(suppressions) and gap.signature in suppressions
    assert run.counts["seeds_by_detector"]["OQ-STATED"]["suppressed"] == 1
    page = await rig.service.list_gaps(rig.project_id, about=[Planted.STATED_QUESTION])
    assert page.gaps == ()
    assert run.counts["gaps_new"]["by_kind"].get("open_question", 0) == 0


async def test_a_failed_dismissal_releases_only_the_suppressions_it_added() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    seeds = rig.world.gap_nodes()[gap.gap_id]["seed_signatures"]
    rig.world.executor.fail_on = "delete_node_Gap"

    with pytest.raises(Exception, match="store_write_failed"):
        await rig.service.dismiss_gap(rig.project_id, gap.gap_id, reason="not a gap", by=MEMBER)

    assert not set(rig.world.records.suppressions[rig.project_id]) & {gap.signature, *seeds}


def test_a_recheck_adds_the_signatures_of_new_seeds_of_the_gap_detector() -> None:
    def seed(detector: str, spec_id: str) -> Seed:
        return Seed(detector, "missing", "structure", "strong", (spec_id,), ())

    site = Site("SITE::1", (seed("MS-A", "X"), seed("MS-A", "Y"), seed("MS-B", "X")), ("X", "Y"), (), "h", 1.0, 1)
    known = StoredGap(gap_id="GAP::1", props={"detector": "MS-A", "seed_signatures": [seed("MS-A", "X").signature]})

    assert seed_signatures(site, "MS-A") == sorted({seed("MS-A", "X").signature, seed("MS-A", "Y").signature})
    assert more_seed_signatures(known, site) == seed_signatures(site, "MS-A")
    assert (
        more_seed_signatures(
            known.__class__(gap_id="GAP::1", props={**known.props, "seed_signatures": seed_signatures(site, "MS-A")}),
            site,
        )
        == []
    )


async def test_still_fires_knows_a_gap_by_its_seed_signatures_or_leaves_it_alone() -> None:
    rig = await answer_world()
    world = rig.world
    snapshot = await read_snapshot(world.reader, rig.project_id, world.ontology)
    seeds = detect(snapshot, rules=world.rules, relevance=RelevanceOutcome(sections={})).seeds
    coverage = next(seed for seed in seeds if seed.detector == "MS-COVERAGE")
    props = {"detector": "MS-COVERAGE", "origin": "analysis"}

    kept = StoredGap(gap_id="GAP::K", props={**props, "seed_signatures": [coverage.signature]})
    bare = StoredGap(gap_id="GAP::B", props=props)

    assert still_fires(kept, seeds, snapshot, world.rules) is True
    assert still_fires(kept, (), snapshot, world.rules) is False
    assert still_fires(bare, seeds, snapshot, world.rules) is None


async def test_a_follow_up_keeps_its_site_in_the_next_run() -> None:
    rig, gap, _, rebuilt = await cancel_answered_and_built(3)
    await update_run(rig.world, rebuilt)
    opened = await rig.service.list_gaps(rig.project_id, about=[Planted.STATED_QUESTION], status="open")

    again = await run_gap(rig.world.context(build=rebuilt.build), mode="full")

    page = await rig.service.list_gaps(rig.project_id, about=[Planted.STATED_QUESTION], status="open")
    assert [item.gap_id for item in page.gaps] == [item.gap_id for item in opened.gaps] != [gap.gap_id]
    assert again.counts["gaps_new"]["by_kind"].get("open_question", 0) == 0
