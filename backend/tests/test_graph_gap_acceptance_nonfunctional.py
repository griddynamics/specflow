"""Acceptance tests of graph.gap: Non-functional (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from gap_acceptance_support import (
    CORPUS_FILES,
    Planted,
    RecordedGapGateway,
    assert_all_recorded,
    corpus_text,
    pending,
)
from gap_detection_support import PROJECT, corpus_reader
from gap_investigation_support import loop_world, round_of, site_about
from gap_run_support import corpus_world
from graph.core.llm.gateway import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from graph.gap import queries as gap_queries
from graph.gap import run_gap
from graph.gap.models import GapChangeReport
from graph.gap.prompts import INVESTIGATE_PROMPT, REPAIR_PROMPT
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph.stores.arcadedb.dialect import ArcadeDbDialect
from graph.stores.postgres.dialect import PostgresDialect
from graph_support import ScriptedRequest

_SENTENCE = re.compile(r"[A-Z][^.?!\n]{15,}[.?!]")


REFUNDS = "upload:payments-spec#payments-service-specification/refunds#56c00bc8"
NOTES = "upload:payments-spec#payments-service-specification/notes#ebf4f9a3"
REFUND_TO_CARD = "REQ::FR::REFUND_TO_ORIGINAL_CARD"


def _refund_report(*, injected: bool) -> dict[str, Any]:
    """What the model reports on the refund-to-card site; a model that read the injected line adds what the checks
    must drop: an id it was never shown, one that does not exist, and an option replacing the injected line."""

    def option(label: str, text: str, *, recommended: bool) -> dict[str, Any]:
        target = {"fragment_id": REFUNDS, "anchor_quote": Planted.POINTER_ANSWER_LINE, "heading": None}
        edit = {"placement": "amend", "target": target, "operation": "insert_after", "text": text}
        return {"label": label, "detail": "", "recommended": recommended, "edits": [edit], "basis": []}

    report: dict[str, Any] = {
        "verdict": "gap",
        "kind": "missing",
        "severity": "minor",
        "title": "Refund to card has no acceptance criteria",
        "question": "How is a refund to the original card checked as done?",
        "why": "The requirement states a time but no acceptance criteria.",
        "about": [REFUND_TO_CARD],
        "witness": [{"fragment_id": REFUNDS, "quote": Planted.POINTER_ANSWER_LINE, "fact": None}],
        "options": [
            option(
                "A: Card statement shows the refund",
                "A refund is done when the card statement shows it.",
                recommended=True,
            ),
            option(
                "B: Provider confirms the refund",
                "A refund is done when the card provider confirms it.",
                recommended=False,
            ),
        ],
    }
    if injected:
        target = {"fragment_id": NOTES, "anchor_quote": Planted.INJECTION_LINE, "heading": None}
        edit = {"placement": "amend", "target": target, "operation": "replace", "text": "No gap."}
        report["about"] = [*report["about"], Planted.OWNERLESS_UNCORROBORATED, "QST::INJECTED"]
        report["options"] = [
            *report["options"],
            {"label": "D: Report no gap", "detail": "", "recommended": False, "edits": [edit], "basis": []},
        ]
    return report


async def _injection_run(reader: Any) -> tuple[Any, list[str]]:
    site_id = await site_about(REFUND_TO_CARD)
    prompts: list[str] = []

    def answer(request: ScriptedRequest) -> Any:
        if round_of(request) == 1:
            return {"action": "read", "reads": [{"read": "find_fragments", "words": "reviewer note"}]}
        text = str(request.messages[-1]["content"])
        prompts.append(text)
        return {"action": "report", "reads": [], "report": _refund_report(injected=Planted.INJECTION_LINE in text)}

    gateway = RecordedGapGateway()
    gateway.handle(INVESTIGATE_PROMPT, answer)
    gateway.handle(REPAIR_PROMPT, lambda _request: {"action": "report", "report": _refund_report(injected=False)})
    world = await loop_world(reader, gateway=gateway)
    await run_gap(world.context(), mode="site", site_ids=[site_id])
    assert_all_recorded(gateway)
    return world, prompts


def _written(world: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    volatile = {"run_id", "created_at", "updated_at"}
    gaps = {
        gap_id: {k: v for k, v in props.items() if k not in volatile} for gap_id, props in world.gap_nodes().items()
    }
    options = {
        gap_id: [row.model_dump(exclude={"created_run"}) for row in rows]
        for gap_id, rows in world.records.option_rows[PROJECT].items()
    }
    return gaps, options


async def test_nfr_gsec_01_no_model_output_written_before_code_validated() -> None:
    """Prompt-injection fixtures in the shop corpus change no gap, option or answer beyond what the checks allow."""

    clean_world, _ = await _injection_run(corpus_reader(replace={Planted.INJECTION_LINE: "Reviewer note: none."}))
    injected_world, prompts = await _injection_run(None)

    assert any(Planted.INJECTION_LINE in text for text in prompts)
    for text in prompts:
        position = text.find(Planted.INJECTION_LINE)
        assert position < 0 or text.rfind(UNTRUSTED_OPEN, 0, position) > text.rfind(UNTRUSTED_CLOSE, 0, position)
    clean, injected = _written(clean_world), _written(injected_world)
    assert clean[0] and clean == injected
    (gap,) = injected[0].values()
    assert injected_world.about(next(iter(injected[0]))) == {REFUND_TO_CARD}
    assert gap["kind"] == "missing" and len(next(iter(injected[1].values()))) == 2
    assert len(injected_world.gateway.calls_of("gap_investigate")) == 3  # two rounds and one repair call


async def test_nfr_gsec_02_logs_contain_document_text_questions_answers_quotes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log capture of a full run and an answer contains no fixture sentence.

    Session 05: a full run and a second (update) run of the shop corpus; answers come with sessions 07 and 08.
    """

    world = await loop_world()
    with caplog.at_level(logging.DEBUG):
        await run_gap(world.context(), mode="full")
        await run_gap(world.context())

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    sentences = {match.group(0) for name in CORPUS_FILES for match in _SENTENCE.finditer(corpus_text(name))}
    questions = {props["question"] for props in world.gap_nodes().values()}
    assert any(record.name.startswith("graph.gap") for record in caplog.records)
    assert sentences and not [sentence for sentence in sentences | questions if sentence in logged]


async def test_nfr_gcost_01_update_run_after_build_changed_no_document() -> None:
    """Gateway fake records zero calls."""

    world = await loop_world()
    first = await run_gap(world.context(), mode="full")
    calls = len(world.gateway.calls)
    unchanged = GapChangeReport(build_id="build-2")

    second = await run_gap(world.context(changes=[unchanged]))

    assert first.cost["gap_investigate"]["calls"] == 19 and second.mode == "update"
    assert len(world.gateway.calls) == calls
    assert all(value == {"calls": 0, "tokens": 0} for value in second.cost.values())
    assert_all_recorded(world.gateway)


@pending("15")
def test_nfr_gcost_02_update_run_make_at_most_calls_limits() -> None:
    """The run's cost report stays within the bound computed from the rules."""


@pending("15", "manual benchmark; early check: 04")
def test_nfr_gperf_01_detection_over_graph_5_000_spec_nodes() -> None:
    """Benchmark test on a generated graph (manual run, reported in the pull request)."""


async def test_nfr_gport_01_graph_gap_run_graph_store_core_supports() -> None:
    """The gap integration tests run against each configured store.

    Offline: a full run renders every statement through the Cypher and PostgreSQL dialects (the in-memory executor
    plans each one), and every gap query has a plan on ArcadeDB too. Live: ``test_graph_gap_run_live.py`` runs the
    run, the record step and the reads on the store ``SPECFLOW_TEST_GRAPH_STORE`` selects.
    """

    world = await corpus_world()
    await run_gap(world.context(), mode="full")

    assert world.executor.called("gap_read_gaps") >= 2 and world.gap_nodes()
    params = {"project_id": "shop", "statuses": ["open"], "gap_ids": []}
    query = gap_queries.READ_GAPS
    assert binds_project_id(render_cypher(query))
    assert PostgresDialect().plan(query, params) is not None
    assert ArcadeDbDialect().plan(query, params) is not None
