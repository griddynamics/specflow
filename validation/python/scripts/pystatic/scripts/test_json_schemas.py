"""Self-test for `json_schemas`. The schema-validation cases run the real `check-jsonschema`,
which the kit's environment installs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pystatic import config, files
from pystatic.harness.docref import doc_anchor_exists
from pystatic.scripts import json_schemas as rule

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["kind", "items"],
    "properties": {
        "kind": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A target tree with one schema, one valid document and the configuration."""
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "catalog.schema.json").write_text(json.dumps(SCHEMA))
    data = tmp_path / "data"
    data.mkdir()
    (data / "books.json").write_text('{"kind": "BOOKS", "items": ["a", "b"]}')
    cfg = tmp_path / "pystatic.toml"
    cfg.write_text(
        "[[json-schemas]]\n"
        'schema = "schemas/catalog.schema.json"\n'
        'files = ["data/*.json"]\n'
        'name-field = "kind"\n'
    )
    files.configure(tmp_path)
    config.configure(cfg)
    return data


def _tool() -> str:
    tool = shutil.which(rule.TOOL)
    assert tool is not None, "check-jsonschema is a declared kit dependency"
    return tool


def test_doc_anchor_resolves() -> None:
    assert doc_anchor_exists(rule.DOC) is True


def test_parse_strict_refuses_a_repeated_key() -> None:
    with pytest.raises(rule.DuplicateKeyError, match="repeated key 'a'"):
        rule.parse_strict('{"outer": {"a": 1, "a": 2}}')


def test_valid_corpus_is_clean(corpus: Path) -> None:
    assert corpus.is_dir()
    assert rule.check(_tool()) == []


def test_schema_violation_is_reported(corpus: Path) -> None:
    (corpus / "books.json").write_text('{"kind": "BOOKS", "items": [1]}')
    assert [f.rule for f in rule.check(_tool())] == ["json-schemas-invalid"]


def test_repeated_key_and_file_name_rules(corpus: Path) -> None:
    (corpus / "music.json").write_text('{"kind": "MUSIC", "items": [], "items": []}')
    (corpus / "films.json").write_text('{"kind": "MOVIES", "items": []}')
    found = {(Path(f.path).name, f.rule) for f in rule.check(_tool())}
    assert found == {
        ("music.json", "json-schemas-duplicate-key"),
        ("films.json", "json-schemas-file-name"),
    }


def test_missing_schema_is_a_finding(corpus: Path) -> None:
    (corpus.parent / "schemas" / "catalog.schema.json").unlink()
    assert [f.rule for f in rule.check(_tool())] == ["json-schemas-missing-schema"]


def test_parse_report_reads_errors_and_parse_errors() -> None:
    report = {
        "status": "fail",
        "errors": [{"filename": "a.json", "path": "$.x", "message": "bad"}],
        "parse_errors": [{"filename": "b.json", "message": "not JSON"}],
    }
    rules = [f.rule for f in rule.parse_report(json.dumps(report))]
    assert rules == ["json-schemas-invalid", "json-schemas-unparseable"]


def test_run_without_entries_is_a_visible_skip(tmp_path: Path) -> None:
    files.configure(tmp_path)
    assert rule.run().detail.startswith("skipped:")


def test_tool_is_found_without_the_interpreter_directory_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert rule.find_tool() is not None
