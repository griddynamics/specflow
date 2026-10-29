"""The two external tools a run drives: SQLFluff over every `.sql` file and Semgrep over the
Python directories (the kit's static rules plus the rules rendered from the schema)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sqlcheck.config import Config
from sqlcheck.files import iter_files
from sqlcheck.findings import Finding, StepResult
from sqlcheck.render_semgrep import write_rules
from sqlcheck.schema import load_schema

SQLFLUFF_STEP = "sqlfluff"
SEMGREP_STEP = "semgrep"


def parse_sqlfluff(stdout: str, config: Config) -> list[Finding]:
    return [
        Finding(
            config.rel(Path(entry["filepath"])),
            int(v.get("start_line_no", 1)),
            int(v.get("start_line_pos", 1)),
            f"sqlfluff-{v.get('code', 'error')}",
            str(v.get("description", "")),
            doc="RULES.md#sqlfluff",
        )
        for entry in (json.loads(stdout) if stdout.strip() else [])
        for v in entry.get("violations", [])
    ]


def run_sqlfluff(config: Config) -> StepResult:
    files = iter_files(config.root, config.sql_dirs, ".sql")
    if not files:
        return StepResult.ok(SQLFLUFF_STEP, detail="skipped: no .sql files")
    command = [
        sys.executable,
        "-m",
        "sqlfluff",
        "lint",
        "--format",
        "json",
        "--config",
        str(config.sqlfluff_config),
        "--dialect",
        {"postgresql": "postgres"}.get(config.dialect, config.dialect),  # project: sqlfluff name
        *(str(f) for f in files),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, cwd=config.root, check=False)
    if proc.returncode not in (0, 1):
        return StepResult.tool_error(SQLFLUFF_STEP, proc.stderr.strip() or proc.stdout.strip())
    return StepResult.from_findings(SQLFLUFF_STEP, parse_sqlfluff(proc.stdout, config))


def rule_id(check_id: str) -> str:
    """Semgrep prefixes a rule loaded from a directory with that directory's dotted path;
    the kit's own ids all start with `kit.`."""
    start = check_id.rfind("kit.")
    return check_id[start:] if start >= 0 else check_id


def parse_semgrep(payload: dict[str, Any], config: Config) -> list[Finding]:
    return [
        Finding(
            config.rel(config.root / r["path"]),
            int(r["start"]["line"]),
            int(r["start"]["col"]),
            rule_id(str(r["check_id"])),
            " ".join(str(r["extra"].get("message", "")).split()),
            doc="RULES.md#semgrep",
        )
        for r in payload.get("results", [])
    ]


def _semgrep_command(config: Config, generated: Path | None) -> list[str]:
    exe = shutil.which("semgrep")
    if exe is None:
        msg = "semgrep is not on PATH (install the kit's `lint` dependency group)"
        raise FileNotFoundError(msg)
    configs = ["--config", str(config.semgrep_rules)]
    if generated is not None:
        configs += ["--config", str(generated)]
    targets = [d for d in config.python_dirs if (config.root / d).exists()]
    return [
        exe,
        "scan",
        *configs,
        "--json",
        "--metrics=off",
        "--disable-nosem",
        "--quiet",
        *targets,
    ]


def run_semgrep(config: Config) -> StepResult:
    if not any((config.root / d).exists() for d in config.python_dirs):
        return StepResult.ok(SEMGREP_STEP, detail="skipped: no [paths].python directory exists")
    schema = load_schema(config.schema)
    with tempfile.TemporaryDirectory(prefix="sqlcheck-semgrep-") as tmp:
        generated: Path | None = None
        if schema.loaded:
            out_dir = config.semgrep_generated or Path(tmp)
            generated = write_rules(schema, out_dir, config.semgrep_paths_include)
        proc = subprocess.run(
            _semgrep_command(config, generated),
            capture_output=True,
            text=True,
            cwd=config.root,
            check=False,
        )
    if proc.returncode not in (0, 1):
        return StepResult.tool_error(SEMGREP_STEP, proc.stderr.strip() or proc.stdout.strip())
    payload = json.loads(proc.stdout or "{}")
    errors = [e for e in payload.get("errors", []) if e.get("level") == "error"]
    if errors:
        return StepResult.tool_error(SEMGREP_STEP, json.dumps(errors)[:2000])
    detail = (
        ""
        if schema.loaded
        else "degraded: no schema file, so the schema-bound rules were not rendered"
    )
    return StepResult.from_findings(SEMGREP_STEP, parse_semgrep(payload, config), detail)
