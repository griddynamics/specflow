"""PDF-parser logging tests (Wave 5 target: src/file_parsers/pdf/parser.py
replaces its ad-hoc `logging.getLogger` / `logging.basicConfig` with the
central logger).

Source: the central-logger test catalog §3, INT-33.

Builds a minimal real PDF with `fitz` (pymupdf is already a project
dependency) so `PDFParser.parse()` runs its real extraction path, then
forces `PDFParser.enrich()` down its `AnthropicError` fallback branch by
monkeypatching `PdfFigureEnricher.enrich` directly. No network call happens
either way: today `Client()`'s underlying `Anthropic()` SDK client is lazy
(constructing it with no credentials does not raise — confirmed by hand),
so it would only fail once a real request is attempted; raising straight
from the enricher is the deterministic, offline way to reach the same
`except AnthropicError` branch in `PDFParser.enrich()`.

Today (pre-Wave-5) `_StageTracker` and `PDFParser.enrich()`'s except-block
both log through the module's ad-hoc `logging.getLogger(__name__)`, with no
`event` field and no `exc_info=True` — confirmed by hand (the plain
`logging` records carry no `event` attribute and `exc_info=None`) — so
every assertion below is expected red until `w5-fp` lands.

Deferred: an explicit assertion that `logging.basicConfig` (currently
called in this file's `__main__` block, parser.py:149-152) is gone after
Wave 5 removes it. Exercising that would mean invoking the module's
`__main__` block (e.g. via a second subprocess), which is disproportionate
for this file. `tests/test_logging_adoption.py`'s INT-22 scan
(`test_basicconfig_is_never_called_outside_src_logger`) already covers
`src/file_parsers/pdf/parser.py` for exactly this — and is red today for
exactly this file — so the same target-state requirement is not left
unchecked, just checked from that file instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

import fitz
import pytest
from anthropic import AnthropicError

import file_parsers.pdf.parser as pdf_parser_module
from file_parsers.pdf.parser import PDFParser
from logger_harness import events, fields

PARSER_LOGGER_NAME = "file_parsers.pdf.parser"


def _write_minimal_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(50, 50, 500, 300),
        "Central Logger Test Spec\n\nThis is a minimal fixture PDF used only to exercise PDFParser's stage logging.",
        fontsize=14,
    )
    document.save(str(path))
    document.close()


@pytest.fixture
def pdf_parser(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> PDFParser:
    """Build a `PDFParser` over a fresh fitz-made PDF, ready to run its real
    extraction path.

    Deliberately does NOT call `.parse()` here: `caplog.records` only
    captures records emitted during the test's `call` phase by default, not
    `setup` (fixture bodies run during setup), so a `parse()` call inside
    this fixture would make every `pdf.stage.start`/`pdf.stage.done`
    assertion fail against a correct implementation too. Each test calls
    `.parse()` itself, inside its own body. `PdfMarkdownExtractor.extract()`
    (what `parse()` calls) only reads the local fitz-generated PDF above —
    no network, no LLM call — so re-running it per test is cheap; there is
    no need for a shared parse-once fixture here.
    """

    pdf_path = tmp_path / "sample.pdf"
    _write_minimal_pdf(pdf_path)

    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)

    return PDFParser(pdf_path, tmp_path / "out")


def test_parse_emits_a_stage_start_and_done_event_pair(pdf_parser: PDFParser, caplog: pytest.LogCaptureFixture) -> None:
    """INT-33: `parse()` alone must emit one `pdf.stage.start` and one
    `pdf.stage.done` for the "parse" stage. `finish()` is called explicitly
    here to force the still-open final stage closed, since nothing else in
    this test proceeds to the next stage."""

    pdf_parser.parse()
    pdf_parser.stages.finish()

    starts = events(caplog, "pdf.stage.start")
    dones = events(caplog, "pdf.stage.done")

    assert starts, "expected at least one pdf.stage.start record"
    assert dones, "expected at least one pdf.stage.done record"
    assert all(record.levelname == "INFO" for record in starts)
    assert all(record.levelname == "INFO" for record in dones)

    parse_starts = [r for r in starts if fields(r).get("stage") == "parse"]
    parse_dones = [r for r in dones if fields(r).get("stage") == "parse"]
    assert parse_starts, f"no parse-stage start among {[fields(r) for r in starts]}"
    assert parse_dones, f"no parse-stage done among {[fields(r) for r in dones]}"
    assert isinstance(fields(parse_dones[0]).get("duration_ms"), (int, float))


def test_enrich_falls_back_and_logs_a_warning_with_traceback_on_anthropic_error(
    pdf_parser: PDFParser,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INT-33: an AnthropicError raised mid-enrich must be swallowed (the
    deterministic document is kept, never lost) and logged as WARNING
    `pdf.enrich.fallback` with `exc_info=True` — never a silent `pass`."""

    def _raise(self: object, document: object) -> None:
        raise AnthropicError("simulated vision-model failure")

    monkeypatch.setattr(pdf_parser_module.PdfFigureEnricher, "enrich", _raise)

    pdf_parser.parse()
    before = pdf_parser.document

    result = pdf_parser.enrich()

    assert pdf_parser.enrichment_error == "AnthropicError"
    # `enrich()` keeps the pre-enrichment content by value, via its own
    # `model_copy(deep=True)` taken before the enrichers ran — not the same
    # object reference, so this is a value comparison, not `is`.
    assert result == before

    fallbacks = events(caplog, "pdf.enrich.fallback")
    assert len(fallbacks) == 1, f"expected exactly 1 pdf.enrich.fallback record, got {[fields(r) for r in fallbacks]}"

    record = fallbacks[0]
    assert record.levelname == "WARNING"
    assert record.exc_info is not None, "must log with exc_info=True"
    assert fields(record).get("error_type") == "AnthropicError"


def test_enrich_still_emits_the_stage_start_for_the_enrich_stage(
    pdf_parser: PDFParser,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entering the enrich stage (even one that immediately fails) still
    closes out "parse" and announces its own `pdf.stage.start`, so a
    partial run's stage timeline is never silently dropped."""

    def _raise(self: object, document: object) -> None:
        raise AnthropicError("simulated vision-model failure")

    monkeypatch.setattr(pdf_parser_module.PdfFigureEnricher, "enrich", _raise)

    pdf_parser.parse()
    pdf_parser.enrich()

    starts = events(caplog, "pdf.stage.start")
    stage_names = {fields(record).get("stage") for record in starts}

    assert "enrich:figure-description" in stage_names, stage_names
