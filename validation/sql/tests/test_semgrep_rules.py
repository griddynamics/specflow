"""Self-tests for the Semgrep rules: the static rule files and the rules rendered from the
schema pass `semgrep --test` against their fixtures, every rule has a fixture case, and
`paths:` scoping holds on a real scan (which `semgrep --test` does not apply)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sqlcheck.render_semgrep import GENERATED_NAME, render_rules, write_rules
from sqlcheck.schema import SqlSchema, load_schema
from tests.support import FIXTURES, SEMGREP_DIR, real_scan, semgrep_exe

SCOPING = SEMGREP_DIR / "fixtures" / "src"
SQL_SCHEMA = FIXTURES / "sql_rules" / "schema.json"


def _rule_ids(path: Path) -> set[str]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(r["id"]) for r in data["rules"]}


def _semgrep_test(config: Path, fixtures: Path) -> None:
    proc = subprocess.run(
        [semgrep_exe(), "--test", "--metrics=off", "--config", str(config), str(fixtures)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_static_rules_pass_semgrep_test() -> None:
    _semgrep_test(SEMGREP_DIR, SEMGREP_DIR / "fixtures")


def test_every_static_rule_has_a_ruleid_case() -> None:
    annotated = "\n".join(
        p.read_text(encoding="utf-8") for p in (SEMGREP_DIR / "fixtures").rglob("*.py")
    )
    for yml in sorted(SEMGREP_DIR.glob("*.yml")):
        for rule_id in _rule_ids(yml):
            assert re.search(rf"ruleid:.*\b{re.escape(rule_id)}\b", annotated), rule_id


def test_rendered_rules_pass_semgrep_test(tmp_path: Path) -> None:
    rules_dir, cases_dir = tmp_path / "rules", tmp_path / "cases"
    cases_dir.mkdir()
    written = write_rules(load_schema(SQL_SCHEMA), rules_dir, ("**/src/**",))
    assert written.name == GENERATED_NAME
    shutil.copy(FIXTURES / "semgrep_generated" / "sql_schema.py", cases_dir / "sql_schema.py")
    _semgrep_test(rules_dir, cases_dir)


def test_an_empty_allowlist_flags_every_delete_and_no_append_only_rule_is_rendered() -> None:
    rules = render_rules(SqlSchema(loaded=True), ("**/src/**",))["rules"]
    assert [r["id"] for r in rules] == ["kit.sql.delete-outside-allowlist"]
    assert not any(
        step.get("metavariable-regex", {}).get("metavariable") == "$OBJ"
        for step in rules[0]["patterns"]
    )


def test_the_allowlist_alternation_names_every_table_with_and_without_underscores() -> None:
    rules = render_rules(load_schema(SQL_SCHEMA), ("**/src/**",))["rules"]
    regex = next(
        step["metavariable-regex"]["regex"]
        for step in rules[0]["patterns"]
        if step.get("metavariable-regex", {}).get("metavariable") == "$OBJ"
    )
    for token in ("cache_entry", "cacheentry", "password_reset", "passwordreset"):
        assert token in regex


def test_sql_commit_and_engine_paths_scoping_via_real_scan() -> None:
    service = SCOPING / "services" / "sql_ok.py"
    database = SCOPING / "database" / "engine.py"
    repository = SCOPING / "repositories" / "store_work_scoping.py"
    found = real_scan([SEMGREP_DIR / "sql.yml"], service, database, repository)
    assert found.get("kit.sql.commit-outside-service") == {str(repository.resolve())}
    assert "kit.sql.self-opened-engine-outside-database-module" not in found


def test_example_layout_engine_factory_is_not_flagged(tmp_path: Path) -> None:
    factory = tmp_path / "src" / "app" / "database.py"
    factory.parent.mkdir(parents=True)
    factory.write_text(
        "def build_engine(url):\n    return create_async_engine(url)\n\n\n"
        "def build_session(engine):\n    return async_sessionmaker(engine)\n",
        encoding="utf-8",
    )
    found = real_scan([SEMGREP_DIR / "sql.yml"], factory)
    assert "kit.sql.self-opened-engine-outside-database-module" not in found


def test_store_work_rules_judge_each_layer_once_via_real_scan() -> None:
    """A repository's finding is the repository rules', a service's the caller rules',
    never both; the one vector module is outside the hand-rolled vector maths rule."""
    repository = SCOPING / "repositories" / "store_work_scoping.py"
    service = SCOPING / "services" / "store_work_scoping.py"
    vector_module = SCOPING / "services" / "vector_similarity.py"
    found = real_scan([SEMGREP_DIR / "store_work.yml"], repository, service, vector_module)
    for rule_id in (
        "kit.store_work.repository-result-shaped-in-python",
        "kit.store_work.repository-result-sliced",
    ):
        assert found.get(rule_id) == {str(repository.resolve())}, rule_id
    for rule_id in (
        "kit.store_work.caller-shapes-repository-read",
        "kit.store_work.caller-slices-repository-read",
    ):
        assert found.get(rule_id) == {str(service.resolve())}, rule_id
    assert found.get("kit.store_work.hand-rolled-vector-maths") == {str(service.resolve())}
