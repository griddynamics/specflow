"""Self-test for `clones` over throwaway git repositories, with the real jscpd engine."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness import models
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import clones as rule

GIT = shutil.which("git") or "git"
TABLE = '[clones]\npaths = ["src", "tests"]\nmin-lines = 5\nmin-tokens = 40\n'
BODY = "\n".join(f"    total = total + value_{index}" for index in range(12))
FUNCTION = f"def calculate(value_0, value_1):\n    total = 0\n{BODY}\n    return total\n"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "reports"
    monkeypatch.setattr(models, "REPORTS_DIR", out)
    return out


def _repo(root: Path, sources: dict[str, str], table: str = TABLE) -> None:
    for rel, text in sources.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run([GIT, "init", "-q"], cwd=root, check=True)
    (root / "pystatic.toml").write_text(table)
    files.configure(root)
    config.configure(root / "pystatic.toml")


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_the_engine_ships_with_the_kit() -> None:
    assert rule.ENGINE.is_file()


def test_without_the_table_the_check_is_skipped(tmp_path: Path) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION}, table="")
    result = rule.run()
    assert (result.exit_code, result.detail) == (0, "skipped: no [clones] table")


def test_a_copied_function_is_a_finding_with_reports(tmp_path: Path, reports: Path) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION, "src/b.py": FUNCTION, "docs/c.py": FUNCTION})
    (tmp_path / "pystatic.toml").write_text(TABLE + 'reporters = ["json", "sarif"]\n')
    result = rule.run()
    assert result.exit_code == 1
    assert [(f.path, f.line, f.rule) for f in result.findings] == [
        ("src/b.py", 1, "clones-duplicate")
    ]
    assert "duplicate src/a.py:1-" in result.findings[0].message
    assert result.detail.startswith("2 files, 1 clones, ")
    report = json.loads((reports / rule.JSON_REPORT).read_text())
    assert report["statistics"]["clones"] == 1
    assert report["statistics"]["percentage"] == 100.0
    sarif = json.loads(result.sarif)
    assert sarif["runs"][0]["results"][0]["ruleId"] == "clones-duplicate"


def test_distinct_sources_are_clean_and_write_no_sarif(tmp_path: Path, reports: Path) -> None:
    other = FUNCTION.replace("+", "*").replace("total", "product")
    _repo(tmp_path, {"src/a.py": FUNCTION, "src/b.py": other.replace("value_", "factor_")})
    result = rule.run()
    assert (result.exit_code, result.findings, result.sarif) == (0, [], "")
    assert (reports / rule.JSON_REPORT).is_file()


def test_a_copy_inside_one_file_is_a_finding(tmp_path: Path, reports: Path) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION + "\n\n" + FUNCTION.replace("calculate", "again")})
    assert [f.path for f in rule.run().findings] == ["src/a.py"]


def test_shared_test_oracles_do_not_count_but_shared_setup_does(
    tmp_path: Path, reports: Path
) -> None:
    oracles = "\n".join(
        f"    assert result.field_{index} == expected_{index}" for index in range(14)
    )
    test = f"def test_it(result):\n{oracles}\n"
    _repo(tmp_path, {"tests/test_a.py": test, "tests/test_b.py": test})
    assert rule.run().findings == []
    setup = f"def test_it(value_0, value_1):\n    total = 0\n{BODY}\n    assert total == 3\n"
    for name in ("tests/test_a.py", "tests/test_b.py"):
        (tmp_path / name).write_text(setup)
    assert [f.path for f in rule.run().findings] == ["tests/test_b.py"]


def test_shared_imports_do_not_count(tmp_path: Path, reports: Path) -> None:
    imports = "".join(f"from package.module_{index} import name_{index}\n" for index in range(20))
    _repo(tmp_path, {"src/a.py": imports + "A = 1\n", "src/b.py": imports + "B = 2\n"})
    assert rule.run().findings == []


def test_implementation_source_keeps_line_numbers() -> None:
    source = "import os\nvalue = os.sep\nassert value == '/'\n"
    production = rule.implementation_source("a.py", source, test=False)
    assert production.splitlines() == [" " * len("import os"), "value = os.sep", source[25:-1]]
    tested = rule.implementation_source("t.py", source, test=True)
    assert tested.splitlines()[1:] == ["value = os.sep", " " * len("assert value == '/'")]


def test_an_unparseable_file_names_itself() -> None:
    with pytest.raises(ValueError, match=r"broken\.py: cannot parse"):
        rule.implementation_source("broken.py", "def (:\n", test=False)


def test_test_files_follow_the_patterns() -> None:
    assert rule.is_test("tests/support.py", rule.DEFAULT_TESTS)
    assert rule.is_test("pkg/test_orders.py", rule.DEFAULT_TESTS)
    assert rule.is_test("conftest.py", rule.DEFAULT_TESTS)
    assert not rule.is_test("src/orders.py", rule.DEFAULT_TESTS)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("ignore = ['src']\n", "unknown keys: ignore"),
        ("threshold = 5\n", "`threshold` must be 0"),
        ("min-lines = 0\n", "`min-lines` must be a positive integer"),
        ("mode = 'loose'\n", "`mode` must be one of"),
        ("reporters = ['html']\n", "`reporters` takes only"),
        ("format = ''\n", "`format` must be a jscpd format name"),
    ],
)
def test_bad_tables_are_configuration_errors(tmp_path: Path, extra: str, message: str) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION}, table='[clones]\npaths = ["src"]\n' + extra)
    with pytest.raises(config.ConfigError, match=message):
        rule.run()


def test_empty_or_unmatched_paths_are_configuration_errors(tmp_path: Path) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION}, table="[clones]\npaths = []\n")
    with pytest.raises(config.ConfigError, match="non-empty `paths`"):
        rule.run()
    (tmp_path / "pystatic.toml").write_text('[clones]\npaths = ["lib"]\n')
    with pytest.raises(config.ConfigError, match="match no Python source"):
        rule.run()


def test_a_missing_engine_is_a_missing_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION})
    monkeypatch.setenv("PYSTATIC_JSCPD_ENGINE", str(tmp_path / "absent.mjs"))
    result = rule.run()
    assert result.exit_code == 2
    assert result.detail.startswith("tool-missing: jscpd engine")


def test_a_failing_engine_raises_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path, {"src/a.py": FUNCTION})
    engine = tmp_path / "failing.mjs"
    engine.write_text("process.stderr.write('engine broke'); process.exit(3);\n")
    monkeypatch.setenv("PYSTATIC_JSCPD_ENGINE", str(engine))
    with pytest.raises(RuntimeError, match="exited 3: engine broke"):
        rule.run()
