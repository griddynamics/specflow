"""Acceptance tests of graph.gap: Non-functional (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import logging
import re

import pytest

from gap_acceptance_support import CORPUS_FILES, corpus_text, pending
from gap_run_support import corpus_world
from graph.gap import queries as gap_queries
from graph.gap import run_gap
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph.stores.arcadedb.dialect import ArcadeDbDialect
from graph.stores.postgres.dialect import PostgresDialect

_SENTENCE = re.compile(r"[A-Z][^.?!\n]{15,}[.?!]")


@pending("06")
def test_nfr_gsec_01_no_model_output_written_before_code_validated() -> None:
    """Prompt-injection fixtures in the shop corpus change no gap, option or answer beyond what the checks allow."""


async def test_nfr_gsec_02_logs_contain_document_text_questions_answers_quotes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log capture of a full run and an answer contains no fixture sentence.

    Session 05: a full run and a second (update) run of the shop corpus; answers come with sessions 07 and 08.
    """

    world = await corpus_world()
    with caplog.at_level(logging.DEBUG):
        await run_gap(world.context(), mode="full")
        await run_gap(world.context())

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    sentences = {match.group(0) for name in CORPUS_FILES for match in _SENTENCE.finditer(corpus_text(name))}
    questions = {props["question"] for props in world.gap_nodes().values()}
    assert any(record.name.startswith("graph.gap") for record in caplog.records)
    assert sentences and not [sentence for sentence in sentences | questions if sentence in logged]


@pending("06")
def test_nfr_gcost_01_update_run_after_build_changed_no_document() -> None:
    """Gateway fake records zero calls."""


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
