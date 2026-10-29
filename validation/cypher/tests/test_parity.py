"""Self-test for `parity`: the schema file and the DDL files declare the same constraints."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from cypher_checks import parity
from cypher_checks.findings import Finding
from cypher_checks.schema import GraphSchema, parse_schema
from tests.conftest import EXAMPLES

#: The statements that match the example schema's `constraints` section exactly.
_MATCHING = (
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT genre_name IF NOT EXISTS FOR (g:Genre) REQUIRE g.name IS UNIQUE",
    "CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name)",
    "CREATE INDEX movie_title IF NOT EXISTS FOR (m:Movie) ON (m.title)",
)


def _findings(tmp_path: Path, schema: GraphSchema, statements: Sequence[str]) -> list[Finding]:
    ddl = tmp_path / "schema" / "constraints.cypher"
    ddl.parent.mkdir(parents=True, exist_ok=True)
    ddl.write_text("// constraints\n" + "".join(f"{s};\n" for s in statements), encoding="utf-8")
    return parity.check([ddl], schema, root=tmp_path)


def _rules(tmp_path: Path, schema: GraphSchema, statements: Sequence[str]) -> set[str]:
    return {f.rule for f in _findings(tmp_path, schema, statements)}


def test_the_example_ddl_matches_the_example_schema(schema: GraphSchema) -> None:
    ddl = parity.ddl_files_among([EXAMPLES / "schema" / "constraints.cypher"], schema)
    assert parity.check(ddl, schema) == []


def test_matching_ddl_is_clean(tmp_path: Path, schema: GraphSchema) -> None:
    assert _rules(tmp_path, schema, _MATCHING) == set()


def test_a_declared_constraint_without_ddl_is_missing(tmp_path: Path, schema: GraphSchema) -> None:
    statements = [s for s in _MATCHING if "genre_name" not in s]
    assert _rules(tmp_path, schema, statements) == {"parity-unique-missing-in-ddl"}


def test_a_declared_index_without_ddl_is_missing(tmp_path: Path, schema: GraphSchema) -> None:
    statements = [s for s in _MATCHING if "movie_title" not in s]
    assert _rules(tmp_path, schema, statements) == {"parity-index-missing-in-ddl"}


def test_undeclared_ddl_is_flagged(tmp_path: Path, schema: GraphSchema) -> None:
    extra = (
        "CREATE CONSTRAINT person_name IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE",
        "CREATE INDEX movie_released IF NOT EXISTS FOR (m:Movie) ON (m.released)",
    )
    assert _rules(tmp_path, schema, (*_MATCHING, *extra)) == {
        "parity-unique-undeclared",
        "parity-index-undeclared",
    }


def test_a_create_without_if_not_exists_is_not_idempotent(
    tmp_path: Path, schema: GraphSchema
) -> None:
    first = _MATCHING[0].replace(" IF NOT EXISTS", "")
    assert _rules(tmp_path, schema, (first, *_MATCHING[1:])) == {"parity-ddl-not-idempotent"}


def test_vector_and_fulltext_indexes_are_out_of_scope(tmp_path: Path, schema: GraphSchema) -> None:
    extra = (
        "CREATE VECTOR INDEX movie_plot IF NOT EXISTS FOR (m:Movie) ON (m.plot_embedding)",
        "CREATE FULLTEXT INDEX movie_text IF NOT EXISTS FOR (m:Movie) ON EACH [m.tagline]",
    )
    assert _rules(tmp_path, schema, (*_MATCHING, *extra)) == set()


def test_no_ddl_files_is_a_finding(schema: GraphSchema) -> None:
    assert [f.rule for f in parity.check([], schema)] == ["parity-no-ddl-files"]


def test_ddl_globs_select_the_ddl_files(schema: GraphSchema) -> None:
    targets = [Path("graph/schema/constraints.cypher"), Path("graph/read/movie.cypher")]
    assert parity.ddl_files_among(targets, schema) == [targets[0]]


# Composite keys with a tenant scope key, and relationship patterns

#: A tenant-scoped schema: every key is composite with `tenant` (`policy.merge_scope_keys`).
_SCOPED = parse_schema(
    {
        "common_properties": {"tenant": {"type": "string"}},
        "nodes": {
            "Item": {"properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
            "Part": {"properties": {"id": {"type": "string"}}},
        },
        "relationships": {
            "LINKS": {"from": ["Item"], "to": ["Item"], "properties": ["tenant", "key"]},
            "USES": {"from": ["Item"], "to": ["Part"], "properties": ["tenant", "key"]},
        },
        "constraints": {
            "unique": ["Item.id", "Part.id"],
            "indexes": ["Item.code", "LINKS.key", "USES.key"],
        },
        "policy": {"merge_scope_keys": ["tenant"]},
    }
)
_SCOPED_DDL = (
    "CREATE CONSTRAINT item_id IF NOT EXISTS FOR (n:Item) REQUIRE (n.tenant, n.id) IS UNIQUE",
    "CREATE CONSTRAINT part_id IF NOT EXISTS FOR (p:Part) REQUIRE (p.tenant, p.id) IS UNIQUE",
    "CREATE INDEX item_code IF NOT EXISTS FOR (n:Item) ON (n.tenant, n.code)",
    "CREATE INDEX links_key IF NOT EXISTS FOR ()-[r:LINKS]-() ON (r.tenant, r.key)",
    "CREATE RANGE INDEX uses_key IF NOT EXISTS\n  FOR ()-[r:USES]->() ON (r.tenant, r.key)",
    "CREATE VECTOR INDEX item_vec IF NOT EXISTS FOR (n:Item) ON (n.vec) "
    "OPTIONS {indexConfig: {`vector.dimensions`: $dimension}}",
    "CREATE FULLTEXT INDEX item_text IF NOT EXISTS FOR (n:Item) ON EACH [n.code, n.id]",
)


def test_scoped_composite_ddl_matches_its_declared_properties(tmp_path: Path) -> None:
    assert _rules(tmp_path, _SCOPED, _SCOPED_DDL) == set()


def test_a_declared_relationship_index_without_ddl_is_missing(tmp_path: Path) -> None:
    statements = [s for s in _SCOPED_DDL if "uses_key" not in s]
    findings = _findings(tmp_path, _SCOPED, statements)
    assert [(f.rule, f.message.split(" ")[0]) for f in findings] == [
        ("parity-index-missing-in-ddl", "USES.key")
    ]


def test_an_undeclared_relationship_index_is_flagged(tmp_path: Path) -> None:
    extra = "CREATE INDEX links_tenant IF NOT EXISTS FOR ()<-[r:LINKS]-() ON (r.tenant, r.since)"
    findings = _findings(tmp_path, _SCOPED, (*_SCOPED_DDL, extra))
    assert [(f.rule, f.message.split(" ")[0]) for f in findings] == [
        ("parity-index-undeclared", "LINKS.since")
    ]


def test_a_scoped_constraint_is_held_to_its_declared_property(tmp_path: Path) -> None:
    statements = [s for s in _SCOPED_DDL if "part_id" not in s]
    statements.append(
        "CREATE CONSTRAINT part_code IF NOT EXISTS FOR (p:Part) "
        "REQUIRE (p.tenant, p.code) IS UNIQUE"
    )
    findings = _findings(tmp_path, _SCOPED, statements)
    assert sorted((f.rule, f.message.split(" ")[0]) for f in findings) == [
        ("parity-unique-missing-in-ddl", "Part.id"),
        ("parity-unique-undeclared", "Part.code"),
    ]


def test_a_composite_of_several_declared_properties_is_out_of_scope(tmp_path: Path) -> None:
    extra = (
        "CREATE INDEX item_pair IF NOT EXISTS FOR (n:Item) ON (n.tenant, n.code, n.id)",
        (
            "CREATE CONSTRAINT item_pair_key IF NOT EXISTS FOR (n:Item) "
            "REQUIRE (n.code, n.id) IS UNIQUE"
        ),
    )
    assert _rules(tmp_path, _SCOPED, (*_SCOPED_DDL, *extra)) == set()


def test_a_key_of_the_scope_property_alone_is_that_property(tmp_path: Path) -> None:
    extra = "CREATE INDEX item_tenant IF NOT EXISTS FOR (n:Item) ON (n.tenant)"
    findings = _findings(tmp_path, _SCOPED, (*_SCOPED_DDL, extra))
    assert [(f.rule, f.message.split(" ")[0]) for f in findings] == [
        ("parity-index-undeclared", "Item.tenant")
    ]


def test_without_scope_keys_a_tenant_composite_is_out_of_scope(tmp_path: Path) -> None:
    """With no `merge_scope_keys`, `(n.tenant, n.id)` stays a two-property composite."""
    unscoped = replace(_SCOPED, policy=replace(_SCOPED.policy, merge_scope_keys=()))
    assert _rules(tmp_path, unscoped, _SCOPED_DDL) == {
        "parity-unique-missing-in-ddl",
        "parity-index-missing-in-ddl",
    }


def test_single_property_forms_in_parentheses_and_without_are_alike(
    tmp_path: Path, schema: GraphSchema
) -> None:
    statements = [
        s.replace("REQUIRE n.id IS UNIQUE", "REQUIRE (n.id) IS UNIQUE") for s in _MATCHING
    ]
    assert statements != list(_MATCHING)
    assert _rules(tmp_path, schema, statements) == set()


# Placeholder labels of templates (`policy.placeholder_labels`)


def _placeholder_schema(concrete: Sequence[str]) -> GraphSchema:
    """`Item.id` and the template label `__ANY__.uuid`, `__ANY__` standing for `concrete`."""
    return parse_schema(
        {
            "common_properties": {"tenant": {"type": "string"}},
            "nodes": {
                "Item": {"properties": {"id": {"type": "string"}}},
                "__ANY__": {"properties": {"uuid": {"type": "string"}}},
            },
            "constraints": {"unique": ["Item.id", "__ANY__.uuid"]},
            "policy": {
                "merge_scope_keys": ["tenant"],
                "placeholder_labels": {"__ANY__": list(concrete)},
            },
        }
    )


_ITEM_ID = "CREATE CONSTRAINT item_id IF NOT EXISTS FOR (n:Item) REQUIRE (n.tenant, n.id) IS UNIQUE"
_TEMPLATE = (
    "CREATE CONSTRAINT any_uuid IF NOT EXISTS FOR (n:__ANY__) REQUIRE (n.tenant, n.uuid) IS UNIQUE"
)


def _concrete_uuid(label: str) -> str:
    return (
        f"CREATE CONSTRAINT {label.lower()}_uuid IF NOT EXISTS FOR (n:{label}) "
        "REQUIRE (n.tenant, n.uuid) IS UNIQUE"
    )


def _missing_and_undeclared(findings: Sequence[Finding]) -> list[tuple[str, str]]:
    return sorted((f.rule, f.message.split(" ")[0]) for f in findings)


def test_a_placeholder_with_no_concrete_label_needs_no_ddl(tmp_path: Path) -> None:
    assert _rules(tmp_path, _placeholder_schema(()), [_ITEM_ID]) == set()


def test_a_placeholder_template_in_the_ddl_is_declared(tmp_path: Path) -> None:
    assert _rules(tmp_path, _placeholder_schema(()), [_ITEM_ID, _TEMPLATE]) == set()


def test_each_concrete_label_of_a_placeholder_needs_its_ddl(tmp_path: Path) -> None:
    schema = _placeholder_schema(("Alpha", "Beta"))
    findings = _findings(tmp_path, schema, [_ITEM_ID, _TEMPLATE, _concrete_uuid("Alpha")])
    assert _missing_and_undeclared(findings) == [("parity-unique-missing-in-ddl", "Beta.uuid")]


def test_concrete_ddl_of_every_concrete_label_is_clean(tmp_path: Path) -> None:
    schema = _placeholder_schema(("Alpha", "Beta"))
    statements = [_ITEM_ID, _concrete_uuid("Alpha"), _concrete_uuid("Beta")]
    assert _rules(tmp_path, schema, statements) == set()


def test_concrete_ddl_of_a_label_the_placeholder_does_not_map_is_undeclared(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _placeholder_schema(()), [_ITEM_ID, _concrete_uuid("Alpha")])
    assert _missing_and_undeclared(findings) == [("parity-unique-undeclared", "Alpha.uuid")]


def test_a_template_on_an_undeclared_placeholder_property_is_undeclared(tmp_path: Path) -> None:
    template = _TEMPLATE.replace("n.uuid", "n.code")
    findings = _findings(tmp_path, _placeholder_schema(()), [_ITEM_ID, template])
    assert _missing_and_undeclared(findings) == [("parity-unique-undeclared", "__ANY__.code")]


def test_a_placeholder_outside_the_register_needs_its_own_ddl(tmp_path: Path) -> None:
    schema = _placeholder_schema(())
    unregistered = replace(schema, policy=replace(schema.policy, placeholder_labels={}))
    findings = _findings(tmp_path, unregistered, [_ITEM_ID])
    assert _missing_and_undeclared(findings) == [("parity-unique-missing-in-ddl", "__ANY__.uuid")]
    assert _rules(tmp_path, unregistered, [_ITEM_ID, _TEMPLATE]) == set()


def test_a_placeholder_relationship_index_expands_to_its_concrete_types(tmp_path: Path) -> None:
    schema = parse_schema(
        {
            "nodes": {"Item": {"properties": {"id": {"type": "string"}}}},
            "relationships": {
                "__REL__": {"from": ["Item"], "to": ["Item"], "properties": ["tenant", "key"]},
            },
            "constraints": {"indexes": ["__REL__.key"]},
            "policy": {
                "merge_scope_keys": ["tenant"],
                "placeholder_labels": {"__REL__": ["LINKS"]},
            },
        }
    )
    findings = _findings(tmp_path, schema, [])
    assert _missing_and_undeclared(findings) == [("parity-index-missing-in-ddl", "LINKS.key")]
    statement = "CREATE INDEX links_key IF NOT EXISTS FOR ()-[r:LINKS]-() ON (r.tenant, r.key)"
    assert _rules(tmp_path, schema, [statement]) == set()
