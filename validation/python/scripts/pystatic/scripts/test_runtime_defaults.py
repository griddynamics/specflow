"""Self-test for `runtime_defaults`."""

from __future__ import annotations

from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import runtime_defaults as rule

DEFAULTS: dict[str, int | float] = {"CART_MAX_ITEMS": 50, "SESSION_TTL_SECONDS": 900}


def _rules(text: str) -> list[tuple[int, str]]:
    return [(f.line, f.message) for f in rule.scan_text("t.py", text, DEFAULTS)]


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_names_cover_the_three_spellings() -> None:
    assert rule.names("CART_MAX_ITEMS") == ["CART_MAX_ITEMS", "cart_max_items", "cartMaxItems"]


def test_a_default_near_its_setting_is_flagged() -> None:
    text = "def test_cart(cart_max_items: int) -> None:\n    assert len(cart) == 50\n"
    assert _rules(text) == [(2, "hard-coded 50 is the documented default of CART_MAX_ITEMS")]


def test_the_same_number_far_from_the_setting_is_not_flagged() -> None:
    text = "cart_max_items = settings.cart_max_items\n" + "pass\n" * 6 + "page = 50\n"
    assert _rules(text) == []


def test_an_override_under_the_quoted_name_is_not_flagged() -> None:
    assert _rules('overrides = {"CART_MAX_ITEMS": 50}\n') == []


def test_comments_docstrings_and_ignored_values_are_skipped() -> None:
    text = '"""cartMaxItems is 50."""\n# cartMaxItems 50\nconst cartMaxItems = 1;\n'
    assert _rules(text) == []


def test_run_reads_the_defaults_file(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "session.spec.ts").write_text(
        "const sessionTtlSeconds = settings.ttl;\nexpect(ttl).toBe(900);\n"
    )
    (tmp_path / "defaults.json").write_text('{"SESSION_TTL_SECONDS": 900}')
    (tmp_path / "pystatic.toml").write_text('[runtime-defaults]\ndefaults = "defaults.json"\n')
    files.configure(tmp_path)
    result = rule.run()
    assert [(f.path, f.line) for f in result.findings] == [("tests/session.spec.ts", 2)]
    assert "defaults.json" in result.findings[0].fix


def test_a_malformed_defaults_file_is_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "defaults.json").write_text('{"X": "ten"}')
    (tmp_path / "pystatic.toml").write_text('[runtime-defaults]\ndefaults = "defaults.json"\n')
    files.configure(tmp_path)
    with pytest.raises(config.ConfigError, match="SETTING_NAME: number"):
        rule.check()
