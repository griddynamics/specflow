"""The `python -m cypher_checks` entry point: exit codes and dispatch, static commands only."""

from __future__ import annotations

from pathlib import Path

import pytest

from cypher_checks.__main__ import main
from tests.conftest import EXAMPLES

SCHEMA = str(EXAMPLES / "schema.yaml")


def test_lint_on_the_examples_exits_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint", "--schema", SCHEMA, str(EXAMPLES)]) == 0
    assert capsys.readouterr().out == ""


def test_lint_reports_findings_with_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    query = tmp_path / "read" / "people.cypher"
    query.parent.mkdir()
    query.write_text("// s\nMATCH (p:Person)\nRETURN p.name AS name\n", encoding="utf-8")
    argv = ["lint", "--schema", SCHEMA, "--root", str(tmp_path), str(tmp_path)]
    assert main(argv) == 1
    assert "read/people.cypher:2:1: cypher-lint-not-anchored" in capsys.readouterr().out


def test_an_unreadable_schema_exits_two(tmp_path: Path) -> None:
    assert main(["lint", "--schema", str(tmp_path / "missing.yaml"), str(tmp_path)]) == 2


def test_no_targets_is_a_reported_skip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint", "--schema", SCHEMA, str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("skipped:")


def test_no_inline_queries_is_dispatched(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text('Q = "MATCH (p:Person {id: $id}) RETURN p"\n')
    assert main(["no-inline-queries", str(tmp_path)]) == 1


def test_parity_on_the_example_ddl_exits_clean() -> None:
    assert main(["parity", "--schema", SCHEMA, str(EXAMPLES / "schema")]) == 0


def test_parity_without_ddl_files_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["parity", "--schema", SCHEMA, str(tmp_path)]) == 1
    assert "parity-no-ddl-files" in capsys.readouterr().out


def test_a_non_neo4j_image_is_a_tool_error_before_any_container(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["explain", "--image", "postgres:17", "--schema", SCHEMA, str(EXAMPLES / "queries")]
    assert main(argv) == 2
    assert "expected a 'neo4j:<tag>' image" in capsys.readouterr().err


def test_all_with_a_non_neo4j_image_exits_two(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "cypher-checks.toml"
    text = (EXAMPLES / "cypher-checks.toml").read_text(encoding="utf-8")
    text = text.replace('# image = "neo4j:', 'image = "postgres:17" # ', 1)
    config.write_text(text.replace('paths = ["app"]', "paths = []", 1), encoding="utf-8")
    for name in ("queries", "schema", "schema.yaml", "app"):
        (tmp_path / name).symlink_to(EXAMPLES / name)
    assert main(["all", "--config", str(config)]) == 2
    assert "the live checks could not run" in caplog.text
