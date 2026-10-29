"""Self-test for `package_data`.

Each fixture builds a real wheel (`uv build --wheel`), so hatchling's file selection is the
oracle. Fixture projects are built under `tmp_path`, never committed.
"""

from __future__ import annotations

from pathlib import Path

from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import package_data as rule

_PYPROJECT = """\
[project]
name = "package-data-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]
{extra}
"""


def _make_fixture_project(root: Path, *, extra_hatch_config: str = "") -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").touch()
    (root / "src" / "pkg" / "x.cypher").write_text("MATCH (n) RETURN n\n")
    (root / "pyproject.toml").write_text(_PYPROJECT.format(extra=extra_hatch_config))


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_expected_files_finds_cypher_and_sql_files(tmp_path: Path) -> None:
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "x.cypher").write_text("MATCH (n) RETURN n\n")
    (tmp_path / "src" / "a" / "y.sql").write_text("SELECT 1;\n")
    (tmp_path / "src" / "a" / "notes.md").write_text("not packaged data\n")

    found = {p.name for p in rule._expected_files(tmp_path)}
    assert found == {"x.cypher", "y.sql"}


def test_expected_files_is_empty_with_no_src(tmp_path: Path) -> None:
    assert rule._expected_files(tmp_path) == []


def test_complete_fixture_is_clean(tmp_path: Path) -> None:
    _make_fixture_project(tmp_path)
    findings = rule._check_at(tmp_path)
    assert findings == []


def test_missing_from_wheel_is_flagged(tmp_path: Path) -> None:
    _make_fixture_project(tmp_path, extra_hatch_config='exclude = ["*.cypher"]')
    findings = rule._check_at(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "package-data-missing-from-wheel"


def test_check_on_a_tree_without_src_runs_clean() -> None:
    result = rule.run()
    assert result.step == rule.STEP
    assert result.exit_code in (0, 1)


def test_test_support_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.TEST_SUPPORT_DOC) is True


def test_test_support_inside_the_package_is_flagged(tmp_path: Path) -> None:
    """A module named as test support ships with the code it supports, wherever it sits."""
    package = tmp_path / "src" / "core"
    (package / "fixtures").mkdir(parents=True)
    for name in ("__init__.py", "test_client.py", "conftest.py", "fixture_files.py"):
        (package / name).touch()
    (package / "fixtures" / "__init__.py").touch()
    (package / "client.py").touch()

    flagged = {Path(finding.path).name for finding in rule._test_support_at(tmp_path)}

    assert flagged == {"test_client.py", "conftest.py", "fixture_files.py", "fixtures"}
    assert {finding.rule for finding in rule._test_support_at(tmp_path)} == {
        "package-data-test-support"
    }


def test_bytecode_of_a_production_module_is_not_test_support(tmp_path: Path) -> None:
    cache = tmp_path / "src" / "core" / "__pycache__"
    cache.mkdir(parents=True)
    (tmp_path / "src" / "core" / "testimony.py").touch()
    (cache / "test_client.cpython-312.pyc").touch()

    assert rule._test_support_at(tmp_path) == []
