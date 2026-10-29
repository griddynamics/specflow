"""Tests read documented runtime defaults from one generated file instead of hard-coding
them. Reads `[runtime-defaults]` (`defaults` JSON, `scan`, `suffixes`); skipped without it.
Doc: /README.md#runtime-defaults"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import iter_files, repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "runtime-defaults"
DOC = "/README.md#runtime-defaults"
IGNORED_VALUES = frozenset({0, 1})
WINDOW_LINES = 5
_LITERAL_RE = re.compile(r"(?<![\w.$#-])\d[\d_]*(?:\.\d+)?(?![\w]|\.\d)")
_COMMENT_RE = re.compile(r"^\s*(?:#|//|\*|/\*)")


def names(key: str) -> list[str]:
    """`MAX_ITEMS` -> `MAX_ITEMS`, `max_items`, `maxItems`."""
    head, *tail = key.lower().split("_")
    return [key, key.lower(), head + "".join(part.capitalize() for part in tail)]


def name_pattern(key: str) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(alias) for alias in names(key))
    return re.compile(rf"(?<![\w])(?:{alternatives})(?![\w])")


def _sets_the_setting(line: str, aliases: list[str], start: int) -> bool:
    """The literal is the value a test gives the setting under its quoted name."""
    before = line[:start]
    return any(re.search(rf"[\"'`]{re.escape(n)}[\"'`]\W{{0,6}}$", before) for n in aliases)


def _number(token: str) -> int | float:
    plain = token.replace("_", "")
    return float(plain) if "." in plain else int(plain)


def scan_text(
    rel: str, text: str, defaults: dict[str, int | float], defaults_rel: str = "the defaults file"
) -> list[Finding]:
    patterns = {key: name_pattern(key) for key in defaults}
    lines = text.splitlines()
    findings: list[Finding] = []
    in_docstring = False
    for index, line in enumerate(lines):
        opens = line.count('"""') % 2 == 1
        prose = in_docstring or line.lstrip().startswith('"""')
        in_docstring ^= opens
        if prose or _COMMENT_RE.match(line):
            continue
        for match in _LITERAL_RE.finditer(line):
            value = _number(match.group(0))
            if value in IGNORED_VALUES:
                continue
            nearby = "\n".join(lines[max(0, index - WINDOW_LINES) : index + WINDOW_LINES + 1])
            findings += [
                Finding(
                    rel,
                    index + 1,
                    match.start() + 1,
                    "runtime-defaults-hard-coded",
                    f"hard-coded {match.group(0)} is the documented default of {setting}",
                    fix=f"read it from {defaults_rel}",
                    doc=DOC,
                )
                for setting, default in defaults.items()
                if default == value
                and patterns[setting].search(nearby)
                and not _sets_the_setting(line, names(setting), match.start())
            ]
    return findings


def load_defaults(path: Path) -> dict[str, int | float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in data.values()
    ):
        msg = f"{path} must be a JSON object of SETTING_NAME: number"
        raise config.ConfigError(msg)
    return {str(k): v for k, v in data.items()}


def check() -> list[Finding]:
    table = config.section(STEP)
    defaults_rel = table.get("defaults")
    if not isinstance(defaults_rel, str):
        msg = "[runtime-defaults] needs `defaults = 'path/to/defaults.json'`"
        raise config.ConfigError(msg)
    defaults = load_defaults(target_root() / defaults_rel)
    roots = config.string_list(table, "scan", ["tests"])
    findings: list[Finding] = []
    for suffix in config.string_list(table, "suffixes", [".py", ".ts"]):
        for path in iter_files(suffix, roots):
            text = path.read_text(encoding="utf-8")
            findings += scan_text(repo_rel(path), text, defaults, defaults_rel)
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    if not config.has_section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [{STEP}] table in the configuration")
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
