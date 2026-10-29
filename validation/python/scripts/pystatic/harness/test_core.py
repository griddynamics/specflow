from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override

from pystatic.harness import core, models
from pystatic.harness.core import (
    aggregate_exit,
    is_outer_run,
    main,
    parse_findings,
    print_result,
    run_step,
    status_for,
    unit_run,
)
from pystatic.harness.models import Finding, StepResult

_HARNESS_DEPTH_PREFIX = "HARNESS_RUN_DEPTH_"
_saved_harness_depth_env: dict[str, str] = {}


def setUpModule() -> None:
    """Snapshot and clear every `HARNESS_RUN_DEPTH_*` claim once for the whole module.

    An inherited ambient claim silently broke assertions; with a claim-free baseline each
    test's tearDown may `pop` what it set, and `tearDownModule` restores the snapshot."""
    for key in list(os.environ):
        if key.startswith(_HARNESS_DEPTH_PREFIX):
            _saved_harness_depth_env[key] = os.environ.pop(key)


def tearDownModule() -> None:
    """Restore exactly what `setUpModule` snapshotted -- never a bare wipe,
    so a real ambient claim (not just a test-injected one) survives this
    module's run for whatever holds it."""
    for key in list(os.environ):
        if key.startswith(_HARNESS_DEPTH_PREFIX):
            del os.environ[key]
    os.environ.update(_saved_harness_depth_env)
    _saved_harness_depth_env.clear()


class FindingRenderTests(unittest.TestCase):
    def test_render_includes_fix_and_doc(self) -> None:
        f = Finding(
            "a/b.py",
            3,
            5,
            "some-rule",
            "did a bad thing",
            fix="do X",
            doc="/guide.md#validation",
        )
        assert (
            f.render() == "a/b.py:3:5: some-rule: did a bad thing"
            " (fix: do X; doc: /guide.md#validation)"
        )

    def test_render_without_fix_or_doc(self) -> None:
        f = Finding("a/b.py", 3, 5, "some-rule", "did a bad thing")
        assert f.render() == "a/b.py:3:5: some-rule: did a bad thing"


class StepResultTests(unittest.TestCase):
    def test_ok_is_exit_zero(self) -> None:
        assert StepResult.ok("x").exit_code == 0

    def test_from_findings_exit_one_when_nonempty(self) -> None:
        result = StepResult.from_findings("x", [Finding("a", 1, 1, "r", "m")])
        assert result.exit_code == 1

    def test_from_findings_exit_zero_when_empty(self) -> None:
        assert StepResult.from_findings("x", []).exit_code == 0


class StatusForTests(unittest.TestCase):
    """The routed reviewer request: a skip is machine-readable (a `status`
    field), not only printed -- and distinct from a clean exit-0 pass."""

    def test_ok_status(self) -> None:
        assert status_for(StepResult.ok("x")) == "ok"

    def test_skipped_status_is_distinct_from_ok(self) -> None:
        assert status_for(StepResult.ok("x", detail="skipped: no targets")) == "skipped"

    def test_findings_status(self) -> None:
        assert (
            status_for(StepResult.from_findings("x", [Finding("a", 1, 1, "r", "m")])) == "findings"
        )

    def test_tool_error_status_for_step_missing(self) -> None:
        assert status_for(StepResult.step_missing("x", "y")) == "tool-error"

    def test_tool_error_status_for_tool_missing(self) -> None:
        assert status_for(StepResult.tool_missing("x", "y")) == "tool-error"


class RunStepTests(unittest.TestCase):
    def test_fail_slow_turns_an_exception_into_tool_error(self) -> None:
        def boom() -> StepResult:
            error = "kaboom"
            raise ValueError(error)

        result = run_step("x", boom)
        assert result.exit_code == 2
        assert "tool-error" in result.detail
        assert "kaboom" in result.detail

    def test_records_duration(self) -> None:
        result = run_step("x", lambda: StepResult.ok("x"))
        assert result.duration_ms >= 0


class PrintResultTests(unittest.TestCase):
    """Reviewer-required: the printed status word must be `status_for`'s
    word (a skip must say `skipped`, never `ok`), and every exit code the
    contract allows -- including a step-missing/tool-missing's 2, which
    the old `{0,1,2}` literal dict happened to also cover -- must print
    without raising."""

    def _status_line(self, result: StepResult) -> str:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            print_result(result)
        return err.getvalue()

    def test_ok_prints_ok(self) -> None:
        assert "] ok (" in self._status_line(StepResult.ok("x"))

    def test_skip_prints_skipped_not_ok(self) -> None:
        line = self._status_line(StepResult.ok("x", detail="skipped: no targets"))
        assert "] skipped (" in line
        assert "] ok (" not in line
        assert "skipped: no targets" in line

    def test_findings_prints_findings(self) -> None:
        result = StepResult.from_findings("x", [Finding("a", 1, 1, "r", "m")])
        assert "] findings (" in self._status_line(result)

    def test_tool_error_prints_tool_error(self) -> None:
        line = self._status_line(StepResult.tool_missing("x", "sometool"))
        assert "] tool-error (" in line

    def test_findings_are_printed_to_stdout(self) -> None:
        result = StepResult.from_findings("x", [Finding("a.py", 1, 1, "r", "m")])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            print_result(result)
        assert "a.py:1:1: r: m" in out.getvalue()

    def test_unstructured_failure_prints_retained_tool_output(self) -> None:
        result = StepResult(
            step="integration-tests",
            exit_code=1,
            detail="pytest exited 1",
            raw_output="FAILED tests/test_api.py::test_contract - timeout",
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            print_result(result)
        assert "tests/test_api.py::test_contract - timeout" in out.getvalue()

    def test_structured_findings_do_not_duplicate_raw_output(self) -> None:
        result = StepResult.from_findings("x", [Finding("a.py", 1, 1, "r", "m")])
        result.raw_output = "duplicate tool output"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            print_result(result)
        assert "duplicate tool output" not in out.getvalue()


class ParseFindingsTests(unittest.TestCase):
    def test_parses_a_contract_line(self) -> None:
        findings = parse_findings("a.py:3:5: some-rule: did a bad thing\n")
        assert len(findings) == 1
        assert findings[0] == Finding("a.py", 3, 5, "some-rule", "did a bad thing")

    def test_non_matching_lines_are_ignored(self) -> None:
        assert parse_findings("not a contract line\n\n") == []

    def test_multiple_lines(self) -> None:
        text = "a.py:1:1: r1: m1\nb.py:2:2: r2: m2\n"
        assert len(parse_findings(text)) == 2


class MainTests(unittest.TestCase):
    """`main`: the shared `__main__` block -- fail-slow (an exception in
    `run_fn` becomes tool-error exit 2, never an uncaught traceback's exit
    1), so a step module's direct-invocation path carries the same
    contract as running it through a runner."""

    def test_returns_the_step_exit_code(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main("x", lambda: StepResult.ok("x"))
        assert code == 0

    def test_an_uncaught_exception_becomes_tool_error_exit_2(self) -> None:
        def boom() -> StepResult:
            error = "no such tool"
            raise FileNotFoundError(error)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main("x", boom)
        assert code == 2

    def test_prints_findings_and_status(self) -> None:
        def run_fn() -> StepResult:
            return StepResult.from_findings("x", [Finding("a.py", 1, 1, "r", "m")])

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main("x", run_fn)
        assert "a.py:1:1: r: m" in out.getvalue()
        assert "] findings (" in err.getvalue()


class AggregateExitTests(unittest.TestCase):
    def test_worst_of_wins(self) -> None:
        results = [
            StepResult.ok("a"),
            StepResult.from_findings("b", [Finding("a", 1, 1, "r", "m")]),
        ]
        assert aggregate_exit(results) == 1
        results.append(StepResult.step_missing("c", "x"))
        assert aggregate_exit(results) == 2

    def test_all_clean_is_zero(self) -> None:
        assert aggregate_exit([StepResult.ok("a"), StepResult.ok("b")]) == 0


class ReportsAndSummaryTests(unittest.TestCase):
    """`write_report`/`append_summary`/`reset_summary_for_unit` write
    Pydantic-validated JSON and regenerate `build/summary.schema.json`
    every run."""

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_reports = models.REPORTS_DIR
        self._orig_summary = models.SUMMARY_PATH
        self._orig_schema = models.SUMMARY_SCHEMA_PATH
        models.REPORTS_DIR = root / ".reports"
        models.SUMMARY_PATH = models.REPORTS_DIR / "summary.json"
        models.SUMMARY_SCHEMA_PATH = root / "build" / "summary.schema.json"

    @override
    def tearDown(self) -> None:
        models.REPORTS_DIR = self._orig_reports
        models.SUMMARY_PATH = self._orig_summary
        models.SUMMARY_SCHEMA_PATH = self._orig_schema
        self._tmp.cleanup()

    def test_reset_writes_an_empty_summary_and_a_schema(self) -> None:
        core.reset_summary_for_unit("api")
        assert json.loads(models.SUMMARY_PATH.read_text()) == []
        assert models.SUMMARY_SCHEMA_PATH.is_file()
        schema = json.loads(models.SUMMARY_SCHEMA_PATH.read_text())
        assert schema.get("type") == "array"

    def test_reset_keeps_other_units_entries(self) -> None:
        models.REPORTS_DIR.mkdir(parents=True)
        models.SUMMARY_PATH.write_text(json.dumps([{"unit": "system", "step": "hygiene"}]))
        core.reset_summary_for_unit("api")
        entries = json.loads(models.SUMMARY_PATH.read_text())
        assert entries == [{"unit": "system", "step": "hygiene"}]

    def test_write_report_matches_the_pydantic_model(self) -> None:
        result = StepResult.from_findings(
            "ruff", [Finding("a.py", 1, 2, "r", "m", doc="/guide.md#style")]
        )
        core.write_report("api", "ruff", result)
        payload = json.loads((models.REPORTS_DIR / "api" / "ruff.json").read_text())
        models.StepReportModel.model_validate(payload)  # raises on any shape drift
        assert payload["status"] == "findings"

    def test_write_report_removes_a_stale_txt_when_there_is_no_new_output(self) -> None:
        (models.REPORTS_DIR / "api").mkdir(parents=True)
        stale = models.REPORTS_DIR / "api" / "ruff.txt"
        stale.write_text("stale raw output from a bigger run")
        core.write_report("api", "ruff", StepResult.ok("ruff"))
        assert not stale.exists()

    def test_write_report_keeps_raw_output_when_present(self) -> None:
        result = StepResult.ok("ruff")
        result.raw_output = "fresh output"
        core.write_report("api", "ruff", result)
        assert (models.REPORTS_DIR / "api" / "ruff.txt").read_text() == "fresh output"

    def test_write_report_writes_the_tools_own_sarif_payload(self) -> None:
        # `.sarif` is the fourth guarded artifact kind (json/txt/xml/sarif):
        # a tool-native SARIF payload persisted verbatim, same guard as json/txt.
        result = StepResult.ok("ruff")
        result.sarif = '{"runs": []}'
        core.write_report("api", "ruff", result)
        assert (models.REPORTS_DIR / "api" / "ruff.sarif").read_text() == '{"runs": []}'

    def test_write_report_removes_a_stale_sarif_when_there_is_no_new_one(self) -> None:
        (models.REPORTS_DIR / "api").mkdir(parents=True)
        stale = models.REPORTS_DIR / "api" / "ruff.sarif"
        stale.write_text('{"runs": [{"stale": true}]}')
        core.write_report("api", "ruff", StepResult.ok("ruff"))  # no `.sarif` this time
        assert not stale.exists()

    def test_append_summary_matches_the_pydantic_model(self) -> None:
        core.append_summary("api", "ruff", StepResult.ok("ruff"))
        entries = json.loads(models.SUMMARY_PATH.read_text())
        models.SummaryModel.model_validate(entries)  # raises on any shape drift
        assert entries[0]["status"] == "ok"

    def test_write_report_for_a_foreign_pid_never_touches_the_sarif_either(self) -> None:
        # same guard as the json/txt foreign-pid test above, for `.sarif`:
        # a nested/foreign call must not overwrite the outer run's own SARIF.
        result = StepResult.ok("ruff")
        result.sarif = '{"runs": [{"outer": true}]}'
        core.write_report("api", "ruff", result)
        before = (models.REPORTS_DIR / "api" / "ruff.sarif").read_text()
        key = "HARNESS_RUN_DEPTH_API"
        os.environ[key] = "999999999:1"  # a pid that is never this process's own
        try:
            nested = StepResult.ok("ruff")
            nested.sarif = '{"runs": [{"nested": true}]}'
            core.write_report("api", "ruff", nested)
        finally:
            del os.environ[key]
        after = (models.REPORTS_DIR / "api" / "ruff.sarif").read_text()
        assert before == after

    def test_write_report_for_a_foreign_pid_never_touches_disk(self) -> None:
        # `write_report` reads `HARNESS_RUN_DEPTH_<UNIT>`
        # itself, rather than trusting an `is_outer` argument a caller could
        # default wrong -- a *subprocess* a step shells out to (a different
        # pid) inherits this env var from an active ancestor run but must
        # never mistake itself for that ancestor and clobber its report.
        core.write_report("api", "ruff", StepResult.from_findings("ruff", []))
        before = json.loads((models.REPORTS_DIR / "api" / "ruff.json").read_text())
        key = "HARNESS_RUN_DEPTH_API"
        os.environ[key] = "999999999:1"  # a pid that is never this process's own
        try:
            core.write_report(
                "api",
                "ruff",
                StepResult(step="ruff", exit_code=2, detail="tool-error: nested"),
            )
        finally:
            del os.environ[key]
        after = json.loads((models.REPORTS_DIR / "api" / "ruff.json").read_text())
        assert before == after


class IsOuterRunTests(unittest.TestCase):
    """Reproduced-live bug: `is_outer_run`
    is read directly by `write_report`/`append_summary`, never threaded as a
    caller-supplied argument that could default wrong."""

    KEY = "HARNESS_RUN_DEPTH_API"

    @override
    def tearDown(self) -> None:
        os.environ.pop(self.KEY, None)

    def test_no_ancestor_at_all_is_outer(self) -> None:
        assert is_outer_run("api")

    def test_this_process_at_depth_one_is_outer(self) -> None:
        os.environ[self.KEY] = f"{os.getpid()}:1"
        assert is_outer_run("api")

    def test_this_process_at_depth_two_is_not_outer(self) -> None:
        # a same-process nested `unit_run` (self-tests calling `run_level`
        # again in-process) increments depth without changing pid.
        os.environ[self.KEY] = f"{os.getpid()}:2"
        assert not is_outer_run("api")

    def test_a_different_pid_is_never_outer_regardless_of_depth(self) -> None:
        # the actual bug: a *subprocess* a step shells out to inherits this
        # env var from an active ancestor process but never entered
        # `unit_run` itself -- its pid can never match the recorded owner.
        os.environ[self.KEY] = "999999999:1"
        assert not is_outer_run("api")


class UnitRunNestingTests(unittest.TestCase):
    """Reproduced-live bug: a nested `run_level` of the same unit (self-
    tests spawning a real one) used to wipe the outer run's summary
    entries. `unit_run` depth-guards this; only the outermost call resets."""

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = models.REPORTS_DIR, models.SUMMARY_PATH, models.SUMMARY_SCHEMA_PATH
        models.REPORTS_DIR = root / ".reports"
        models.SUMMARY_PATH = models.REPORTS_DIR / "summary.json"
        models.SUMMARY_SCHEMA_PATH = root / "build" / "summary.schema.json"

    @override
    def tearDown(self) -> None:
        models.REPORTS_DIR, models.SUMMARY_PATH, models.SUMMARY_SCHEMA_PATH = self._orig
        self._tmp.cleanup()

    def test_nested_run_does_not_drop_the_outer_runs_entries(self) -> None:
        with unit_run("system") as outer:
            assert outer
            core.append_summary("system", "docs", StepResult.ok("docs"))
            with unit_run("system") as nested:
                # mypy narrows `nested` to `Literal[False]` after the `assert` above,
                # which is the regression guard; no defensive branch is needed.
                assert not nested
            core.append_summary("system", "lint", StepResult.ok("lint"))
        steps = [e["step"] for e in json.loads(models.SUMMARY_PATH.read_text())]
        assert steps == ["docs", "lint"]

    def test_a_later_separate_run_still_resets(self) -> None:
        with unit_run("system"):
            core.append_summary("system", "docs", StepResult.ok("docs"))
        with unit_run("system") as outer:
            assert outer
        assert json.loads(models.SUMMARY_PATH.read_text()) == []


class NestedRunAcrossProcessesTests(unittest.TestCase):
    """A real subprocess inheriting `HARNESS_RUN_DEPTH_<UNIT>` is not treated as depth 0.

    Only a foreign pid entering `unit_run` proves `unit_run`'s reset path keeps the outer
    run's summary entries and written `.reports/<unit>/*.json`."""

    @override
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = models.REPORTS_DIR, models.SUMMARY_PATH, models.SUMMARY_SCHEMA_PATH
        models.REPORTS_DIR = self.root / ".reports"
        models.SUMMARY_PATH = models.REPORTS_DIR / "summary.json"
        models.SUMMARY_SCHEMA_PATH = self.root / "build" / "summary.schema.json"

    @override
    def tearDown(self) -> None:
        models.REPORTS_DIR, models.SUMMARY_PATH, models.SUMMARY_SCHEMA_PATH = self._orig
        self._tmp.cleanup()

    def test_a_real_child_process_entering_unit_run_cannot_clobber_the_outer(self) -> None:
        with unit_run("api") as outer:
            assert outer
            outer_result = StepResult.ok("outer-step-1")
            core.append_summary("api", "outer-step-1", outer_result)
            core.write_report("api", "outer-step-1", outer_result, extra_text="outer raw output")
            outer_json = models.REPORTS_DIR / "api" / "outer-step-1.json"
            outer_txt = models.REPORTS_DIR / "api" / "outer-step-1.txt"
            assert outer_json.exists()
            assert outer_txt.exists()

            # A real child process -- not this same process -- that inherits
            # the env var this `with` block just set and calls `unit_run`
            # itself, exactly like a self-tests subprocess would.
            child_script = "\n".join(
                (
                    "from pathlib import Path",
                    "from pystatic.harness import core, models",
                    f"models.REPORTS_DIR = Path({str(models.REPORTS_DIR)!r})",
                    "models.SUMMARY_PATH = models.REPORTS_DIR / 'summary.json'",
                    f"models.SUMMARY_SCHEMA_PATH = Path({str(models.SUMMARY_SCHEMA_PATH)!r})",
                    "with core.unit_run('api') as inner:",
                    "    assert inner is False, f'child wrongly claimed outer: {inner!r}'",
                    "    core.append_summary('api', 'child-step', core.StepResult.ok('x'))",
                )
            )
            proc = subprocess.run(
                [sys.executable, "-c", child_script],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr

            # The outer's own report files and its still-open summary entry
            # survived the child's nested `unit_run` call.
            assert outer_json.exists(), "child's unit_run reset the outer's .json"
            assert outer_txt.exists(), "child's unit_run reset the outer's .txt"
            core.append_summary("api", "outer-step-2", StepResult.ok("outer-step-2"))

        steps = [e["step"] for e in json.loads(models.SUMMARY_PATH.read_text())]
        # "child-step" never appears: the child's own append_summary is a
        # no-op too (is_outer_run False for it, same guard as write_report).
        assert steps == ["outer-step-1", "outer-step-2"]


if __name__ == "__main__":
    unittest.main()
