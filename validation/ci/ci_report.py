#!/usr/bin/env python3
"""Validation report for CI: record each check, merge the jobs' reports, write the GitHub summary.

    ci_report.py run   --job JOB --unit UNIT --step STEP [--cwd DIR] [--sarif F] [--junit F]
                       [--junit-substeps] -- CMD ...
    ci_report.py skip  --job JOB --unit UNIT --step STEP --reason TEXT
    ci_report.py merge [--expect JOB ...] [--plan FILE]
    ci_report.py github-summary [--max-annotations N]

Exit codes: 0 ok, 1 findings or failing tests, 2 tool or configuration error.
Reports live under $CI_REPORTS_DIR (default .reports): <job>/summary.json, <job>/<unit>/<step>.{json,txt}.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from xml.parsers import expat

EXIT_OK, EXIT_FINDINGS, EXIT_TOOL_ERROR = 0, 1, 2
STATUS_RANK = {"ok": 0, "skipped": 1, "findings": 2, "failed": 3, "tool-error": 4}
STATUS_EXIT = {"ok": 0, "skipped": 0, "findings": 1, "failed": 1, "tool-error": 2}
STATUS_ICON = {"ok": "✅", "skipped": "⏭️", "findings": "⚠️", "failed": "❌", "tool-error": "💥"}
LEVELS = {"error": "error", "warning": "warning", "note": "notice", "none": "notice"}
REPORTS = Path(os.environ.get("CI_REPORTS_DIR", ".reports"))
WORKSPACE = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd()).resolve()
MAX_LISTED = 30  # failing tests listed per step; keeps the summary far below GitHub's size cap
LOG_TAIL = 40  # lines of raw output shown for a tool error
MAX_NOTE = 300  # characters of a step's notes (skip reason, hints) shown in the checks table
SUBSTEP = " \N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "  # parent and tool in a sub-step name
EXIT_NOT_STARTED = 127  # the shell's code for a command that could not be started
# Why a runner skips a step it names only in its `== summary` table (validation/typescript/lint/run.mjs
# prints no section and no reason for a step whose command it never built).
SUMMARY_SKIP_REASONS = {
    "size-limit": "opt-in: run.mjs runs it only with --size-limit, which this command does not pass",
    "tsc": "run.mjs runs it only when the project has a tsconfig.json",
}

Row = dict[str, Any]


def _say(text: str) -> None:
    """One line of this command's own output (the tool's output is teed separately)."""
    sys.stdout.write(f"{text}\n")


# --------------------------------------------------------------------------- reading tool output


class UnsafeXMLError(ValueError):
    """An XML report declares a DTD or entities, which no test report needs (entity-expansion attacks)."""


def _parse_xml(path: Path) -> ET.Element:
    """The file's element tree, parsed by expat with every DTD and entity declaration refused."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_declaration: object) -> None:
        raise UnsafeXMLError(f"{path}: DTD and entity declarations are not accepted")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    with path.open("rb") as stream:
        parser.ParseFile(stream)
    return builder.close()


def _repo_path(reference: str, cwd: Path) -> str:
    """A SARIF URI or JUnit file attribute as a workspace-relative POSIX path."""
    if reference.startswith("file:"):
        reference = unquote(urlparse(reference).path)
    path = Path(reference)
    resolved = (path if path.is_absolute() else cwd / path).resolve()
    try:
        return resolved.relative_to(WORKSPACE).as_posix()
    except ValueError:
        return reference


def read_sarif(path: Path, cwd: Path) -> list[Row]:
    """Every unsuppressed result of every run, with its first location."""
    findings: list[Row] = []
    for run in json.loads(path.read_text(encoding="utf-8")).get("runs", []):
        base = cwd / unquote(urlparse(run.get("originalUriBaseIds", {}).get("%SRCROOT%", {}).get("uri", "")).path)
        for result in run.get("results", []):
            if result.get("suppressions"):
                continue
            location = (result.get("locations") or [{}])[0].get("physicalLocation", {})
            region = location.get("region", {})
            uri = location.get("artifactLocation", {}).get("uri", "")
            findings.append(
                {
                    "rule": result.get("ruleId", ""),
                    "level": result.get("level", "error"),
                    "message": result.get("message", {}).get("text", ""),
                    "path": _repo_path(uri, base) if uri else "",
                    "line": region.get("startLine"),
                    "column": region.get("startColumn"),
                }
            )
    return findings


def _test_cases(path: Path) -> Iterator[tuple[ET.Element, ET.Element | None]]:
    """Every test case (nested suites included) with its failure, error or skipped child, if any."""
    for case in _parse_xml(path).iter("testcase"):
        yield case, next((child for child in case if child.tag in {"failure", "error", "skipped"}), None)


def _outcome_message(outcome: ET.Element) -> str:
    text = outcome.get("message") or outcome.text or ""
    return next((part.strip() for part in text.splitlines() if part.strip()), outcome.tag)[:300]


def read_junit(path: Path, cwd: Path) -> tuple[Row, list[Row]]:
    """Counts over every test case (nested suites included) and the failing cases."""
    counts = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures: list[Row] = []
    for case, outcome in _test_cases(path):
        counts["total"] += 1
        if outcome is None:
            counts["passed"] += 1
            continue
        if outcome.tag == "skipped":
            counts["skipped"] += 1
            continue
        counts["failed" if outcome.tag == "failure" else "errors"] += 1
        line = case.get("line", "")
        failures.append(
            {
                "name": "::".join(part for part in (case.get("classname"), case.get("name")) if part),
                "message": _outcome_message(outcome),
                "path": _repo_path(case.get("file", ""), cwd) if case.get("file") else "",
                "line": int(line) if line.isdigit() else None,
            }
        )
    return counts, failures


@dataclass
class _CaseGroup:
    """The test cases of one test module: one check of a pytest-based checker such as archkit."""

    total: int = 0
    failed: int = 0
    seconds: float = 0.0
    skip_reasons: list[str] = field(default_factory=list)


def _seconds(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def junit_substeps(path: Path, parent: str) -> list[Row]:
    """One row per test module of a JUnit file, so every check of a pytest-based checker is listed."""
    groups: dict[str, _CaseGroup] = {}
    for case, outcome in _test_cases(path):
        module = (case.get("classname") or Path(case.get("file") or "tests").stem).rsplit(".", 1)[-1]
        group = groups.setdefault(module.removeprefix("test_"), _CaseGroup())
        group.total += 1
        group.seconds += _seconds(case.get("time"))
        if outcome is not None and outcome.tag == "skipped":
            group.skip_reasons.append(_outcome_message(outcome))
        elif outcome is not None:
            group.failed += 1
    rows: list[Row] = []
    for name, group in groups.items():
        reasons = "; ".join(dict.fromkeys(group.skip_reasons))
        if group.failed:  # a failing check is a finding; the pack's row and "Failing tests" list the cases
            status = "findings"
            notes = [f"{group.failed} of {group.total} failed"]
        elif len(group.skip_reasons) == group.total:
            status, notes = "skipped", []
        else:
            status, notes = "ok", []
        if reasons:
            skipped = "" if status == "skipped" else f"{len(group.skip_reasons)} of {group.total} skipped: "
            notes.append(skipped + reasons)
        rows.append(
            _row(
                "",
                "",
                f"{parent}{SUBSTEP}{name}",
                status,
                findings=group.failed,
                detail="; ".join(notes),
                duration_ms=int(group.seconds * 1000),
                child=True,
            )
        )
    return rows


# --------------------------------------------------------------------------- shared helpers


def _load_rows(path: Path) -> list[Row]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError(f"{path} is not a JSON list")
    return [row for row in rows if isinstance(row, dict)]


def _append_row(job: str, row: Row) -> None:
    path = REPORTS / job / "summary.json"
    rows = _load_rows(path) if path.is_file() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([*rows, row], indent=2) + "\n", encoding="utf-8")


def _row(job: str, unit: str, step: str, status: str, **extra: Any) -> Row:
    base: Row = {"job": job, "unit": unit, "step": step, "exit": 0, "status": status}
    return {**base, "findings": 0, "tests": None, "duration_ms": 0, "detail": "", **extra}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: STATUS_RANK.get(status, 4), default="ok")


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table_row(cells: Iterable[object]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _seconds_text(duration_ms: int) -> str:
    return f"{duration_ms / 1000:.1f} s"


# --------------------------------------------------------------------------- sharded jobs
# A test pack split over a matrix (`playwright test --shard=i/N`) reports one job per shard, named
# `<pack>-shard<i>` (`e2e-production-shard2`). The shards are ordered by number, a gap in their numbers is a
# missing report, and the test packs table adds one combined row per sharded pack.

SHARD_JOB = re.compile(r"^(?P<base>.+)-shard(?P<index>[1-9]\d*)$")
TEST_COUNTS = ("total", "passed", "failed", "errors", "skipped")
MIN_COMBINED_SHARDS = 2  # a pack that only one shard reported needs no combined row


def shard_of(job: str) -> tuple[str, int] | None:
    """`e2e-production-shard2` as ("e2e-production", 2); None for a job that is not one shard of a sharded job."""
    match = SHARD_JOB.match(job)
    return (str(match["base"]), int(match["index"])) if match else None


def job_key(job: str) -> tuple[str, int]:
    """Sort key of a job name: the shards of one job together, in numeric order (shard2 before shard10)."""
    return shard_of(job) or (job, 0)


def missing_shards(jobs: Iterable[str]) -> list[str]:
    """Shards below the highest one that reported, for each sharded job, that sent no report themselves."""
    seen: dict[str, set[int]] = {}
    for job in jobs:
        shard = shard_of(job)
        if shard:
            seen.setdefault(shard[0], set()).add(shard[1])
    return [
        f"{base}-shard{index}"
        for base, indices in sorted(seen.items())
        for index in range(1, max(indices) + 1)
        if index not in indices
    ]


def shard_totals(packs: list[Row]) -> list[Row]:
    """One row per test pack run by two or more shards: summed counts, worst status, the slowest shard's duration."""
    groups: dict[tuple[str, str, str], list[Row]] = {}
    for row in packs:
        shard = shard_of(str(row["job"]))
        if shard and row.get("tests"):
            groups.setdefault((str(row["unit"]), str(row["step"]), shard[0]), []).append(row)
    totals: list[Row] = []
    for (unit, step, base), rows in groups.items():
        if len(rows) < MIN_COMBINED_SHARDS:
            continue
        tests = {key: sum(int(row["tests"].get(key) or 0) for row in rows) for key in TEST_COUNTS}
        totals.append(
            _row(
                f"{base} ({len(rows)} shards)",
                unit,
                step,
                _worst([str(row["status"]) for row in rows]),
                tests=tests,
                duration_ms=max(int(row.get("duration_ms") or 0) for row in rows),
                shard_base=base,
            )
        )
    return totals


def _pack_key(row: Row) -> tuple[str, str, str, int, int]:
    """Packs by unit and step; the shards of one pack in numeric order, followed by their combined row."""
    if row.get("shard_base"):
        return str(row["unit"]), str(row["step"]), str(row["shard_base"]), 1, 0
    base, index = job_key(str(row["job"]))
    return str(row["unit"]), str(row["step"]), base, 0, index


# --------------------------------------------------------------------------- sub-steps of a wrapper command
# A command such as `make validate-python-lint` runs many tools. Their runners print one marker per
# tool; the markers below are split into one row per tool, with a finding count read from the
# tool's own summary line (or, failing that, from its `path:line:col: rule: message` lines).

ANSI = re.compile(r"\x1b\[[0-9;]*m")
ARROW = re.compile(r"^==> (?P<name>[\w.-]+): (?P<rest>.*)$")  # `==> ruff: cmd`, `==> sql-rules: findings`
SECTION = re.compile(r"^== (?P<name>[\w.-]+)(?:[: ].*)?$")  # `== eslint: cmd`, `== lint`
BRACKET = re.compile(  # `[tokens] findings`, `[file-size] ok (52 ms)`, `[x] skipped (0 ms) -- reason`
    r"^\[(?P<name>[\w.-]+)\] (?P<status>ok|findings|skipped|failed|error)\b(?P<rest>.*)$"
)
RUN_ALL_DONE = re.compile(r"^== all selected tracks passed$")  # validation/run-all.sh's last line, not a tool
SUMMARY_ROW = re.compile(r"^(?P<name>[\w.-]+)\s+(?P<state>pass|fail|skipped|not run)\b")
FINDING_LINE = re.compile(
    r"^(?P<path>[^:\s][^:]*):(?P<line>\d+)(?::(?P<col>\d+))?: (?P<rule>[A-Za-z0-9_./@-]+): (?P<msg>.+)$"
)
# The bracket runners' last line: `python all: 13 steps, 92 findings, exit 1` (pystatic), `8 steps, ...` (run.mjs).
RUNNER_TOTAL = re.compile(r"^(?:[\w.-]+ [\w.-]+: )?\d+ steps?, (?P<findings>\d+) findings?, exit \d+$")
DOC_ANCHOR = re.compile(r"\bdoc: /README\.md#(?P<name>[\w.-]+)\)?$")  # `(fix: ...; doc: /README.md#docs-bundle)`
KNIP_HEADING = re.compile(r"^(?P<title>[A-Z][A-Za-z ]+) \((?P<count>\d+)\)\s*$")
KNIP_HINTS = "Configuration hints"  # knip's advice about its own config: printed on a clean run, not an issue
TOOL_COUNT = {
    "ruff-format": [r"(\d+) files? would be reformatted"],
    "ruff": [r"Found (\d+) errors?"],
    "mypy": [r"Found (\d+) errors? in"],
    "semgrep": [r"(\d+) Code Findings", r"Ran \d+ rules on \d+ files: (\d+) findings"],
    "import-linter": [r"Contracts: \d+ kept, (\d+) broken"],
    "deptry": [r"Found (\d+) dependency issues?"],
    "eslint": [r"✖ (\d+) problems?"],
    "stylelint": [r"✖ (\d+) problems?"],
    "prettier": [r"Code style issues found in (\d+) files?"],
}
STATE_STATUS = {"pass": "ok", "fail": "findings", "skipped": "skipped", "error": "tool-error", "not run": "tool-error"}
ARROW_STATES = {"ok", "findings", "skipped", "failed"}
MAX_STORED_FINDINGS = 500  # structured findings kept per wrapper step (annotations are capped far lower)


@dataclass
class _Section:
    """One tool's part of a wrapper command's output."""

    name: str
    kind: str  # "arrow", "section", "bracket" or "summary" (named only in the runner's summary table)
    rest: str = ""
    body: list[str] = field(default_factory=list)
    lines: int = 0  # bracket steps: the finding lines of the whole log given to this step


def _headings(body: list[str]) -> list[tuple[str, int]]:
    return [(m["title"], int(m["count"])) for line in body if (m := KNIP_HEADING.match(line))]


def _tool_count(name: str, body: list[str]) -> int:
    """The count a tool states itself: its summary line, or knip's `Unused files (2)` headings; 0 if none."""
    text = "\n".join(body)
    for pattern in TOOL_COUNT.get(name, []):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return sum(count for title, count in _headings(body) if title != KNIP_HINTS)


def _count(name: str, body: list[str]) -> int:
    return _tool_count(name, body) or sum(1 for line in body if FINDING_LINE.match(line))


def _hints(body: list[str]) -> tuple[int, str]:
    """knip's configuration hints: how many, and their text on one line."""
    count = sum(count for title, count in _headings(body) if title == KNIP_HINTS)
    if not count:
        return 0, ""
    start = next(index for index, line in enumerate(body) if line.startswith(KNIP_HINTS))
    lines: list[str] = []
    for line in body[start + 1 :]:
        if not line.strip():
            break
        lines.append(" ".join(line.split()))
    noun = "configuration hint" if count == 1 else "configuration hints"
    return count, f"{count} {noun}: {'; '.join(lines)}" if lines else f"{count} {noun}"


def _bracket_sections(lines: list[str]) -> list[_Section]:
    """`[name] status` markers: each closes a section whose output precedes it."""
    sections: list[_Section] = []
    pending: list[str] = []
    for line in lines:
        match = BRACKET.match(line)
        if match:
            sections.append(_Section(match["name"], "bracket", f"{match['status']}{match['rest']}", pending))
            pending = []
        else:
            pending.append(line)
    return sections


def _owner(finding: re.Match[str], names: list[str]) -> str | None:
    """The step a finding line names: its `doc: /README.md#step` anchor, else the longest step its rule starts with."""
    anchor = DOC_ANCHOR.search(finding["msg"])
    if anchor and anchor["name"] in names:
        return str(anchor["name"])
    return next((name for name in names if finding["rule"].startswith(name)), None)


def _bracket_findings(sections: list[_Section], lines: list[str]) -> int:
    """Give each finding line of a bracket log to one step; how many lines no step could take.

    pystatic prints every finding after all the markers, run.mjs prints a step's findings before its marker, so
    the position of a line is only a hint: the line's own doc anchor or rule prefix decides first, then the marker
    that follows it, then the one step whose marker says `findings`.
    """
    names = sorted({section.name for section in sections}, key=len, reverse=True)
    reported = [section.name for section in sections if _bracket_status(section.rest)[0] == "findings"]

    def fallback(holder: str | None) -> str | None:
        if holder in reported or len(reported) != 1:
            return holder
        return reported[0]

    held: list[tuple[re.Match[str], str | None]] = []  # each finding line and the marker that follows it
    pending: list[re.Match[str]] = []
    for line in lines:
        marker, finding = BRACKET.match(line), FINDING_LINE.match(line)
        if finding:
            pending.append(finding)
        elif marker:
            held += [(item, str(marker["name"])) for item in pending]
            pending = []
    held += [(item, None) for item in pending]  # printed after the last marker
    counts: dict[str, int] = {}
    unowned = 0
    for item, holder in held:
        owner = _owner(item, names) or fallback(holder)
        if owner is None:
            unowned += 1
        else:
            counts[owner] = counts.get(owner, 0) + 1
    for section in sections:
        section.lines = counts.pop(section.name, 0)  # a step listed twice keeps its lines once
    return unowned


def runner_total(log: str) -> int | None:
    """The findings a bracket runner counted itself (`python all: 13 steps, 92 findings, exit 1`), if it says."""
    lines = [ANSI.sub("", line.rstrip()) for line in log.splitlines()]
    if not any(BRACKET.match(line) for line in lines):
        return None
    totals = [int(match["findings"]) for line in lines if (match := RUNNER_TOTAL.match(line))]
    return totals[-1] if totals else None


def _marker_sections(lines: list[str], parent: str) -> tuple[list[_Section], dict[str, str]]:
    """`==> name:` and `== name` markers, and the states of a trailing `== summary` table."""
    sections: list[_Section] = []
    summary: dict[str, str] = {}
    in_summary = False
    for line in lines:
        arrow, section = ARROW.match(line), SECTION.match(line)
        if arrow:
            in_summary = False
            sections.append(_Section(arrow["name"], "arrow", arrow["rest"]))
        elif RUN_ALL_DONE.match(line):
            in_summary = False
        elif section and section["name"] == "summary":
            in_summary = True
        elif section and section["name"] != parent:
            in_summary = False
            sections.append(_Section(section["name"], "section"))
        elif in_summary:
            row = SUMMARY_ROW.match(line)
            if row:
                summary[row["name"]] = STATE_STATUS[row["state"]]
        elif sections:
            sections[-1].body.append(line)
    named = {section.name for section in sections}
    sections += [_Section(name, "summary") for name in summary if name not in named]
    return sections, summary


def _bracket_status(rest: str) -> tuple[str, int, str]:
    """`ok (52 ms)`, `skipped (0 ms) -- reason`: status, duration in ms and detail."""
    word = rest.split(maxsplit=1)[0]
    duration = re.search(r"\((\d+) ms\)", rest)
    detail = rest.split("--", 1)[1].strip() if "--" in rest else ""
    return {"error": "tool-error"}.get(word, word), int(duration[1]) if duration else 0, detail


def _arrow_status(rest: str) -> tuple[str, int, str] | None:
    """`==> name: findings (3)`, `==> name: skipped ...`; None when the marker carries no state (`==> ruff: cmd`)."""
    word = rest.split(" ", 1)[0].rstrip(":")
    if word in ARROW_STATES:
        return word, 0, rest.partition(" ")[2].strip(" ()")
    if rest.startswith("skipped"):
        return "skipped", 0, rest
    return None


def _section_status(section: _Section, summary: dict[str, str], count: int) -> tuple[str, int, str]:
    """Status, duration in ms and detail of one section, from its marker, the summary or its output."""
    if section.kind == "bracket":
        return _bracket_status(section.rest)
    arrow = _arrow_status(section.rest) if section.kind == "arrow" else None
    if arrow is not None:
        return arrow
    if section.kind == "summary":
        status = summary[section.name]
        reason = SUMMARY_SKIP_REASONS.get(section.name) if status == "skipped" else None
        return status, 0, reason or "listed by the runner's summary; the step itself did not run"
    if section.name in summary:
        return summary[section.name], 0, ""
    first = next((line.strip() for line in section.body if line.strip()), "")
    if first.startswith("skipped"):
        return "skipped", 0, first
    return ("findings" if count else "ok"), 0, ""


def _substep(section: _Section, summary: dict[str, str], unowned: int, parent: str) -> Row:
    if section.kind == "bracket":
        count = section.lines or _tool_count(section.name, section.body)
    else:
        count = _count(section.name, section.body)
    status, duration_ms, detail = _section_status(section, summary, count)
    hints, hint_text = _hints(section.body)
    notes = [detail, hint_text]
    if status == "findings" and not count:
        count = hints  # knip --treat-config-hints-as-errors: the hints are the findings
    if status == "findings" and not count and detail.isdigit():
        count = int(detail)  # `==> sql-rules: findings (3)`: the marker's own count
    if status == "findings" and not count:
        count = 1  # the step says it has findings: never reported as findings with none
        notes.append(
            f"at least 1 finding: {unowned} finding lines could not be given to one step"
            if unowned
            else "at least 1 finding: the step reported findings but printed no countable finding line"
        )
    if status == "skipped":
        count = 0
    elif status == "ok" and count:
        status = "findings"  # a tool whose output holds findings is never reported clean
    return _row(
        "",
        "",
        f"{parent}{SUBSTEP}{section.name}",
        status,
        findings=count,
        detail="; ".join(part for part in notes if part),
        duration_ms=duration_ms,
        child=True,
    )


def parse_substeps(log: str, parent: str) -> list[Row]:
    """One row per tool found in the output of a wrapper command; empty when it has no markers."""
    lines = [ANSI.sub("", line.rstrip()) for line in log.splitlines()]
    summary: dict[str, str] = {}
    unowned = 0
    if any(BRACKET.match(line) for line in lines):
        sections = _bracket_sections(lines)
        unowned = _bracket_findings(sections, lines)
    else:
        sections, summary = _marker_sections(lines, parent)
    return [_substep(section, summary, unowned, parent) for section in sections]


def parse_findings(log: str, prefix: str) -> list[Row]:
    """Structured `path:line:col: rule: message` lines, for annotations on the changed files."""
    found: list[Row] = []
    for line in log.splitlines():
        match = FINDING_LINE.match(ANSI.sub("", line.rstrip()))
        if match and len(found) < MAX_STORED_FINDINGS:
            path = f"{prefix.rstrip('/')}/{match['path']}" if prefix else match["path"]
            found.append(
                {
                    "rule": match["rule"],
                    "level": "warning",
                    "message": match["msg"],
                    "path": path,
                    "line": int(match["line"]),
                    "column": int(match["col"]) if match["col"] else None,
                }
            )
    return found


# --------------------------------------------------------------------------- commands


def _tee(command: list[str], cwd: Path, log_path: Path) -> tuple[int, list[str]]:
    """Run the command, copying its merged output to stdout and to the log; its exit code and problems."""
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
            )
        except OSError as exc:
            problem = f"cannot start {command[0]}: {exc}"
            log.write(problem + "\n")
            return EXIT_NOT_STARTED, [problem]
        with process:  # closes the pipe and waits for the exit
            for line in process.stdout or ():
                sys.stdout.write(line)
                log.write(line)
        return process.returncode, []


@dataclass
class _Results:
    """What the SARIF and JUnit files of one step hold."""

    findings: list[Row] = field(default_factory=list)
    failures: list[Row] = field(default_factory=list)
    tests: Row | None = None
    children: list[Row] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _read_results(args: argparse.Namespace, cwd: Path) -> _Results:
    results = _Results()
    for name in args.sarif:
        try:
            results.findings += read_sarif(cwd / name, cwd)
        except (OSError, ValueError, AttributeError) as exc:
            results.problems.append(f"SARIF {name}: {exc}")
    for name in args.junit:
        try:
            counts, failed = read_junit(cwd / name, cwd)
            if args.junit_substeps:
                results.children += junit_substeps(cwd / name, args.step)
        except (OSError, ValueError, expat.ExpatError) as exc:
            results.problems.append(f"JUnit {name}: {exc}")
            continue
        results.tests = {key: (results.tests or {}).get(key, 0) + value for key, value in counts.items()}
        results.failures += failed
    return results


def _step_status(args: argparse.Namespace, exit_code: int, results: _Results, children: list[Row]) -> str:
    tests = results.tests
    if results.problems or exit_code not in {*args.ok_codes, *args.findings_codes}:
        status = "tool-error"
    elif tests and (tests["failed"] or tests["errors"]):
        status = "failed"
    elif results.findings:
        status = "findings"
    elif exit_code not in args.ok_codes:
        status = "failed"
    else:
        status = "ok"
    worst_child = _worst([str(child["status"]) for child in children]) if children else "ok"
    if status == "failed" and STATUS_RANK[worst_child] >= STATUS_RANK["findings"]:
        status = worst_child
    elif status == "ok" and any(child["status"] == "findings" for child in children):
        status = "findings"  # a wrapper whose tools reported findings is never ok, whatever its exit code
    return status


def cmd_run(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        sys.stderr.write("run: no command given after --\n")
        return EXIT_TOOL_ERROR
    cwd = Path(args.cwd)
    stem = REPORTS / args.job / args.unit / args.step
    stem.parent.mkdir(parents=True, exist_ok=True)
    for name in [*args.sarif, *args.junit]:
        (cwd / name).unlink(missing_ok=True)
    log_path = Path(f"{stem}.txt")
    started = time.monotonic()
    exit_code, problems = _tee(command, cwd, log_path)
    duration_ms = int((time.monotonic() - started) * 1000)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    children = parse_substeps(log_text, args.step)

    results = _read_results(args, cwd)
    results.problems[:0] = problems
    status = _step_status(args, exit_code, results, children)
    findings = results.findings
    if not findings and not (results.tests and results.tests["total"]):
        findings = parse_findings(log_text, args.path_prefix)
    detail = "; ".join(results.problems) or ("" if status == "ok" else f"exit {exit_code}")

    report = _row(
        args.job,
        args.unit,
        args.step,
        status,
        exit=exit_code,
        duration_ms=duration_ms,
        detail=detail,
        tests=results.tests,
        command=shlex.join(command),
        cwd=cwd.as_posix(),
    )
    report.update(findings=findings, failures=results.failures)
    Path(f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    row = {key: value for key, value in report.items() if key not in {"findings", "failures", "command", "cwd"}}
    own = sum(int(child["findings"]) for child in children) if children else len(findings)
    total = runner_total(log_text)
    if children and total is not None:
        own = max(own, total)  # the runner's own count: every finding line it printed, as the annotations show
    every_child = [*children, *results.children]
    row.update(findings=own, children=len(every_child), report=Path(f"{stem}.json").relative_to(REPORTS).as_posix())
    _append_row(args.job, row)
    for child in every_child:
        _append_row(args.job, {**child, "job": args.job, "unit": args.unit, "parent": args.step})
    _say(f"[{status}] {args.unit} / {args.step}: exit {exit_code}, {own} findings, {duration_ms / 1000:.1f} s")
    return STATUS_EXIT[status]


def cmd_skip(args: argparse.Namespace) -> int:
    _append_row(args.job, _row(args.job, args.unit, args.step, "skipped", detail=args.reason))
    return EXIT_OK


def cmd_merge(args: argparse.Namespace) -> int:
    rows: list[Row] = []
    expected = set(args.expect)
    plan = Path(args.plan) if args.plan else REPORTS / "plan.json"
    if plan.is_file() or args.plan:
        try:
            expected |= set(json.loads(plan.read_text(encoding="utf-8"))["expected_jobs"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rows.append(_row("gate", "validation", "plan", "tool-error", exit=2, detail=f"{plan}: {exc}"))
    jobs = sorted((path.parent.name for path in REPORTS.glob("*/summary.json")), key=job_key)
    for job in jobs:
        try:
            rows += [{**row, "job": row.get("job", job)} for row in _load_rows(REPORTS / job / "summary.json")]
        except (OSError, ValueError, TypeError) as exc:
            rows.append(_row(job, "validation", "report", "tool-error", exit=2, detail=str(exc)))
    missing = sorted((expected - set(jobs)) | set(missing_shards(jobs)), key=job_key)
    if missing:
        detail = "no report from: " + ", ".join(missing)
        rows.append(
            _row("gate", "validation", "missing-reports", "tool-error", exit=2, findings=len(missing), detail=detail)
        )
    if not rows:
        rows.append(_row("gate", "validation", "reports", "tool-error", exit=2, detail=f"no reports under {REPORTS}"))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "summary.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    for row in rows:
        if row["status"] not in {"ok", "skipped"}:
            _say(f"[{row['status']}] {row['job']} / {row['unit']} / {row['step']}: {row.get('detail', '')}")
    verdict = _worst([str(row["status"]) for row in rows])
    _say(f"validation: {verdict} ({len(rows)} steps from {len(jobs)} jobs)")
    return STATUS_EXIT[verdict]


def _step_report(row: Row) -> Row:
    if not row.get("report"):
        return {}
    try:
        report: Row = json.loads((REPORTS / str(row["report"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return report


def _validated_commit() -> str:
    """Which commit the checks ran on: on a pull request, GitHub's merge of its head into the base."""
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        return ""
    head = ""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event:
        try:
            head = str(json.loads(Path(event).read_text(encoding="utf-8"))["pull_request"]["head"]["sha"])
        except (OSError, ValueError, KeyError, TypeError):
            head = ""
    merged = f", the merge of pull request head `{head[:12]}` into its base" if head and head != sha else ""
    return f"Checked commit `{sha[:12]}`{merged}. A local run also sees uncommitted changes."


def _overview_lines(tops: list[Row], verdict: str) -> list[str]:
    lines = [
        "### Overview",
        "",
        "| Job | Unit | Step | Status | Findings | Tests (passed / total) | Duration |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for row in sorted(tops, key=lambda item: (item["unit"], job_key(str(item["job"])), item["step"])):
        tests = row.get("tests")
        lines.append(
            _table_row(
                (
                    row["job"],
                    row["unit"],
                    row["step"],
                    f"{STATUS_ICON[row['status']]} {row['status']}",
                    int(row.get("findings") or 0),
                    f"{tests['passed']} / {tests['total']}" if tests else "",
                    _seconds_text(row["duration_ms"]),
                )
            )
        )
    total_tests = [row["tests"] for row in tops if row.get("tests")]
    total_findings = sum(int(row.get("findings") or 0) for row in tops)
    passed = sum(item["passed"] for item in total_tests)
    total = sum(item["total"] for item in total_tests)
    lines += [
        (
            f"| **Total** | | | {STATUS_ICON[verdict]} {verdict} | **{total_findings}** | **{passed} / {total}** "
            f"| {sum(int(row.get('duration_ms') or 0) for row in tops) / 1000:.1f} s |"
        ),
        "",
    ]
    return lines


def _pack_lines(rows: list[Row]) -> list[str]:
    packs = [row for row in rows if row.get("tests")]
    if not packs:
        return []
    lines = [
        "### Test packs",
        "",
        "| Job | Unit | Pack | Status | Total | Passed | Failed | Errors | Skipped | Duration |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sorted([*packs, *shard_totals(packs)], key=_pack_key):
        tests = row["tests"]
        lines.append(
            _table_row(
                (
                    f"**{row['job']}**" if row.get("shard_base") else row["job"],
                    row["unit"],
                    row["step"],
                    f"{STATUS_ICON[row['status']]} {row['status']}",
                    tests["total"],
                    tests["passed"],
                    tests["failed"],
                    tests["errors"],
                    tests["skipped"],
                    _seconds_text(row["duration_ms"]),
                )
            )
        )
    return [*lines, ""]


def _failing_lines(reports: list[Row]) -> list[str]:
    failing = [report for report in reports if report.get("failures")]
    if not failing:
        return []
    lines = ["### Failing tests", ""]
    for report in failing:
        lines += [f"**{_cell(report['unit'])} / {_cell(report['step'])}** ({report['job']})", ""]
        for failure in report["failures"][:MAX_LISTED]:
            where = f" — `{failure['path']}:{failure['line'] or ''}`" if failure.get("path") else ""
            lines.append(f"- `{failure['name']}`: {_cell(failure['message'])}{where}")
        if len(report["failures"]) > MAX_LISTED:
            lines.append(f"- … and {len(report['failures']) - MAX_LISTED} more (see the step's report artifact)")
        lines.append("")
    return lines


def _note(text: str) -> str:
    return text if len(text) <= MAX_NOTE else text[: MAX_NOTE - 1] + "…"


def _check_lines(rows: list[Row]) -> list[str]:
    steps: dict[tuple[str, str], Row] = {}
    for row in rows:
        if row.get("tests"):
            continue
        empty: Row = {"runs": 0, "findings": 0, "duration_ms": 0, "statuses": [], "notes": []}
        entry = steps.setdefault((row["unit"], row["step"]), empty)
        entry["runs"] += 1
        entry["findings"] += int(row.get("findings") or 0)
        entry["duration_ms"] += int(row.get("duration_ms") or 0)
        entry["statuses"].append(row["status"])
        if row.get("detail") and row["detail"] not in entry["notes"]:
            entry["notes"].append(str(row["detail"]))
    if not steps:
        return []
    lines = [
        "### Checks (every tool of every step)",
        "",
        "| Unit | Step | Runs | Status | Findings | Duration | Notes |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for (unit, step), entry in sorted(steps.items()):
        status = _worst(entry["statuses"])
        lines.append(
            _table_row(
                (
                    unit,
                    step,
                    entry["runs"],
                    f"{STATUS_ICON[status]} {status}",
                    entry["findings"],
                    _seconds_text(entry["duration_ms"]),
                    _note("; ".join(entry["notes"])),
                )
            )
        )
    return [*lines, ""]


def _error_lines(rows: list[Row]) -> list[str]:
    broken = [
        row
        for row in rows
        if row["status"] in {"tool-error", "failed"} and not row.get("tests") and not row.get("children")
    ]
    if not broken:
        return []
    lines = ["### Errors", ""]
    for row in broken:
        lines += [
            (
                f"<details><summary>{STATUS_ICON[row['status']]} {row['job']} / {row['unit']} / {row['step']}: "
                f"{_cell(row.get('detail', ''))}</summary>"
            ),
            "",
        ]
        log = REPORTS / str(row.get("report", "")).removesuffix(".json")
        try:
            tail = Path(f"{log}.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
        except OSError:
            tail = ["(no output recorded)"]
        lines += ["~~~text", *tail, "~~~", "</details>", ""]
    return lines


def _summary_lines(rows: list[Row], reports: list[Row]) -> list[str]:
    verdict = _worst([str(row["status"]) for row in rows])
    tops = [row for row in rows if not row.get("child")]
    tally = {status: sum(row["status"] == status for row in tops) for status in STATUS_RANK}
    commit = _validated_commit()
    return [
        f"## Validation {STATUS_ICON[verdict]} {verdict}",
        "",
        " · ".join(f"{count} {status}" for status, count in tally.items() if count)
        + f" · {sum(int(row.get('findings') or 0) for row in tops)} findings in total",
        "",
        *([commit, ""] if commit else []),
        *_overview_lines(tops, verdict),
        *_pack_lines(rows),
        *_failing_lines(reports),
        *_check_lines(rows),
        *_error_lines(rows),
    ]


def _escape(value: object, *, prop: bool = False) -> str:
    text = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def _annotations(rows: list[Row], reports: list[Row], limit: int) -> list[str]:
    commands: list[str] = []
    for row, report in zip(rows, reports, strict=True):
        title = f"{row['unit']} / {row['step']}"
        items = [
            (
                LEVELS.get(item.get("level", "error"), "error"),
                item,
                f"{title}: {item.get('rule', '')}",
                item.get("message", ""),
            )
            for item in report.get("findings", [])
        ]
        items += [
            ("error", item, f"{title}: {item['name']}", item.get("message", "")) for item in report.get("failures", [])
        ]
        if not items and row["status"] in {"failed", "tool-error"}:
            items = [("error", {}, title, row.get("detail") or row["status"])]
        for level, where, label, message in items:
            props = [f"title={_escape(label, prop=True)}"]
            if where.get("path"):
                props.insert(0, f"file={_escape(where['path'], prop=True)}")
                props += [
                    f"{key}={where[field]}" for key, field in (("line", "line"), ("col", "column")) if where.get(field)
                ]
            commands.append(f"::{level} {','.join(props)}::{_escape(message)}")
    return commands[:limit]


def cmd_github_summary(args: argparse.Namespace) -> int:
    try:
        rows = _load_rows(REPORTS / "summary.json")
    except (OSError, ValueError, TypeError) as exc:
        rows = [_row("gate", "validation", "reports", "tool-error", exit=2, detail=f"run `merge` first: {exc}")]
    reports = [_step_report(row) for row in rows]
    markdown = "\n".join(_summary_lines(rows, reports)) + "\n"
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(markdown)
    else:
        sys.stdout.write(markdown)
    for command in _annotations(rows, reports, args.max_annotations):
        _say(command)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci_report.py", description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="name", required=True)
    for name in ("run", "skip"):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--job", required=True, help="CI job id; include the matrix key (one shard of a pack: <pack>-shard<i>)"
        )
        sub.add_argument("--unit", required=True, help="component, e.g. api, web or .")
        sub.add_argument("--step", required=True, help="check name, e.g. ruff or pytest")
    run = commands.choices["run"]
    run.add_argument("--cwd", default=".", help="where the command runs; SARIF/JUnit paths are relative to it")
    run.add_argument("--sarif", action="append", default=[], help="SARIF file the command writes (repeatable)")
    run.add_argument("--junit", action="append", default=[], help="JUnit XML file the command writes (repeatable)")
    run.add_argument(
        "--junit-substeps",
        action="store_true",
        help="also list one row per test module of the JUnit files (a pytest-based checker such as archkit)",
    )
    run.add_argument("--path-prefix", default="", help="prepended to the paths in `path:line:col:` lines (annotations)")
    run.add_argument("--ok-codes", type=int, nargs="+", default=[0])
    run.add_argument("--findings-codes", type=int, nargs="+", default=[1])
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command")
    commands.choices["skip"].add_argument("--reason", required=True)
    merge = commands.add_parser("merge")
    merge.add_argument("--expect", nargs="*", default=[], help="jobs that must have sent a report")
    merge.add_argument("--plan", help='JSON file {"expected_jobs": [...]} (default: <reports>/plan.json if present)')
    summary = commands.add_parser("github-summary")
    summary.add_argument("--max-annotations", type=int, default=50)
    args = parser.parse_args(argv)
    handlers = {"run": cmd_run, "skip": cmd_skip, "merge": cmd_merge, "github-summary": cmd_github_summary}
    return handlers[args.name](args)


if __name__ == "__main__":
    raise SystemExit(main())
