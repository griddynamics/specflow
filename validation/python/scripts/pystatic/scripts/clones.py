"""No Python code is copied: jscpd token clones over every source under `paths`, with imports
and, in test files, independent assertion oracles left out (`pystatic.style.clone_inputs`).
Reads `[clones]`; skipped without it. Zero baseline: any clone is a finding.
Doc: /README.md#clones"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import target_root
from pystatic.harness import Finding, StepResult, models
from pystatic.harness.gitutil import glob_tracked
from pystatic.style.clone_inputs import assertion_lines, import_lines

STEP = "clones"
DOC = "/README.md#clones"
RULE = "clones-duplicate"
KEYS = frozenset(
    {"paths", "tests", "min-lines", "min-tokens", "mode", "format", "threshold", "reporters"}
)
MODES = ("strict", "mild", "weak")
REPORTERS = ("json", "sarif")
DEFAULT_TESTS = (
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
    "*/*_test.py",
    "conftest.py",
    "*/conftest.py",
    "tests/*",
    "*/tests/*",
)
# The engine is the TypeScript kit's duplicates engine (pinned @jscpd/core in its package.json).
ENGINE = Path(__file__).resolve().parents[4] / "typescript/scripts/duplicates/detect-sources.mjs"
JSON_REPORT = "clones-jscpd.json"


@dataclass(frozen=True)
class Settings:
    """The validated `[clones]` table."""

    paths: tuple[str, ...]
    tests: tuple[str, ...]
    min_lines: int
    min_tokens: int
    mode: str
    format: str
    reporters: tuple[str, ...]


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line: int
    column: int


class Fragment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    start: Location
    end: Location


class Clone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str
    tokens: int
    a: Fragment
    b: Fragment


class EngineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scanned: int
    clones: list[Clone]


def _positive(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        msg = f"[{STEP}] `{key}` must be a positive integer"
        raise config.ConfigError(msg)
    return value


def _mode(table: dict[str, Any]) -> str:
    value = table.get("mode", "weak")
    if value not in MODES:
        msg = f"[{STEP}] `mode` must be one of {', '.join(MODES)}"
        raise config.ConfigError(msg)
    return str(value)


def settings(table: dict[str, Any]) -> Settings:
    """The `[clones]` table, every key checked; an unknown key is a configuration error."""
    unknown = sorted(set(table) - KEYS)
    if unknown:
        msg = f"[{STEP}] unknown keys: {', '.join(unknown)}"
        raise config.ConfigError(msg)
    paths = config.string_list(table, "paths", [])
    if not paths:
        msg = f"[{STEP}] needs a non-empty `paths` list"
        raise config.ConfigError(msg)
    threshold = table.get("threshold", 0)
    if isinstance(threshold, bool) or threshold != 0:
        msg = f"[{STEP}] `threshold` must be 0: the kit is zero-baseline, any clone fails"
        raise config.ConfigError(msg)
    reporters = config.string_list(table, "reporters", ["json"])
    if set(reporters) - set(REPORTERS):
        msg = f"[{STEP}] `reporters` takes only {', '.join(REPORTERS)}"
        raise config.ConfigError(msg)
    fmt = table.get("format", "python")
    if not isinstance(fmt, str) or not fmt:
        msg = f"[{STEP}] `format` must be a jscpd format name"
        raise config.ConfigError(msg)
    return Settings(
        paths=tuple(paths),
        tests=tuple(config.string_list(table, "tests", list(DEFAULT_TESTS))),
        min_lines=_positive(table, "min-lines", 10),
        min_tokens=_positive(table, "min-tokens", 100),
        mode=_mode(table),
        format=fmt,
        reporters=tuple(reporters),
    )


def sources(root: Path, paths: tuple[str, ...]) -> list[str]:
    """Every tracked or untracked-but-not-ignored `.py` file under the `paths` pathspecs."""
    names = {name for spec in paths for name in glob_tracked(spec, cwd=root)}
    return sorted(name for name in names if PurePosixPath(name).suffix == ".py")


def is_test(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def implementation_source(name: str, source: str, *, test: bool) -> str:
    """`source` with imports (and, in a test file, assertion oracles) blanked, lines kept.

    A file Python cannot parse raises `ValueError` naming it (a tool error, not a clone)."""
    try:
        blank = assertion_lines(source) if test else import_lines(source)
    except SyntaxError as exc:
        msg = f"{name}: cannot parse: {exc}"
        raise ValueError(msg) from exc
    lines = source.splitlines(keepends=True)
    for number in blank:
        line = lines[number - 1]
        body = line.rstrip("\r\n")
        lines[number - 1] = " " * len(body) + line[len(body) :]
    return "".join(lines)


def _engine() -> tuple[str, Path] | str:
    """`(node, engine script)`, or the name of what is missing."""
    node = os.environ.get("PYSTATIC_NODE") or shutil.which("node")
    engine = Path(os.environ.get("PYSTATIC_JSCPD_ENGINE") or ENGINE)
    if node is None:
        return "node (Node.js 24, as in validation/typescript/scripts/package.json)"
    if not engine.is_file():
        return f"jscpd engine {engine} (the validation/typescript/scripts kit)"
    return node, engine


def detect(request: dict[str, Any], node: str, engine: Path) -> EngineResult:
    """Run the engine on `request`; a failed run raises `RuntimeError` with its stderr."""
    proc = subprocess.run(
        [node, str(engine)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        reason = proc.stderr.strip() or proc.stdout.strip()
        msg = f"clone engine exited {proc.returncode}: {reason}"
        raise RuntimeError(msg)
    return EngineResult.model_validate_json(proc.stdout)


def finding(clone: Clone) -> Finding:
    a, b = clone.a, clone.b
    return Finding(
        a.id,
        a.start.line,
        a.start.column,
        RULE,
        f"lines {a.start.line}-{a.end.line} duplicate {b.id}:{b.start.line}-{b.end.line} "
        f"({clone.tokens} tokens)",
        fix="move the shared code into one function, fixture or helper that both places use",
        doc=DOC,
    )


def statistics(texts: dict[str, str], clones: list[Clone]) -> dict[str, Any]:
    """jscpd's totals: sources, lines, clones, duplicated lines (both fragments) and percent."""
    covered: dict[str, set[int]] = {}
    for clone in clones:
        for fragment in (clone.a, clone.b):
            lines = range(fragment.start.line, fragment.end.line + 1)
            covered.setdefault(fragment.id, set()).update(lines)
    total = sum(len(text.splitlines()) for text in texts.values())
    duplicated = sum(len(lines) for lines in covered.values())
    percentage = round(100 * duplicated / total, 2) if total else 0.0
    return {
        "sources": len(texts),
        "lines": total,
        "clones": len(clones),
        "duplicatedLines": duplicated,
        "percentage": percentage,
    }


def sarif(clones: list[Clone]) -> str:
    """A SARIF 2.1.0 log: one result per clone, the copied fragment as its related location."""

    def region(fragment: Fragment) -> dict[str, Any]:
        return {
            "physicalLocation": {
                "artifactLocation": {"uri": fragment.id},
                "region": {"startLine": fragment.start.line, "endLine": fragment.end.line},
            }
        }

    results = [
        {
            "ruleId": RULE,
            "level": "error",
            "message": {"text": finding(clone).message},
            "locations": [region(clone.a)],
            "relatedLocations": [{"id": 1, **region(clone.b)}],
        }
        for clone in clones
    ]
    tool = {"driver": {"name": "pystatic-clones", "rules": [{"id": RULE}]}}
    log = {"version": "2.1.0", "runs": [{"tool": tool, "results": results}]}
    return json.dumps(log, indent=2) + "\n"


def run() -> StepResult:
    if not config.has_section(STEP):
        return StepResult.ok(STEP, f"skipped: no [{STEP}] table")
    chosen = settings(config.section(STEP))
    root = target_root()
    names = sources(root, chosen.paths)
    if not names:
        msg = f"[{STEP}] `paths` {list(chosen.paths)} match no Python source under {root}"
        raise config.ConfigError(msg)
    engine = _engine()
    if isinstance(engine, str):
        return StepResult.tool_missing(STEP, engine)
    texts = {name: (root / name).read_text(encoding="utf-8") for name in names}
    request = {
        "minLines": chosen.min_lines,
        "minTokens": chosen.min_tokens,
        "mode": chosen.mode,
        "sources": [
            {
                "id": name,
                "format": chosen.format,
                "source": implementation_source(name, text, test=is_test(name, chosen.tests)),
            }
            for name, text in texts.items()
        ],
    }
    clones = detect(request, *engine).clones
    stats = statistics(texts, clones)
    if "json" in chosen.reporters:
        models.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report = {"statistics": stats, "clones": [clone.model_dump() for clone in clones]}
        (models.REPORTS_DIR / JSON_REPORT).write_text(json.dumps(report, indent=2) + "\n")
    findings = sorted((finding(clone) for clone in clones), key=lambda f: (f.path, f.line))
    result = StepResult.from_findings(STEP, findings)
    result.detail = (
        f"{stats['sources']} files, {stats['clones']} clones, "
        f"{stats['percentage']}% duplicated lines"
    )
    result.sarif = sarif(clones) if "sarif" in chosen.reporters else ""
    return result


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
