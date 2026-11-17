"""Acceptance tests of graph.gap: Development gaps (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from typing import Any

import pytest

import graph.gap.schema as gap_schema
from gap_acceptance_support import Planted
from gap_development_support import (
    ADD,
    ALPHA,
    API_CONTRACT,
    API_LOGIC,
    API_STORY,
    GAPS,
    QUESTION,
    REFUND_TO_CARD,
    REFUNDS,
    OneReport,
    claim,
    dev_rig,
    report,
    report_blocking,
    task,
)
from gap_port_fakes import MemoryGapRecords
from gap_support import PROJECT, MemoryGapExecutor, spec_graph
from graph.core import load_ontology
from graph.gap import ensure_gap_schema
from graph.gap.gaps import read_gaps
from graph.gap.models import Quote
from graph.gap.prompts import JUDGE_PROMPT


@pytest.fixture
async def dev(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> Any:
    async with dev_rig(monkeypatch, valid_env) as rig:
        yield rig


async def test_fr_gdev_01_finding_reported_over_mcp_become_gap_origin(dev: Any) -> None:
    """Given `add_finding` with kind question, then an `open_question` gap with origin `development` exists."""

    dev.world.investigator = OneReport(report(kind="open_question"))
    body = {"kind": "question", "text": QUESTION, "about": [Planted.PLACE_ORDER], "story_id": API_STORY}

    added = (await dev.rig.post(ADD, body, project_id=ALPHA)).json()

    gap = await dev.gaps.get_gap(ALPHA, added["finding"]["finding_id"])
    assert (gap.kind, gap.origin, gap.status, gap.story_id) == ("open_question", "development", "open", API_STORY)


async def test_fr_gdev_02_blocking_development_gap_holds_the_claimed_story(dev: Any) -> None:
    """Given a blocking finding, then the story's claimed tasks are `blocked` with `blocked_by` the gap id."""

    added = await report_blocking(dev, await claim(dev))

    gap_id = added["finding"]["finding_id"]
    assert added["blocked"] == [API_CONTRACT, API_LOGIC]
    assert {(task(dev, item)["status"], task(dev, item)["blocked_by"]) for item in added["blocked"]} == {
        ("blocked", gap_id)
    }
    assert dev.store.stories[API_STORY]["claimed_by"] is None


async def test_fr_gdev_03_development_gap_investigated_at_once_mode_site(dev: Any) -> None:
    """Given a new development gap, then options exist before the next build and the question is the agent's text."""

    added = await report_blocking(dev, await claim(dev))

    gap = await dev.gaps.get_gap(ALPHA, added["finding"]["finding_id"])
    assert dev.runner.sites == [(ALPHA, gap.gap_id)] and dev.world.investigator.calls == 1
    assert len(gap.options) == 2 and gap.question == QUESTION


async def test_fr_gdev_04_spec_already_answers_development_gap_resolve_pointer(dev: Any) -> None:
    """Given a question answered in `payments-spec.md`, then the story is released and the brief shows the answering
    lines.
    """

    both = (Planted.PLACE_ORDER, REFUND_TO_CARD)
    dev.world.investigator = OneReport(
        report(about=both, options=(), pointer=Quote(fragment_id=REFUNDS, quote=Planted.POINTER_ANSWER_LINE))
    )
    judge = {"entailed": True, "consistent": True, "confidence": 0.95, "reason": "It says so.", "find_words": None}
    dev.world.gateway.handle(JUDGE_PROMPT, lambda _request: judge)

    added = await report_blocking(dev, await claim(dev), about=both)

    gap_id = added["finding"]["finding_id"]
    assert task(dev, API_LOGIC)["status"] == "pending" and dev.builds.requests == []
    listed = await dev.rig.get(GAPS, project_id=ALPHA, story_id=API_STORY, status=["resolved"])
    answers = {item["gap_id"]: item["answer"] for item in listed.json()["items"]}
    assert Planted.POINTER_ANSWER_LINE in answers[gap_id]


async def test_fr_gdev_05_development_gap_always_routed_ask_needs_answer(dev: Any) -> None:
    """Given a minor development gap with every gate holding, then it is `ask`."""

    added = await report_blocking(dev, await claim(dev))

    gap = await dev.gaps.get_gap(ALPHA, added["finding"]["finding_id"])
    assert (gap.severity, gap.route, gap.status, gap.clarification) == ("minor", "ask", "open", None)


async def test_fr_gdev_06_not_spec_development_finding_closes_as_a_note(dev: Any) -> None:
    """Given a finding about a library bug, then no gap stays open and the story is released."""

    dev.world.investigator = OneReport(report(verdict="not_spec", not_spec_reason="A library bug.", options=()))
    text = "The HTTP client library times out on every retry?"

    added = await report_blocking(dev, await claim(dev), kind="gap", text=text)

    open_ids = {gap.gap_id for gap in (await dev.gaps.list_gaps(ALPHA, origin="development")).gaps}
    assert added["finding"]["finding_id"] not in open_ids
    assert task(dev, API_LOGIC)["status"] == "pending" and task(dev, API_CONTRACT)["status"] == "pending"
    assert dev.world.records.notes[ALPHA][-1].text == text


async def test_fr_gdev_07_existing_first_slice_findings_migrated_gaps_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a project with open and resolved findings, when `ensure_gap_schema` runs, then each has a gap with the
    mapped status and its old id resolves.
    """

    async def no_ddl(*_args: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(gap_schema, "ensure_schema", no_ddl)  # the DDL is FR-GONT-05's; this unit is the migration
    executor = spec_graph(MemoryGapExecutor())
    common: dict[str, Any] = {"origin": "agent", "created_by": "agent-1", "created_at": "2026-03-01T09:00:00.000+00:00"}
    closed: dict[str, Any] = {"resolved_by": "user-owner", "resolved_at": "2026-03-02T09:00:00.000+00:00"}
    executor.plant_finding(
        "FND::0000000000A1", about=["REQ::FR::CARD_PAYMENT"], kind="question", status="open", text="Retry?", **common
    )
    executor.plant_finding(
        "FND::0000000000B2",
        about=["REQ::FR::REFUND"],
        kind="gap",
        status="resolved",
        text="Refund?",
        **common,
        **closed,
    )
    ontology = load_ontology()

    await ensure_gap_schema(executor, ontology, None, records=MemoryGapRecords(), project_ids=[PROJECT])

    gaps = await read_gaps(executor, PROJECT, ontology)
    by_alias = {alias: gap for gap in gaps for alias in gap.props.get("aliases") or ()}
    assert {alias: (gap.origin, gap.props["kind"], gap.status) for alias, gap in by_alias.items()} == {
        "FND::0000000000A1": ("development", "open_question", "open"),
        "FND::0000000000B2": ("development", "missing", "resolved"),
    }
    assert executor.findings() == {}
