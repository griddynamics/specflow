"""Imported-module probes remain bounded when a module has many ordinary getters."""

from __future__ import annotations

import json
from pathlib import Path

from tests.semgrep_support import SEMGREP_DIR, run_semgrep


def test_large_module_probe_scan_finishes_without_skipping_rules(tmp_path: Path) -> None:
    imports = [f"import module_{index}" for index in range(200)]
    getters = [f"value_{index} = getattr(record, 'field_{index}', None)" for index in range(1200)]
    source = tmp_path / "module_probes.py"
    source.write_text(
        "\n".join([*imports, *getters, "if hasattr(module_199, 'load'):", "    pass"])
    )
    result = run_semgrep(
        "--config",
        str(SEMGREP_DIR / "imports.yml"),
        "--json",
        "--quiet",
        "--strict",
        "--timeout",
        "5",
        str(source),
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert report["errors"] == []
    matches = [row for row in report["results"] if row["check_id"].endswith("module-feature-probe")]
    assert len(matches) == 1
    assert matches[0]["start"]["line"] == len(imports) + len(getters) + 1
