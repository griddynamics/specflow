"""Self-test for the `sqlcheck` entry point and its configuration file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlcheck import cli, inline_queries, orm_lint
from sqlcheck.config import ALL_STEPS, KIT_SQLFLUFF, load_config
from sqlcheck.findings import EXIT_FINDINGS, EXIT_OK, EXIT_TOOL_ERROR
from tests.support import EXAMPLE_CONFIG, FIXTURES, KIT_DIR, RULES_DOC, markdown_anchors


def _project(tmp_path: Path, sql: str, steps: list[str]) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "query.sql").write_text(sql, encoding="utf-8")
    config = tmp_path / "sqlcheck.toml"
    config.write_text(
        f"[run]\nsteps = {json.dumps(steps)}\n"
        f'[paths]\nschema = "{(FIXTURES / "sql_rules" / "schema.json").as_posix()}"\n',
        encoding="utf-8",
    )
    return config


DYNAMIC_STEPS = ("schema-parity", "migrations", "sqlite-pragmas")


def test_the_example_config_lists_the_static_steps() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert config.steps == tuple(s for s in ALL_STEPS if s not in DYNAMIC_STEPS)
    assert config.models_package == "app.models"
    assert config.expected_pragmas["journal_mode"] == "wal"
    assert config.root == KIT_DIR


def test_no_config_means_every_default(tmp_path: Path) -> None:
    config = load_config(None, tmp_path)
    assert config.root == tmp_path.resolve()
    assert config.sqlfluff_config == KIT_SQLFLUFF
    assert config.schema is None


def test_an_unknown_step_is_a_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "sqlcheck.toml"
    path.write_text('[run]\nsteps = ["nope"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown steps"):
        load_config(path)


def test_a_broken_config_exits_with_the_tool_error_code(tmp_path: Path) -> None:
    path = tmp_path / "sqlcheck.toml"
    path.write_text('[run]\nsteps = ["nope"]\n', encoding="utf-8")
    assert cli.main(["run", "--config", str(path)]) == EXIT_TOOL_ERROR


def test_run_exits_one_on_findings_and_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _project(tmp_path, "DELETE FROM customer_order;\n", ["sql-rules"])
    exit_code = cli.main(["run", "--config", str(config), "--format", "json"])
    assert exit_code == EXIT_FINDINGS
    (step,) = json.loads(capsys.readouterr().out)
    assert step["step"] == "sql-rules"
    assert {f["rule"] for f in step["findings"]} == {
        "sql-rules-no-where",
        "sql-rules-delete-not-allowlisted",
    }


def test_run_exits_zero_on_clean_sql(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sql = "SELECT o.status FROM customer_order AS o WHERE o.status = 'OPEN';\n"
    config = _project(tmp_path, sql, ["sql-rules", "alembic-lint", "orm-lint"])
    assert cli.main(["run", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "==> sql-rules: ok" in out
    assert "==> alembic-lint: skipped" in out


def test_sqlfluff_step_reports_layout_findings(tmp_path: Path) -> None:
    config = _project(tmp_path, "select  1\n", ["sqlfluff"])
    assert cli.main(["run", "--config", str(config)]) == EXIT_FINDINGS


def test_a_crashing_step_is_a_tool_error_and_the_rest_still_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _project(tmp_path, "SELECT 1;\n", ["schema-parity", "sql-rules"])
    config.write_text(
        config.read_text(encoding="utf-8") + '[schema_parity]\nmetadata = "no_such_mod:x"\n',
        encoding="utf-8",
    )
    assert cli.main(["run", "--config", str(config)]) == EXIT_TOOL_ERROR
    out = capsys.readouterr().out
    assert "==> schema-parity: tool-error" in out
    assert "==> sql-rules:" in out


def test_build_schema_and_render_semgrep_write_their_files(tmp_path: Path) -> None:
    out = tmp_path / "sql-schema.json"
    metadata = "sample_app.ledger:Base.metadata"
    assert cli.main(["build-schema", "--metadata", metadata, "--out", str(out)]) == EXIT_OK
    assert "ledger_entry" in json.loads(out.read_text(encoding="utf-8"))["tables"]
    config = tmp_path / "sqlcheck.toml"
    config.write_text('[paths]\nschema = "sql-schema.json"\n', encoding="utf-8")
    rules_dir = tmp_path / "rules"
    assert cli.main(["render-semgrep", "--config", str(config), "--out", str(rules_dir)]) == 0
    assert (rules_dir / "sql_schema.yml").is_file()


def test_build_schema_needs_exactly_one_source(tmp_path: Path) -> None:
    assert cli.main(["build-schema", "--out", str(tmp_path / "x.json")]) == EXIT_TOOL_ERROR


def test_every_step_and_the_semgrep_section_have_a_rules_doc_heading() -> None:
    anchors = markdown_anchors(RULES_DOC)
    for step in (*ALL_STEPS, "semgrep"):
        assert step in anchors, step


def test_orm_lint_and_inline_queries_are_skipped_without_python_dirs(tmp_path: Path) -> None:
    config = tmp_path / "sqlcheck.toml"
    config.write_text('[run]\nsteps = ["orm-lint", "inline-queries"]\n', encoding="utf-8")
    loaded = load_config(config)
    for step in (orm_lint, inline_queries):
        result = step.run(loaded)
        assert result.detail.startswith("skipped:"), step.__name__


def test_the_example_project_passes_every_static_step_with_none_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = FIXTURES / "example_project" / "sqlcheck.toml"
    assert cli.main(["run", "--config", str(config), "--format", "json"]) == EXIT_OK
    steps = json.loads(capsys.readouterr().out)
    assert [s["step"] for s in steps] == list(load_config(config).steps)
    for step in steps:
        assert not step["findings"], step
        assert not str(step.get("detail", "")).startswith("skipped"), step
