"""The `all` entry point: the config file, the step order and the worst-step exit code."""

from __future__ import annotations

from pathlib import Path

import pytest

from cypher_checks import run_all
from cypher_checks.__main__ import main
from tests.conftest import EXAMPLES

CONFIG = EXAMPLES / "cypher-checks.toml"
_STEPS = ("lint", "parity", "no-inline-queries", "param-names", "semgrep", "cyver", "explain")


def test_the_example_config_resolves_against_its_directory() -> None:
    cfg = run_all.load_config(CONFIG)
    assert cfg.schema == EXAMPLES / "schema.yaml"
    assert cfg.queries == (EXAMPLES / "queries",)
    assert cfg.ddl == (EXAMPLES / "schema",)
    assert cfg.python is not None
    assert cfg.python.root == EXAMPLES / "app"
    assert set(cfg.python.loaders) == {"load_query"}
    assert cfg.semgrep_paths == (EXAMPLES / "app",)
    assert cfg.semgrep_rules == run_all.DEFAULT_SEMGREP_RULES
    assert cfg.semgrep_rules.is_file()


def test_the_examples_pass_every_static_step(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["all", "--config", str(CONFIG), "--static-only"]) == 0
    out = capsys.readouterr().out
    for step in _STEPS:
        assert f"== {step}\n" in out


def _config(tmp_path: Path, text: str) -> str:
    config = tmp_path / "cypher-checks.toml"
    config.write_text(text, encoding="utf-8")
    return str(config)


def test_a_finding_in_any_step_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "people.cypher").write_text(
        "// Every person.\nMATCH (p:Person)\nRETURN p.name AS name\n", encoding="utf-8"
    )
    schema = (EXAMPLES / "schema.yaml").as_posix()
    config = _config(tmp_path, f'schema = "{schema}"\nqueries = ["queries"]\n')
    assert main(["all", "--config", config, "--static-only"]) == 1
    out = capsys.readouterr().out
    assert "cypher-lint-not-anchored" in out
    assert "== parity\nskipped:" in out
    assert "== semgrep\nskipped:" in out


@pytest.mark.parametrize(
    "text",
    [
        'queries = ["queries"]\n',
        'schema = "schema.yaml"\nqueries = "not-a-list"\n',
        'schema = "schema.yaml"\n[python]\nroot = "src"\nloaders = ["not", "a", "table"]\n',
        'schema = "schema.yaml"\nlive = "not-a-table"\n',
        "schema = [unclosed\n",
    ],
)
def test_a_malformed_config_exits_two(tmp_path: Path, text: str) -> None:
    assert main(["all", "--config", _config(tmp_path, text)]) == 2


def test_a_missing_config_or_schema_exits_two(tmp_path: Path) -> None:
    assert main(["all", "--config", str(tmp_path / "absent.toml")]) == 2
    config = _config(tmp_path, 'schema = "absent.yaml"\n')
    assert main(["all", "--config", config, "--static-only"]) == 2


@pytest.mark.integration
def test_the_examples_pass_the_live_steps() -> None:
    assert main(["all", "--config", str(CONFIG)]) == 0
