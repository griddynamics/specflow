"""Paths and helpers the self-tests share."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

KIT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = KIT_DIR / "tests" / "fixtures"
SEMGREP_DIR = KIT_DIR / "semgrep"
SAMPLE_APP = FIXTURES / "sample_app"
EXAMPLE_SCHEMA = KIT_DIR / "sql-schema.example.json"
EXAMPLE_CONFIG = KIT_DIR / "sqlcheck.example.toml"
RULES_DOC = KIT_DIR / "RULES.md"


def semgrep_exe() -> str:
    exe = shutil.which("semgrep")
    assert exe, "semgrep is not on PATH: install the kit's `lint` dependency group"
    return exe


def real_scan(configs: list[Path], *targets: Path) -> dict[str, set[str]]:
    """`{rule id: {file, ...}}` of a real `semgrep scan`, which applies `paths:` scoping
    (`semgrep --test` does not)."""
    args = [semgrep_exe(), "scan", "--json", "--metrics=off", "--quiet", "--disable-nosem"]
    for config in configs:
        args += ["--config", str(config)]
    command = [*args, *map(str, targets)]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    assert proc.returncode in (0, 1), proc.stderr
    payload: dict[str, Any] = json.loads(proc.stdout)
    found: dict[str, set[str]] = {}
    for result in payload["results"]:
        check_id = str(result["check_id"])
        rule = check_id[check_id.rfind("kit.") :]
        found.setdefault(rule, set()).add(str(Path(result["path"]).resolve()))
    return found


def markdown_anchors(path: Path) -> set[str]:
    """GitHub-style anchors of every heading in `path`."""
    anchors = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line)
        if match:
            text = match.group(1).strip().lower().replace("`", "")
            anchors.add(re.sub(r"[^a-z0-9 _-]", "", text).replace(" ", "-"))
    return anchors
