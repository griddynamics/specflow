"""Shared helpers of the semgrep self-tests: the kit's rule directory and a real scan."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[1]
SEMGREP_DIR = KIT_DIR / "semgrep"
FIXTURES = SEMGREP_DIR / "fixtures"
CONFIGURABLE_DIR = KIT_DIR / "configurable" / "semgrep"
CONFIGURABLE_FIXTURES = CONFIGURABLE_DIR / "fixtures"

_RULEID_LINE_RE = re.compile(r"^\s*#\s*ruleid:\s*(.+?)\s*$")
_OK_LINE_RE = re.compile(r"^\s*#\s*ok:\s*(.+?)\s*$")


def semgrep_exe() -> str:
    """Resolved via PATH: `python -m semgrep` prints a deprecation warning on stdout that
    would corrupt the `--json` report."""
    exe = shutil.which("semgrep")
    assert exe, "semgrep is not on PATH"
    return exe


def run_semgrep(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [semgrep_exe(), "--metrics=off", *args], capture_output=True, text=True, check=False
    )


def real_scan(
    config: str | Path, *targets: Path, extra: tuple[str, ...] = ()
) -> dict[str, dict[str, set[int]]]:
    """rule_id -> path -> reported lines, from one real (non-`--test`) scan, which applies
    each rule's `paths:` the way `--test` does not. A bare file name is a file of `semgrep/`;
    an absolute path (for example under `configurable/semgrep/`) is used as given."""
    proc = run_semgrep(
        "--config", str(SEMGREP_DIR / config), "--json", "--quiet", *extra, *map(str, targets)
    )
    report = json.loads(proc.stdout)
    by_rule: dict[str, dict[str, set[int]]] = {}
    for finding in report["results"]:
        # A local config's rule id comes back prefixed with its dotted config path.
        raw_id = finding["check_id"]
        rule_id = "kit." + raw_id.rsplit(".kit.", 1)[1] if ".kit." in raw_id else raw_id
        by_rule.setdefault(rule_id, {}).setdefault(finding["path"], set()).add(
            finding["start"]["line"]
        )
    return by_rule


def marker_lines(fixture: Path, rule_id: str) -> tuple[set[int], set[int]]:
    """Lines a fixture expects `rule_id` to fire on (`ruleid`) or not (`ok`): the line after
    each marker, as `semgrep --test` reads a single-line pattern."""
    ruleid_lines: set[int] = set()
    ok_lines: set[int] = set()
    lines = fixture.read_text().splitlines()
    for index, line in enumerate(lines, start=1):
        for pattern, bucket in ((_RULEID_LINE_RE, ruleid_lines), (_OK_LINE_RE, ok_lines)):
            match = pattern.match(line)
            if match and rule_id in {s.strip() for s in match.group(1).split(",")}:
                target = index + 1
                # Stacked markers (`# ruleid: a` above `# ok: b`) all describe the first code line.
                while target <= len(lines) and lines[target - 1].lstrip().startswith("#"):
                    target += 1
                bucket.add(target)
    return ruleid_lines, ok_lines


def assert_scoping(by_rule: dict[str, dict[str, set[int]]], rule_id: str, *fixtures: Path) -> None:
    reported = by_rule.get(rule_id, {})
    for fixture in fixtures:
        expected_ruleid, expected_ok = marker_lines(fixture, rule_id)
        actual = reported.get(str(fixture), set())
        assert expected_ruleid <= actual, (
            f"{rule_id} did not fire on {fixture}'s ruleid lines {expected_ruleid - actual}"
        )
        assert not (expected_ok & actual), (
            f"{rule_id} unexpectedly fired on {fixture}'s ok lines {expected_ok & actual}"
        )
