"""Self-test for the runner: every quick step runs against a target tree and reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pystatic import run
from pystatic.cli import build_parser
from pystatic.harness import models


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "out"
    monkeypatch.setattr(models, "REPORTS_DIR", out / ".reports")
    monkeypatch.setattr(models, "SUMMARY_PATH", out / ".reports" / "summary.json")
    monkeypatch.setattr(models, "SUMMARY_SCHEMA_PATH", out / "build" / "summary.schema.json")
    monkeypatch.delenv(f"HARNESS_RUN_DEPTH_{run.UNIT.upper()}", raising=False)
    return out / ".reports"


def _target(root: Path, module: str) -> Path:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "service.py").write_text(module)
    (root / "pyproject.toml").write_text("[project]\nname = 'target'\n")
    return root


def test_clean_tree_exits_zero(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _target(tmp_path / "target", "async def f(x: int) -> int:\n    return x\n")
    assert run.main(["--root", str(target)]) == 0
    summary = json.loads((reports / "summary.json").read_text())
    assert [entry["step"] for entry in summary] == run.QUICK_STEPS
    err = capsys.readouterr().err
    assert "[enum-literals] skipped (" in err
    assert "[docs-bundle] skipped (" in err


def test_configured_checks_read_the_config_file(tmp_path: Path, reports: Path) -> None:
    module = "def is_open(order: object) -> bool:\n    return order.status == 'OPEN'\n"
    target = _target(tmp_path / "target", module)
    cfg = tmp_path / "settings.toml"
    cfg.write_text('[enum-literals]\nvalues = ["OPEN"]\n')
    assert run.main(["--root", str(target), "--config", str(cfg)]) == 1
    report = json.loads((reports / run.UNIT / "enum-literals.json").read_text())
    assert [f["rule"] for f in report["findings"]] == ["enum-literals-hand-typed"]


def test_a_missing_config_file_is_a_tool_error(tmp_path: Path, reports: Path) -> None:
    target = _target(tmp_path / "target", "VALUE = 1\n")
    assert run.main(["--root", str(target), "--config", str(tmp_path / "absent.toml")]) == 2
    report = json.loads((reports / run.UNIT / "enum-literals.json").read_text())
    assert report["status"] == "tool-error"


def test_finding_is_reported_and_written(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = "import pandas as pd\nasync def load(p: str) -> object:\n    return pd.read_csv(p)\n"
    target = _target(tmp_path / "target", module)
    assert run.main(["--root", str(target), "--level", "quick"]) == 1
    out = capsys.readouterr().out
    assert "src/pkg/service.py:3:12: blocking-in-async:" in out
    report = json.loads((reports / run.UNIT / "blocking-in-async.json").read_text())
    assert report["status"] == "findings"
    assert report["findings"][0]["doc"] == "/README.md#blocking-in-async"


def test_missing_pyproject_is_a_tool_error(tmp_path: Path, reports: Path) -> None:
    target = tmp_path / "bare"
    target.mkdir()
    assert run.main(["--root", str(target)]) == 2
    report = json.loads((reports / run.UNIT / "config-scoping.json").read_text())
    assert report["status"] == "tool-error"


def test_every_level_names_only_known_steps() -> None:
    known = set(run.step_table(build_parser("x").parse_args([])))
    for steps in run.LEVELS.values():
        assert set(steps) <= known
