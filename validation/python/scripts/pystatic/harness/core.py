"""Report-contract plumbing every step runs through: fail-slow timing,
`.reports/<unit>/<step>.*`, `summary.json`, and the one `run_level` loop every
runner shares."""

from __future__ import annotations

import json
import os
import re
import sys
import time

# `models.REPORTS_DIR`/`models.SUMMARY_PATH` stay qualified (never a bare
# copy): a test monkeypatches `models.REPORTS_DIR` -- its one real home --
# and every function below picks that up dynamically.
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

from pystatic.harness import models
from pystatic.harness.models import (
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_TOOL_ERROR,
    Finding,
    StepReportModel,
    StepResult,
    SummaryEntryModel,
    write_summary_schema,
)

FINDING_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+): (?P<rule>\S+): (?P<message>.+)$"
)


def parse_findings(text: str) -> list[Finding]:
    """Every `path:line:col: rule: message` contract line -- the one such parser."""
    matches = (FINDING_RE.match(line) for line in text.splitlines())
    return [
        Finding(m["path"], int(m["line"]), int(m["col"]), m["rule"], m["message"])
        for m in matches
        if m
    ]


def status_for(result: StepResult) -> models.Status:
    """`ok | findings | skipped | tool-error`, machine-readable in the report."""
    if result.detail.startswith("skipped:"):
        return "skipped"
    if result.exit_code == EXIT_TOOL_ERROR:
        return "tool-error"
    return "findings" if result.exit_code == EXIT_FINDINGS else "ok"


def run_step(step_name: str, fn: Callable[[], StepResult]) -> StepResult:
    """Time `fn`; an uncaught exception becomes a tool-error, not a crash (fail-slow)."""
    start = time.monotonic()
    try:
        result = fn()
    except Exception as exc:  # deliberately broad: fail-slow, never a runner crash
        result = StepResult(
            step=step_name,
            exit_code=EXIT_TOOL_ERROR,
            detail=f"tool-error: {type(exc).__name__}: {exc}",
        )
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def _owner(unit: str) -> tuple[str, int]:
    """`(pid, depth)` this unit's run env var records, `("", 0)` if unset."""
    pid, _, depth = os.environ.get(f"HARNESS_RUN_DEPTH_{unit.upper()}", "").partition(":")
    return (pid, int(depth)) if pid else ("", 0)


def is_outer_run(unit: str) -> bool:
    """True with no ancestor claim, or at this process's own depth-1 level --
    false for a nested `unit_run` or an inherited-but-foreign subprocess."""
    pid, depth = _owner(unit)
    return not pid or (pid == str(os.getpid()) and depth == 1)


def write_report(unit: str, step: str, result: StepResult, extra_text: str = "") -> None:
    """`.reports/<unit>/<step>.json` (+ `.txt`/`.sarif`); a no-op unless
    `is_outer_run`, so a nested/foreign run cannot clobber the real owner's."""
    if not is_outer_run(unit):
        return
    unit_dir = models.REPORTS_DIR / unit
    unit_dir.mkdir(parents=True, exist_ok=True)
    report = StepReportModel(
        unit=unit,
        step=step,
        exit=result.exit_code,
        status=status_for(result),
        duration_ms=result.duration_ms,
        findings=result.findings,
        detail=result.detail,
    )
    (unit_dir / f"{step}.json").write_text(report.model_dump_json(indent=2) + "\n")
    for ext, text in ((".txt", extra_text or result.raw_output), (".sarif", result.sarif)):
        path = unit_dir / f"{step}{ext}"
        if text:
            path.write_text(text)
        elif path.exists():
            path.unlink()


def _read_summary_entries() -> list[dict[str, object]]:
    try:
        entries: list[dict[str, object]] = json.loads(models.SUMMARY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return entries


def reset_summary_for_unit(unit: str) -> None:
    """Drop `unit`'s entries from `summary.json`, keep siblings', regen schema."""
    models.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    entries = [e for e in _read_summary_entries() if e.get("unit") != unit]
    models.SUMMARY_PATH.write_text(json.dumps(entries, indent=2) + "\n")
    write_summary_schema()


def reset_reports_for_unit(unit: str) -> None:
    """Remove `unit`'s previous per-step report files (no stale files after a smaller run)."""
    unit_dir = models.REPORTS_DIR / unit
    if not unit_dir.exists():
        unit_dir.mkdir(parents=True, exist_ok=True)
        return
    for entry in unit_dir.iterdir():
        if entry.is_file():
            entry.unlink()


def append_summary(unit: str, step: str, result: StepResult) -> None:
    """A no-op unless `is_outer_run` -- same guard as `write_report`."""
    if not is_outer_run(unit):
        return
    models.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    entries = _read_summary_entries()
    entry = SummaryEntryModel(
        unit=unit,
        step=step,
        exit=result.exit_code,
        status=status_for(result),
        duration_ms=result.duration_ms,
        findings=len(result.findings),
    )
    entries.append(json.loads(entry.model_dump_json()))
    models.SUMMARY_PATH.write_text(json.dumps(entries, indent=2) + "\n")


@contextmanager
def unit_run(unit: str) -> Iterator[bool]:
    """Depth-guards a run (a nested run, same-process or a foreign-pid
    subprocess that inherited the env var, must not wipe the outer summary
    or reports); yields True only for the outermost, claim-free call."""
    key = f"HARNESS_RUN_DEPTH_{unit.upper()}"
    before = os.environ.get(key, "")
    pid, depth = _owner(unit)
    # Any existing claim -- ours or a foreign pid's -- means an ancestor
    # already owns this unit; only a fresh (no pid at all) claim resets.
    depth = depth if pid else 0
    os.environ[key] = f"{os.getpid()}:{depth + 1}"
    try:
        if depth == 0:
            reset_summary_for_unit(unit)
            reset_reports_for_unit(unit)
        yield depth == 0
    finally:
        if before:
            os.environ[key] = before
        else:
            del os.environ[key]


def print_result(result: StepResult) -> None:
    """Findings to stdout, one status line (always `status_for`'s word) to
    stderr. A failed result with no structured findings prints its retained
    tool output too, so the validation log contains the test identity and
    traceback instead of only `pytest exited 1`."""
    for finding in result.findings:
        sys.stdout.write(finding.render() + "\n")
    if result.exit_code != EXIT_OK and not result.findings and result.raw_output:
        sys.stdout.write(result.raw_output)
        if not result.raw_output.endswith("\n"):
            sys.stdout.write("\n")
    suffix = f" -- {result.detail}" if result.detail else ""
    sys.stderr.write(f"[{result.step}] {status_for(result)} ({result.duration_ms} ms){suffix}\n")


def aggregate_exit(results: Iterable[StepResult]) -> int:
    """Worst-of wins: 2 (tool/config error) > 1 (findings) > 0 (clean)."""
    return max((r.exit_code for r in results), default=EXIT_OK)


def run_level(
    unit: str, steps: list[str], dispatch: Callable[[str], StepResult]
) -> list[StepResult]:
    """`dispatch` (already timed, already writing its own report) supplies
    each step's result; this appends it to the summary, prints it, collects it."""
    results: list[StepResult] = []
    with unit_run(unit):
        for name in steps:
            result = dispatch(name)
            append_summary(unit, name, result)
            print_result(result)
            results.append(result)
    return results


def main(step_name: str, run_fn: Callable[[], StepResult]) -> int:
    """The shared `__main__` block for one check module run directly."""
    result = run_step(step_name, run_fn)
    print_result(result)
    return result.exit_code
