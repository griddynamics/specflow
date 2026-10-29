"""Self-test for `sqlcheck.sql_rules`."""

from __future__ import annotations

from pathlib import Path

from sqlcheck import sql_rules as rule
from sqlcheck.config import Config
from sqlcheck.schema import SqlSchema, load_schema
from tests.support import EXAMPLE_SCHEMA, FIXTURES

SQL_FIXTURES = FIXTURES / "sql_rules"
SCHEMA = load_schema(SQL_FIXTURES / "schema.json")


def _rules(tmp_path: Path, sql: str, schema: SqlSchema = SCHEMA) -> set[str]:
    path = tmp_path / "x.sql"
    path.write_text(sql, encoding="utf-8")
    return {f.rule for f in rule.check_file(path, schema)}


def test_positive_fixture_is_flagged() -> None:
    findings = rule.check_file(SQL_FIXTURES / "positive.sql", SCHEMA)
    assert {f.rule for f in findings} == {
        "sql-rules-no-where",
        "sql-rules-delete-not-allowlisted",
        "sql-rules-append-only-table",
        "sql-rules-drop-outside-migration",
        "sql-rules-unknown-table",
        "sql-rules-unknown-column",
        "sql-rules-unknown-qualifier",
        "sql-rules-noqa-comment",
    }


def test_negative_fixture_is_clean() -> None:
    assert rule.check_file(SQL_FIXTURES / "negative.sql", SCHEMA) == []


def test_drop_inside_the_migration_sql_dir_is_allowed() -> None:
    path = SQL_FIXTURES / "alembic" / "sql" / "0001_drop_allowed.sql"
    findings = rule.check_file(path, SCHEMA)
    assert not any(f.rule == "sql-rules-drop-outside-migration" for f in findings)


def test_migration_dirs_are_configurable(tmp_path: Path) -> None:
    target = tmp_path / "db" / "migrations" / "0001.sql"
    target.parent.mkdir(parents=True)
    target.write_text("DROP TABLE legacy_role;\n", encoding="utf-8")
    default = rule.check_file(target, SCHEMA)
    assert "sql-rules-drop-outside-migration" in {f.rule for f in default}
    custom = rule.check_file(target, SCHEMA, migration_dirs=("db/migrations",))
    assert "sql-rules-drop-outside-migration" not in {f.rule for f in custom}


def test_a_dir_merely_containing_both_names_is_not_a_migration_dir() -> None:
    nested = Path("alembic/sql/extra/x.sql")
    assert not rule.in_migration_dir(nested, ("alembic/sql",))
    assert rule.in_migration_dir(Path("alembic/sql/x.sql"), ("alembic/sql",))


def test_delete_without_where_on_allowlisted_table_still_flags_no_where(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "DELETE FROM password_reset;\n")
    assert "sql-rules-no-where" in rules
    assert "sql-rules-delete-not-allowlisted" not in rules


def test_unqualified_column_is_not_flagged(tmp_path: Path) -> None:
    sql = "SELECT made_up_name FROM customer_order WHERE status = 'OPEN';\n"
    assert "sql-rules-unknown-column" not in _rules(tmp_path, sql)


def test_alias_qualified_unknown_column_is_flagged(tmp_path: Path) -> None:
    sql = "SELECT o.made_up_column FROM customer_order AS o WHERE o.status = 'OPEN';\n"
    rules = _rules(tmp_path, sql)
    assert "sql-rules-unknown-column" in rules
    assert "sql-rules-unknown-table" not in rules


def test_alias_qualified_known_column_is_clean(tmp_path: Path) -> None:
    sql = "SELECT o.status FROM customer_order AS o WHERE o.status = 'OPEN';\n"
    assert _rules(tmp_path, sql) == set()


def test_unresolvable_qualifier_is_flagged(tmp_path: Path) -> None:
    sql = "SELECT z.mystery FROM customer_order AS o WHERE o.status = 'OPEN';\n"
    assert _rules(tmp_path, sql) == {"sql-rules-unknown-qualifier"}


def test_qualifier_on_an_unknown_table_is_reported_once(tmp_path: Path) -> None:
    sql = "SELECT x.id FROM nope_table AS x WHERE x.id = 1;\n"
    assert _rules(tmp_path, sql) == {"sql-rules-unknown-table"}


def test_cte_names_are_known(tmp_path: Path) -> None:
    sql = (
        "WITH open_orders AS (SELECT o.id FROM customer_order AS o WHERE o.status = 'OPEN')\n"
        "SELECT oo.id FROM open_orders AS oo;\n"
    )
    assert _rules(tmp_path, sql) == set()


def test_subquery_and_function_aliases_are_known(tmp_path: Path) -> None:
    sql = (
        "SELECT recent.id, u.x\n"
        "FROM (SELECT o.id FROM customer_order AS o WHERE o.status = 'OPEN') AS recent\n"
        "CROSS JOIN unnest(:ids) AS u (x);\n"
    )
    assert _rules(tmp_path, sql) == set()


def test_a_subquery_alias_does_not_borrow_an_inner_table(tmp_path: Path) -> None:
    sql = "SELECT s.made_up FROM (SELECT id AS made_up FROM customer_order) AS s;\n"
    assert _rules(tmp_path, sql) == set()


def test_an_update_target_alias_resolves_to_its_table(tmp_path: Path) -> None:
    known = "UPDATE customer_order o SET status = 'OPEN' WHERE o.status = 'NEW';\n"
    assert _rules(tmp_path, known) == set()
    unknown = "UPDATE customer_order o SET status = 'OPEN' WHERE o.made_up_column = 1;\n"
    assert _rules(tmp_path, unknown) == {"sql-rules-unknown-column"}


def test_update_with_where_only_in_a_subquery_still_flags_no_where(tmp_path: Path) -> None:
    sql = "UPDATE cache_entry SET payload = (SELECT payload FROM audit_log WHERE id = 1);\n"
    assert "sql-rules-no-where" in _rules(tmp_path, sql)


def test_delete_with_where_containing_a_subquery_is_not_flagged(tmp_path: Path) -> None:
    sql = "DELETE FROM cache_entry WHERE key IN (SELECT payload FROM audit_log WHERE ts < 0);\n"
    assert "sql-rules-no-where" not in _rules(tmp_path, sql)


def test_unparsable_text_is_a_finding(tmp_path: Path) -> None:
    assert "sql-rules-parse-error" in _rules(tmp_path, "SELEKT FROM WHERE;\n")


def test_without_a_schema_references_are_skipped_and_deletes_fail_closed(tmp_path: Path) -> None:
    sql = "DELETE FROM cache_entry WHERE key = 'a';\nSELECT q.x FROM nowhere AS q;\n"
    assert _rules(tmp_path, sql, SqlSchema()) == {"sql-rules-delete-not-allowlisted"}


def test_the_detailed_schema_form_is_read_the_same_way(tmp_path: Path) -> None:
    from_example = load_schema(EXAMPLE_SCHEMA)
    sql = "SELECT o.status FROM customer_order AS o WHERE o.status = 'OPEN';\n"
    assert _rules(tmp_path, sql, from_example) == set()
    assert "cache_entry" in from_example.hard_delete_allowlist


def test_run_skips_when_there_are_no_sql_files(tmp_path: Path) -> None:
    result = rule.run(Config(root=tmp_path))
    assert result.exit_code == 0
    assert result.detail.startswith("skipped:")


def test_run_walks_the_configured_dirs_and_records_a_missing_schema(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.sql").write_text("DELETE FROM customer_order;\n", encoding="utf-8")
    degraded = rule.run(Config(root=tmp_path, schema=tmp_path / "absent.json"))
    assert degraded.exit_code == 1
    assert degraded.detail.startswith("degraded:")
    assert {f.path for f in degraded.findings} == {"src/bad.sql"}
    clean = rule.run(
        Config(root=tmp_path, schema=SQL_FIXTURES / "schema.json"),
        [SQL_FIXTURES / "negative.sql"],
    )
    assert clean.exit_code == 0
    assert clean.detail == ""
