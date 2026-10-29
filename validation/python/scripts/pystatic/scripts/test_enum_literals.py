"""Self-test for `enum_literals`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import enum_literals as rule

KIT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "enum_literals"

ENUM_VALUES = frozenset({"ACTIVE", "INACTIVE", "ADMIN"})


@pytest.fixture(autouse=True)
def _kit_root() -> None:
    """Fixture modules import each other as `pystatic.scripts.fixtures...`."""
    files.configure(KIT_ROOT)


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_untyped_enum_typed_mismatched_unresolved_and_shadowed_calls_are_flagged() -> None:
    findings = rule.scan_file(FIXTURES / "positive.py", ENUM_VALUES)
    assert len(findings) == 9
    assert {f.rule for f in findings} == {"enum-literals-hand-typed"}
    messages = [finding.message for finding in findings]

    def count(name: str, value: str) -> int:
        expected = (
            f"{name!r} compared/assigned to the string literal {value!r}, a declared enum value"
        )
        return messages.count(expected)

    assert count("kind", "ACTIVE") == 3
    assert count("status", "ACTIVE") == 4
    assert count("role", "ADMIN") == 1
    assert count("kind", "ADMIN") == 1


def test_negative_fixture_is_clean() -> None:
    assert rule.scan_file(FIXTURES / "negative.py", ENUM_VALUES) == []


def test_configured_names_replace_the_default_eight() -> None:
    findings = rule.scan_file(
        FIXTURES / "positive.py", ENUM_VALUES, names=frozenset({"nothing_matches"})
    )
    assert findings == []


def test_shadowed_literal_constructor_remains_flagged(tmp_path: Path) -> None:
    path = tmp_path / "shadowed.py"
    statement = ast.parse('ImportedLiteralEntry(kind="ADMIN")').body[0]
    assert isinstance(statement, ast.Expr)
    assert isinstance(statement.value, ast.Call)
    contracts = rule._ContractIndex(
        classes={("fixture.models", "ImportedLiteralEntry"): {"kind": frozenset({"ADMIN"})}},
        files={
            path.resolve(): rule._FileContracts(
                local={},
                imported_classes={
                    "ImportedLiteralEntry": ("fixture.models", "ImportedLiteralEntry")
                },
                imported_modules={},
                rebound={"ImportedLiteralEntry"},
            )
        },
    )

    finding = rule._check_keyword(
        "shadowed.py",
        path,
        statement.value,
        statement.value.keywords[0],
        rule._Rule(values=ENUM_VALUES),
        contracts,
    )

    assert finding is not None
    assert finding.rule == "enum-literals-hand-typed"


def test_enum_values_are_harvested_from_enum_classes() -> None:
    harvested = rule.harvest_enum_values([FIXTURES / "literal_models.py", FIXTURES / "negative.py"])
    assert harvested == frozenset({"ACTIVE", "INACTIVE"})


def test_run_without_a_configuration_table_is_a_visible_skip(tmp_path: Path) -> None:
    files.configure(tmp_path)
    result = rule.run()
    assert result.exit_code == 0
    assert result.detail.startswith("skipped:")


def test_run_reads_values_and_harvest_from_the_configuration(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from enum import StrEnum\n\n"
        "class Status(StrEnum):\n    OPEN = 'OPEN'\n\n"
        "def closed(order: object) -> bool:\n"
        "    return order.status == 'OPEN' or order.kind == 'LEGACY'\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text('[enum-literals]\nvalues = ["LEGACY"]\nvalues-from-enums = true\n')
    files.configure(tmp_path)
    config.configure(cfg)

    result = rule.run()

    assert result.exit_code == 1
    assert [(f.path, f.line) for f in result.findings] == [
        ("src/app/service.py", 7),
        ("src/app/service.py", 7),
    ]
