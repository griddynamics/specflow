"""Self-test for `sqlcheck.schema_parity` and `sqlcheck.build_schema`, against the sample
models and the kit's own example schema (which must stay in step with them)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Column, Index, MetaData, String, Table, column

from sqlcheck import schema_parity as rule
from sqlcheck.build_schema import build, write
from sqlcheck.config import DEFAULT_TYPE_FAMILIES, Config
from sqlcheck.schema import load_schema, parse_schema
from tests.support import EXAMPLE_SCHEMA

METADATA_REF = "sample_app.models:Base.metadata"


def _example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE_SCHEMA.read_text(encoding="utf-8"))
    return data


def _rules(data: dict[str, Any], config: Config | None = None) -> list[str]:
    config = config or Config(root=EXAMPLE_SCHEMA.parent, schema=EXAMPLE_SCHEMA)
    findings = rule.check(rule.load_metadata(METADATA_REF), parse_schema(data), config)
    return sorted(f.rule for f in findings)


def test_the_example_schema_equals_the_sample_models() -> None:
    assert _rules(_example()) == []


def test_a_missing_table_is_reported() -> None:
    data = _example()
    del data["tables"]["cache_entry"]
    assert _rules(data) == ["schema-parity-tables"]


def test_a_missing_column_is_reported() -> None:
    data = _example()
    data["tables"]["customer"]["columns"]["nickname"] = {"type": "text"}
    assert _rules(data) == ["schema-parity-columns"]


def test_implicit_columns_are_added_to_the_declared_side() -> None:
    data = _example()
    del data["tables"]["customer"]["columns"]["id"]
    assert _rules(data) == ["schema-parity-columns"]
    # `cache_entry` has no `id`, so it is left out of this comparison altogether.
    config = Config(
        root=EXAMPLE_SCHEMA.parent,
        schema=EXAMPLE_SCHEMA,
        implicit_columns=("id",),
        ignore_tables=("alembic_version", "cache_entry"),
    )
    data["tables"]["customer_order"]["columns"].pop("id")
    assert _rules(data, config) == []


def test_a_wrong_type_family_and_an_unmapped_type_are_reported() -> None:
    data = _example()
    data["tables"]["customer"]["columns"]["disabled"]["type"] = "int"
    data["tables"]["customer"]["columns"]["email"]["type"] = "mystery"
    assert _rules(data) == ["schema-parity-type", "schema-parity-unmapped-type"]


def test_enum_values_compare_in_order() -> None:
    data = _example()
    data["tables"]["customer_order"]["columns"]["status"]["enum"] = ["PAID", "OPEN", "CLOSED"]
    assert _rules(data) == ["schema-parity-enum"]


def test_foreign_keys_compare_both_ways() -> None:
    data = _example()
    data["tables"]["customer_order"]["columns"]["customer_id"].pop("fk")
    data["tables"]["customer"]["columns"]["email"]["fk"] = "customer_order"
    assert _rules(data) == ["schema-parity-foreign-key", "schema-parity-foreign-key"]


def test_uniqueness_and_its_condition_are_compared() -> None:
    data = _example()
    data["tables"]["customer"]["columns"]["email"]["unique"] = False
    data["tables"]["customer_order"]["columns"]["external_ref"]["unique"] = {
        "where": "status = 'OPEN'"
    }
    assert _rules(data) == ["schema-parity-unique", "schema-parity-unique"]


def test_a_declared_index_needs_an_orm_index() -> None:
    data = _example()
    data["tables"]["customer"]["indexes"] = ["disabled"]
    assert _rules(data) == ["schema-parity-index"]


def test_the_plain_list_form_checks_names_only() -> None:
    data = copy.deepcopy(_example())
    data["tables"]["customer"] = ["id", "email", "disabled"]
    assert _rules(data) == []


def test_conditional_unique_compares_the_compiled_predicate_per_dialect() -> None:
    table = Table(
        "plan",
        MetaData(),
        Column("active", Boolean),
        Column("code", String),
        Index("ux_plan_code", "code", unique=True, sqlite_where=column("active") == 1),
    )
    assert rule.has_conditional_unique(table, "code", "active = 1", ("sqlite",))
    assert rule.has_conditional_unique(table, "code", "(ACTIVE  =  1)", ("sqlite",))
    assert not rule.has_conditional_unique(table, "code", "active = 0", ("sqlite",))
    assert not rule.has_conditional_unique(table, "active", "active = 1", ("sqlite",))


def test_run_skips_without_metadata_or_schema(tmp_path: Path) -> None:
    assert rule.run(Config(root=tmp_path)).detail.startswith("skipped:")
    no_schema = Config(root=tmp_path, metadata=METADATA_REF, schema=tmp_path / "absent.json")
    assert rule.run(no_schema).detail.startswith("skipped:")


def test_run_points_findings_at_the_schema_file(tmp_path: Path) -> None:
    data = _example()
    del data["tables"]["audit_log"]
    target = tmp_path / "sql-schema.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    result = rule.run(Config(root=tmp_path, schema=target, metadata=METADATA_REF))
    assert [(f.path, f.rule) for f in result.findings] == [
        ("sql-schema.json", "schema-parity-tables")
    ]


def test_build_schema_round_trips_through_parity(tmp_path: Path) -> None:
    """What `build-schema` writes from the models passes parity against those models, and
    the policy keys of an existing file are kept."""
    target = tmp_path / "sql-schema.json"
    target.write_text(json.dumps({"hard_delete_allowlist": ["cache_entry"]}), encoding="utf-8")
    metadata = rule.load_metadata(METADATA_REF)
    write(build(metadata, target, dict(DEFAULT_TYPE_FAMILIES)), target)
    built = load_schema(target)
    assert built.hard_delete_allowlist == frozenset({"cache_entry"})
    assert built.tables["customer_order"].columns["status"].type == "enum"
    assert built.tables["customer_order"].columns["customer_id"].fk_table == "customer"
    assert built.tables["customer_order"].columns["external_ref"].unique_where
    assert built.tables["customer_order"].indexes == ("status",)
    config = Config(root=tmp_path, schema=target, metadata=METADATA_REF)
    assert rule.check(metadata, built, config) == []


def test_conditional_unique_literal_case_is_data() -> None:
    table = Table(
        "ticket",
        MetaData(),
        Column("status", String),
        Column("ref", String),
        Index("ux_ticket_ref", "ref", unique=True, sqlite_where=column("status") != "CLOSED"),
    )
    assert rule.has_conditional_unique(table, "ref", "STATUS != 'CLOSED'", ("sqlite",))
    assert not rule.has_conditional_unique(table, "ref", "status != 'closed'", ("sqlite",))
    assert rule.normalise_predicate("Status != 'It''s Open'") == "status != 'It''s Open'"
