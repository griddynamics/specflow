"""Committed JSON documents validate against their JSON Schema (`check-jsonschema`), repeat
no key, and optionally follow a file-name rule. Reads `[[json-schemas]]`; skipped without it.
Doc: /README.md#json-schemas"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "json-schemas"
DOC = "/README.md#json-schemas"
TOOL = "check-jsonschema"


class DuplicateKeyError(ValueError):
    """A JSON object repeats a key; `json.loads` would silently keep the last one."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            msg = f"repeated key {key!r}"
            raise DuplicateKeyError(msg)
        seen[key] = value
    return seen


def parse_strict(text: str) -> Any:
    """`json.loads` that refuses a repeated key in any object."""
    return json.loads(text, object_pairs_hook=_no_duplicates)


@dataclass(frozen=True)
class Entry:
    schema: Path
    globs: tuple[str, ...]
    name_field: str | None = None

    def targets(self, root: Path) -> list[Path]:
        return sorted(
            {p for g in self.globs for p in root.glob(g) if p.is_file() and p != self.schema}
        )


def _finding(path: Path, rule: str, message: str, fix: str) -> Finding:
    return Finding(repo_rel(path), 1, 1, rule, message, fix=fix, doc=DOC)


def semantic_findings(path: Path, name_field: str | None) -> list[Finding]:
    """Repeated keys and the file-name rule; unparseable JSON is left to the schema tool."""
    try:
        document = parse_strict(path.read_text(encoding="utf-8"))
    except DuplicateKeyError as exc:
        return [_finding(path, "json-schemas-duplicate-key", f"$: {exc}", "keep one entry per key")]
    except json.JSONDecodeError:
        return []
    if name_field is None or not isinstance(document, dict):
        return []
    value = document.get(name_field)
    if isinstance(value, str) and f"{value.lower()}.json" != path.name:
        message = f"$.{name_field}: {value!r} requires the file name {value.lower()}.json"
        return [_finding(path, "json-schemas-file-name", message, "rename the file")]
    return []


def parse_report(stdout: str) -> list[Finding]:
    """`check-jsonschema --output-format json`: `{status, errors: [...], parse_errors: [...]}`."""
    if not stdout.strip():
        return []
    report = json.loads(stdout)
    fix = "correct the document, or the schema if the contract changed"
    findings = [
        _finding(
            Path(err["filename"]),
            "json-schemas-invalid",
            f"{err.get('path', '$')}: {err['message']}",
            fix,
        )
        for err in report.get("errors", [])
    ]
    findings += [
        _finding(
            Path(err["filename"]),
            "json-schemas-unparseable",
            str(err.get("message", "the file is not valid JSON")),
            "make the document valid JSON",
        )
        for err in report.get("parse_errors", [])
    ]
    return findings


def validate(tool: str, entry: Entry, targets: list[Path]) -> list[Finding]:
    if not targets:
        return []
    if not entry.schema.is_file():
        message = f"documents exist but the schema {repo_rel(entry.schema)} does not"
        return [_finding(entry.schema, "json-schemas-missing-schema", message, "add the schema")]
    proc = subprocess.run(
        [tool, "--schemafile", str(entry.schema), "--output-format", "json", *map(str, targets)],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = parse_report(proc.stdout)
    for path in targets:
        findings += semantic_findings(path, entry.name_field)
    return findings


def entries() -> list[Entry]:
    raw = config.section(STEP)
    root = target_root()
    out: list[Entry] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("schema"), str):
            msg = "each [[json-schemas]] entry needs `schema` and `files`"
            raise config.ConfigError(msg)
        name_field = item.get("name-field")
        out.append(
            Entry(
                schema=root / item["schema"],
                globs=tuple(config.string_list(item, "files", [])),
                name_field=name_field if isinstance(name_field, str) else None,
            )
        )
    return out


def check(tool: str) -> list[Finding]:
    root = target_root()
    findings = [f for entry in entries() for f in validate(tool, entry, entry.targets(root))]
    return sorted(findings, key=lambda f: (f.path, f.rule, f.message))


def find_tool() -> str | None:
    """Locate the validator next to the running interpreter first, then on PATH."""
    return shutil.which(TOOL, path=str(Path(sys.executable).parent)) or shutil.which(TOOL)


def run() -> StepResult:
    if not config.section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [[{STEP}]] entry in the configuration")
    tool = find_tool()
    if tool is None:
        return StepResult.tool_missing(STEP, TOOL)
    return StepResult.from_findings(STEP, check(tool))


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
