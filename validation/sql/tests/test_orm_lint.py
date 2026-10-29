"""Self-test for `sqlcheck.orm_lint`."""

from __future__ import annotations

import ast
from pathlib import Path

from sqlcheck import orm_lint as rule
from sqlcheck.config import Config, ScanRegister
from sqlcheck.schema import load_schema, parse_schema
from tests.support import EXAMPLE_SCHEMA, FIXTURES, SAMPLE_APP

ORM_FIXTURES = FIXTURES / "orm_lint"
# A repository module path, so the dotted method names read as register entries do.
_REL = "src/app/repositories/fixture.py"
_PREFIX = "app.repositories.fixture"
_EMPTY = ScanRegister()
_SCHEMA = parse_schema(
    {
        "tables": {
            "pricing_plan": {"columns": {"active": {"unique": {"where": "active"}}}},
            "warehouse": {"columns": {"code": {"unique": True}}},
            "item": {"columns": {"external_ref": {"unique": {"where": "status != 'CLOSED'"}}}},
        }
    }
)
_CTX = rule.ModelContext(
    models_package="app.models",
    not_models=frozenset({"app.models.enums"}),
    unique=rule.unique_columns(
        _SCHEMA, {"PricingPlan": "pricing_plan", "Warehouse": "warehouse", "Item": "item"}
    ),
)


def _source(name: str) -> str:
    return ORM_FIXTURES.joinpath(name).read_text(encoding="utf-8")


def _scans(source: str, register: ScanRegister = _EMPTY) -> list[str]:
    findings = rule.check_source(_REL, source, register, _CTX)
    return [f.message.split(" ", 1)[0] for f in findings]


def _reads(register: ScanRegister) -> list[rule.ScanRead]:
    return rule.scan_reads(_REL, ast.parse(_source("scan_positive.py")), register, _CTX)


# --- relationship() declares lazy --------------------------------------------


def test_lazy_positive_fixture_is_flagged() -> None:
    findings = rule.check_source("x.py", _source("positive.py"))
    assert {f.rule for f in findings} == {"orm-lint-missing-lazy", "orm-lint-disallowed-lazy"}


def test_lazy_negative_fixture_is_clean() -> None:
    assert rule.check_source("x.py", _source("negative.py")) == []


def test_non_literal_lazy_is_flagged() -> None:
    findings = rule.check_source("x.py", "relationship(lazy=some_variable)")
    assert len(findings) == 1
    assert "non-literal" in findings[0].message


def test_attribute_call_form_is_checked() -> None:
    findings = rule.check_source("x.py", "import sqlalchemy.orm as orm\norm.relationship()\n")
    assert [f.rule for f in findings] == ["orm-lint-missing-lazy"]


def test_syntax_error_returns_no_findings() -> None:
    assert rule.check_source("x.py", "def broken(:\n") == []


# --- a read of every row is registered ----------------------------------------


def test_every_read_of_every_row_is_flagged() -> None:
    """No predicate, a predicate over constants alone, an inner join, a false value on a
    column unique only among true rows, and a job reading a column of every row."""
    assert _scans(_source("scan_positive.py")) == [
        f"{_PREFIX}.Repo.every_item",
        f"{_PREFIX}.Repo.every_active_item",
        f"{_PREFIX}.Repo.categories_with_items",
        f"{_PREFIX}.Repo.inactive_plans",
        f"{_PREFIX}.NightlyReconciliationJob.run",
    ]


def test_a_bounded_keyed_or_one_row_read_is_clean() -> None:
    """A key from the parameters (directly, through a list built from them, or a
    `filter_by`), a `limit`, an aggregate, an `exists`, a unique column, a subquery, a
    statement handed to a helper or returned, and true on a column unique among true
    rows: none is flagged."""
    assert rule.check_source(_REL, _source("scan_negative.py"), _EMPTY, _CTX) == []


def test_without_a_models_package_no_read_is_judged() -> None:
    assert rule.check_source(_REL, _source("scan_positive.py"), _EMPTY) == []


def test_a_method_entry_admits_that_method_alone() -> None:
    register = ScanRegister(methods=frozenset({f"{_PREFIX}.Repo.every_item"}))
    flagged = _scans(_source("scan_positive.py"), register)
    assert f"{_PREFIX}.Repo.every_item" not in flagged
    assert f"{_PREFIX}.Repo.every_active_item" in flagged


def test_a_class_entry_admits_the_reads_of_that_class() -> None:
    register = ScanRegister(classes=frozenset({"NightlyReconciliationJob"}))
    flagged = _scans(_source("scan_positive.py"), register)
    assert f"{_PREFIX}.NightlyReconciliationJob.run" not in flagged
    assert len(flagged) == 4


def test_a_complete_list_entry_admits_that_method_alone() -> None:
    register = ScanRegister(complete_lists=frozenset({f"{_PREFIX}.Repo.every_item"}))
    flagged = _scans(_source("scan_positive.py"), register)
    assert f"{_PREFIX}.Repo.every_item" not in flagged
    assert f"{_PREFIX}.Repo.every_active_item" in flagged


def test_a_complete_list_entry_that_admits_no_read_is_stale() -> None:
    gone = "app.repositories.gone.R.list_all"
    register = ScanRegister(complete_lists=frozenset({f"{_PREFIX}.Repo.every_item", gone}))
    stale = rule.stale_entries(register, _reads(register), "sqlcheck.toml")
    assert [f.rule for f in stale] == ["orm-lint-stale-complete-list-entry"]
    assert gone in stale[0].message
    assert stale[0].path == "sqlcheck.toml"


def test_a_read_in_both_lists_is_a_defect() -> None:
    both = f"{_PREFIX}.Repo.every_item"
    register = ScanRegister(methods=frozenset({both}), complete_lists=frozenset({both}))
    findings = rule.stale_entries(register, _reads(register), "sqlcheck.toml")
    assert [f.rule for f in findings] == ["orm-lint-doubly-registered-read"]
    assert both in findings[0].message


def test_an_entry_that_admits_no_read_is_stale() -> None:
    register = ScanRegister(
        methods=frozenset({f"{_PREFIX}.Repo.every_item", "app.repositories.gone.R.m"}),
        classes=frozenset({"NoSuchJob"}),
    )
    stale = rule.stale_entries(register, _reads(register), "sqlcheck.toml")
    assert [f.rule for f in stale] == ["orm-lint-stale-scan-entry"] * 2
    assert {f.message.split("'")[1] for f in stale} == {"NoSuchJob", "app.repositories.gone.R.m"}


def test_a_column_unique_among_true_rows_is_a_key_for_true_alone() -> None:
    def seeks(text: str) -> bool:
        return rule.seeks_unique_column(ast.parse(text, mode="eval").body, _CTX.unique)

    assert seeks("PricingPlan.active.is_(True)")
    assert seeks("PricingPlan.active == True")
    assert not seeks("PricingPlan.active.is_(False)")
    assert not seeks("PricingPlan.active == flag")
    # Unique only while another column holds a value: no equality alone seeks one row.
    assert not seeks("Item.external_ref == ref")
    # Unconditionally unique: any value seeks one row.
    assert seeks("Warehouse.code == name")


def test_model_tables_are_read_off_tablename() -> None:
    tables = rule.model_tables(SAMPLE_APP)
    assert tables["CustomerOrder"] == "customer_order"
    assert tables["LedgerEntry"] == "ledger_entry"
    assert rule.model_tables(None) == {}


def test_unique_columns_come_from_the_declared_schema() -> None:
    unique = rule.unique_columns(load_schema(EXAMPLE_SCHEMA), rule.model_tables(SAMPLE_APP))
    assert unique["Customer"] == {"email": None}
    assert unique["CustomerOrder"] == {}


def test_run_walks_the_configured_root(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "models.py").write_text(_source("positive.py"), encoding="utf-8")
    result = rule.run(Config(root=tmp_path))
    assert result.exit_code == 1
    assert result.detail.startswith("degraded:")
    assert {f.path for f in result.findings} == {"src/pkg/models.py"}


def test_run_reports_stale_entries_against_the_config_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "empty.py").write_text("", encoding="utf-8")
    config = Config(
        root=tmp_path,
        source=tmp_path / "sqlcheck.toml",
        models_package="app.models",
        scan_register=ScanRegister(methods=frozenset({"app.gone.f"})),
    )
    result = rule.run(config)
    assert [(f.path, f.rule) for f in result.findings] == [
        ("sqlcheck.toml", "orm-lint-stale-scan-entry")
    ]
