"""Self-test for `config_literals`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import config_literals as rule

SETTINGS = rule.Settings(keys=frozenset({"retry_limit"}), config_classes=frozenset({"Weights"}))


def _literals(source: str) -> list[str]:
    return [f.rule for f in rule.literal_findings("e.py", ast.parse(source), SETTINGS)]


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_bare_literal_is_flagged() -> None:
    assert _literals("x = 7\n") == ["config-literals-numeric"]


def test_zero_one_minus_one_are_allowed() -> None:
    assert _literals("a = 0\nb = 1\nc = -1\n") == []


def test_keyed_upper_snake_constant_is_allowed() -> None:
    assert _literals("RETRY_LIMIT = 3\n") == []


def test_unkeyed_upper_snake_constant_is_still_flagged() -> None:
    assert _literals("SOME_OTHER_CONSTANT = 42\n") == ["config-literals-numeric"]


def test_subscript_positions_are_allowed_but_arithmetic_literals_are_not() -> None:
    assert _literals("value = item[2]\n") == []
    assert _literals("value = score * 2\n") == ["config-literals-numeric"]


def test_pydantic_field_bounds_are_schema_constraints() -> None:
    assert _literals("class Spec:\n    effort: int = Field(ge=1, le=5)\n") == []


def test_config_class_default_is_flagged() -> None:
    tree = ast.parse("class Weights:\n    damping: float = 0.5\n")
    findings = rule.default_field_findings("e.py", tree, SETTINGS)
    assert [f.rule for f in findings] == ["config-literals-class-default"]


def test_config_class_without_defaults_is_clean() -> None:
    tree = ast.parse("class Weights:\n    damping: float\n    depth: int = Field(ge=1, le=3)\n")
    assert rule.default_field_findings("e.py", tree, SETTINGS) == []


def test_config_class_field_with_explicit_default_is_flagged() -> None:
    tree = ast.parse("class Weights:\n    depth: int = Field(default=3, ge=1, le=3)\n")
    assert len(rule.default_field_findings("e.py", tree, SETTINGS)) == 1


def test_zero_arg_construction_is_flagged_and_configured_construction_is_clean() -> None:
    flagged = rule.zero_arg_construction_findings("e.py", ast.parse("w = Weights()\n"), SETTINGS)
    assert [f.rule for f in flagged] == ["config-literals-class-no-config"]
    clean = ast.parse("w = Weights(**cfg.weights)\nother = Other()\n")
    assert rule.zero_arg_construction_findings("e.py", clean, SETTINGS) == []


def test_run_is_skipped_without_a_table(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert rule.run().detail.startswith("skipped:")


def test_run_scans_the_configured_directories(tmp_path: Path) -> None:
    pricing = tmp_path / "src" / "pricing"
    pricing.mkdir(parents=True)
    (pricing / "rates.py").write_text("RETRY_LIMIT = 3\nfactor = 2.5\n")
    (pricing / "test_rates.py").write_text("expected = 42\n")
    (tmp_path / "src" / "other.py").write_text("w = Weights()\n")
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text(
        '[config-literals]\nscan = ["src/pricing"]\nkeys = ["retry_limit"]\n'
        'config-classes = ["Weights"]\n'
    )
    files.configure(tmp_path)
    config.configure(cfg)

    result = rule.run()

    assert [(f.path, f.rule) for f in result.findings] == [
        ("src/other.py", "config-literals-class-no-config"),
        ("src/pricing/rates.py", "config-literals-numeric"),
    ]


def _configure_scan(tmp_path: Path, scan: str) -> None:
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text(f'[config-literals]\nscan = {scan}\nkeys = ["api_port"]\n')
    files.configure(tmp_path)
    config.configure(cfg)


def test_a_scan_entry_may_name_one_module_and_leaves_its_siblings_alone(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "settings.py").write_text("API_PORT = 8000\ntimeout = 30\n")
    (src / "service.py").write_text("factor = 2.5\n")
    _configure_scan(tmp_path, '["src/settings.py"]')

    result = rule.run()

    assert [(f.path, f.line, f.rule) for f in result.findings] == [
        ("src/settings.py", 2, "config-literals-numeric")
    ]


def test_file_and_directory_entries_combine_without_duplicates(tmp_path: Path) -> None:
    pricing = tmp_path / "src" / "pricing"
    pricing.mkdir(parents=True)
    (pricing / "rates.py").write_text("factor = 2.5\n")
    _configure_scan(tmp_path, '["src/pricing", "src/pricing/rates.py"]')

    assert [f.path for f in rule.run().findings] == ["src/pricing/rates.py"]


def test_a_scan_entry_that_names_nothing_is_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("7\n")
    for entry in ('["src/missing"]', '["notes.txt"]'):
        _configure_scan(tmp_path, entry)
        with pytest.raises(config.ConfigError, match=r"neither a directory nor a \.py file"):
            rule.check()
