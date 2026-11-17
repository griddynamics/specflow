"""The migration of first-slice findings offline (docs/graph/gap/lifecycle.md "Migration of first-slice findings",
FR-GDEV-07): open and resolved findings become development gaps whose ``aliases`` keep the old id, dismissed findings
suppressions, notes ``gap_note`` rows; the first-slice nodes are deleted and a second run does nothing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import graph.gap.schema as gap_schema
from gap_port_fakes import MemoryGapRecords
from gap_support import PROJECT, Clock, MemoryGapExecutor, spec_graph
from graph.core import load_ontology
from graph.core.errors import GraphWriteError
from graph.gap import ensure_gap_schema
from graph.gap.gaps import cite_key, link_key, quote_hash, read_gaps
from graph.gap.identity import clr_id, development_signature, gap_id
from graph.gap.migrate import MIGRATION_RUN, NO_NOTE, QUESTION_CHARS, migrate_first_slice, question_of, title

CREATED = "2026-03-01T09:00:00.000+00:00"
CLOSED = "2026-03-02T10:30:00.000+00:00"
STORY = "STORY::CHECKOUT"
FRAGMENT = "FRG::payments::1"
LONG = "Which card brands does checkout accept? " + "Every brand needs its own rule. " * 40


def _finding(kind: str, status: str, text: str, **extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "kind": kind,
        "status": status,
        "text": text,
        "origin": "agent",
        "blocking": False,
        "created_by": "agent-1",
        "created_at": CREATED,
        "updated_at": CREATED,
        "orphaned": False,
    }
    if status != "open":
        props |= {"resolved_by": "user-owner", "resolved_at": CLOSED, "updated_at": CLOSED}
    return props | extra


def first_slice() -> MemoryGapExecutor:
    executor = spec_graph(MemoryGapExecutor())
    executor.fragment(FRAGMENT, "upload:payments")
    executor.plant_finding(
        "FND::0000000000A1",
        about=["REQ::FR::CARD_PAYMENT"],
        refs=[("pr", "42"), ("file", "src/pay.py")],
        **_finding("question", "open", "May a card payment be retried?", blocking=True, story_id=STORY),
    )
    executor.plant_finding(
        "FND::0000000000B2",
        about=["FEAT::CARD_PAYMENT", "MOD::PAYMENTS_SERVICE"],
        **_finding("conflict", "resolved", "Two refund limits disagree.", resolution_note="The 30-day limit holds."),
    )
    executor.plant_finding("FND::0000000000C3", about=["REQ::FR::REFUND"], **_finding("gap", "resolved", "Refund?"))
    executor.plant_finding(
        "FND::0000000000D4", about=["REQ::FR::REFUND"], **_finding("gap", "dismissed", "Refund currency?")
    )
    executor.plant_finding(
        "FND::0000000000E5", about=["MOD::PAYMENTS_SERVICE"], **_finding("note", "open", "The SDK pins TLS 1.2.")
    )
    executor.plant_finding("FND::0000000000F6", fragments=[FRAGMENT], **_finding("gap", "open", "Which fee applies?"))
    executor.plant_finding("FND::0000000000A7", about=["REQ::FR::CARD_PAYMENT"], **_finding("gap", "open", LONG))
    return executor


async def _migrate(executor: MemoryGapExecutor, records: MemoryGapRecords) -> Any:
    return await migrate_first_slice(executor, load_ontology(), PROJECT, records, clock=Clock())


async def _by_alias(executor: MemoryGapExecutor) -> dict[str, Any]:
    gaps = await read_gaps(executor, PROJECT, load_ontology())
    return {alias: gap for gap in gaps for alias in gap.props.get("aliases") or ()}


async def test_fr_gdev_07_open_and_resolved_findings_become_development_gaps_with_their_old_id() -> None:
    executor, records = first_slice(), MemoryGapRecords()

    result = await _migrate(executor, records)

    assert (result.gaps, result.resolved, result.suppressed, result.notes, result.deleted) == (5, 2, 1, 1, 7)
    found = await _by_alias(executor)
    assert set(found) == {
        "FND::0000000000A1",
        "FND::0000000000B2",
        "FND::0000000000C3",
        "FND::0000000000F6",
        "FND::0000000000A7",
    }
    open_gap = found["FND::0000000000A1"]
    signature = development_signature(STORY, ["REQ::FR::CARD_PAYMENT"], "May a card payment be retried?")
    assert open_gap.gap_id == gap_id(signature)
    assert {name: open_gap.props[name] for name in ("kind", "severity", "status", "origin", "route", "detector")} == {
        "kind": "open_question",
        "severity": "major",
        "status": "open",
        "origin": "development",
        "route": "ask",
        "detector": "development",
    }
    assert open_gap.props["blocking"] == "yes" and open_gap.props["held_back"] == "no"
    assert (open_gap.props["story_id"], open_gap.props["created_by"]) == (STORY, "agent-1")
    assert open_gap.props["created_at"] == CREATED and open_gap.props["signature"] == signature
    assert open_gap.about_ids == ("REQ::FR::CARD_PAYMENT",)
    assert open_gap.about[0].edge_key == link_key(open_gap.gap_id, "ABOUT", "REQ::FR::CARD_PAYMENT")
    assert found["FND::0000000000B2"].props["kind"] == "contradiction"
    assert found["FND::0000000000C3"].props["kind"] == "missing"
    assert found["FND::0000000000B2"].props["blocking"] == "no"


async def test_a_resolved_finding_gets_an_applied_own_clarification_with_its_closing_note() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)
    found = await _by_alias(executor)

    noted, bare = found["FND::0000000000B2"], found["FND::0000000000C3"]

    assert (noted.status, noted.props["resolution"], noted.props["resolved_at"]) == ("resolved", "answered", CLOSED)
    (clarification,) = noted.clarifications
    closed = datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert clarification == {
        "clr_id": clr_id(noted.gap_id, "The 30-day limit holds.", closed),
        "uuid": clarification["uuid"],
        "mode": "own",
        "placement": "none",
        "status": "applied",
        "option_label": "",
        "text": "The 30-day limit holds.",
        "answered_by": "user-owner",
        "answered_at": CLOSED,
    }
    assert bare.clarifications[0]["text"] == NO_NOTE
    events = await records.events(PROJECT, noted.gap_id)
    assert [event.event for event in events] == ["created", "resolved"]
    assert events[1].clr_id == clarification["clr_id"] and events[1].at == closed


async def test_dismissed_findings_become_suppressions_and_notes_gap_notes_without_nodes() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)

    (suppression,) = records.suppressions[PROJECT].values()
    assert suppression.signature == development_signature("", ["REQ::FR::REFUND"], "Refund currency?")
    assert (suppression.gap_id, suppression.question, suppression.dismissed_by) == (
        None,
        "Refund currency?",
        "user-owner",
    )
    assert suppression.dismissed_at == datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    (note,) = records.notes[PROJECT]
    assert (note.run_id, note.detector, note.spec_ids, note.text) == (
        MIGRATION_RUN,
        "development",
        ("MOD::PAYMENTS_SERVICE",),
        "The SDK pins TLS 1.2.",
    )
    found = await _by_alias(executor)
    assert "FND::0000000000D4" not in found and "FND::0000000000E5" not in found


async def test_refs_and_the_alias_go_into_the_first_event_and_a_fragment_becomes_a_cites() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)
    found = await _by_alias(executor)

    (created,) = await records.events(PROJECT, found["FND::0000000000A1"].gap_id)
    assert created.event == "created" and created.actor == "agent-1"
    assert created.at == datetime(2026, 3, 1, 9, tzinfo=UTC)
    assert created.detail["alias"] == "FND::0000000000A1" and created.detail["migrated"] is True
    assert created.detail["refs"] == [{"type": "file", "value": "src/pay.py"}, {"type": "pr", "value": "42"}]
    cited = found["FND::0000000000F6"]
    assert cited.about == ()
    (cite,) = cited.cites
    assert (cite.fragment_id, cite.quote) == (FRAGMENT, "")
    assert cite.edge_key == cite_key(cited.gap_id, FRAGMENT, quote_hash(""))


async def test_a_long_text_is_cut_to_the_question_limit_and_the_title_is_its_first_line() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)

    long_gap = (await _by_alias(executor))["FND::0000000000A7"]

    assert len(long_gap.props["question"]) <= QUESTION_CHARS
    assert long_gap.props["question"] == question_of(LONG)
    assert long_gap.props["title"] == title(question_of(LONG))
    assert len(long_gap.props["title"]) <= 160


async def test_the_current_ids_of_a_rekeyed_node_are_used() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    executor.rekey_spec("REQ::FR::REFUND", "REQ::FR::REFUND_ORDER")

    await _migrate(executor, records)

    assert (await _by_alias(executor))["FND::0000000000C3"].about_ids == ("REQ::FR::REFUND_ORDER",)


async def test_the_first_slice_nodes_are_deleted_and_a_second_run_is_a_no_op() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)
    before = (dict(executor.nodes), dict(executor.edges), len(records.event_rows[PROJECT]), len(records.notes[PROJECT]))

    again = await _migrate(executor, records)

    assert executor.findings() == {} and executor.refs() == []
    assert not executor.edge_keys("REFERS_TO")
    assert again.gaps == again.deleted == 0
    after = (dict(executor.nodes), dict(executor.edges), len(records.event_rows[PROJECT]), len(records.notes[PROJECT]))
    assert after == before


async def test_a_run_repeated_after_a_failed_delete_writes_no_gap_or_event_twice() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    executor.fail_on = "gap_delete_first_slice_findings"
    with pytest.raises(GraphWriteError):
        await _migrate(executor, records)
    gaps = dict(executor.gaps())
    events = len(records.event_rows[PROJECT])
    executor.fail_on = None

    result = await _migrate(executor, records)

    assert executor.gaps() == gaps and len(records.event_rows[PROJECT]) == events
    assert (result.gaps, result.merged, result.deleted) == (5, 0, 7)
    assert executor.findings() == {}


async def test_a_finding_with_the_signature_of_an_existing_gap_becomes_its_alias() -> None:
    executor, records = first_slice(), MemoryGapRecords()
    await _migrate(executor, records)
    executor.plant_finding(
        "FND::0000000000A9",
        about=["REQ::FR::CARD_PAYMENT"],
        **_finding("question", "open", "May a card payment be retried?", story_id=STORY),
    )

    result = await _migrate(executor, records)

    assert (result.gaps, result.merged) == (0, 1)
    found = await _by_alias(executor)
    assert found["FND::0000000000A9"].gap_id == found["FND::0000000000A1"].gap_id
    events = await records.events(PROJECT, found["FND::0000000000A9"].gap_id)
    assert [event.event for event in events] == ["created", "aliased"]


async def test_a_project_without_findings_is_left_as_it_is() -> None:
    executor, records = spec_graph(MemoryGapExecutor()), MemoryGapRecords()

    result = await _migrate(executor, records)

    assert result.gaps == result.deleted == 0
    assert [name for name, _ in executor.calls] == ["gap_read_first_slice"]


async def test_ensure_gap_schema_migrates_the_named_projects_and_skips_without_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def schema(*_args: Any) -> tuple[str, ...]:
        return ("graph_gap_id",)

    monkeypatch.setattr(gap_schema, "ensure_schema", schema)
    executor, records = first_slice(), MemoryGapRecords()

    assert await ensure_gap_schema(executor, load_ontology(), None, project_ids=[PROJECT]) == ("graph_gap_id",)
    assert len(executor.findings()) == 7
    assert await ensure_gap_schema(executor, load_ontology(), None, records=records) == ("graph_gap_id",)
    assert len(executor.findings()) == 7

    assert await ensure_gap_schema(executor, load_ontology(), None, records=records, project_ids=[PROJECT]) == (
        "graph_gap_id",
    )
    assert executor.findings() == {} and len(executor.gaps()) == 5
