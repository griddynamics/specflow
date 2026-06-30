"""Unit tests for services.file_sync.ensure_gain_json merge behavior."""

import json
from unittest.mock import patch

import pytest

from schemas.gain_json import SpecFlow_JSON_FILENAME
from services.file_sync import _canonical_gain_dict, _merge_gain_json, ensure_gain_json


def test_merge_preserves_top_level_and_nested_extras() -> None:
    canonical = _canonical_gain_dict()
    existing = {
        "otherTop": {"a": 1},
        "servicesDescription": {
            "extraSvc": "keep",
        },
        "versions": {
            "legacy": True,
        },
    }
    merged = _merge_gain_json(existing, canonical)
    assert merged["otherTop"] == {"a": 1}
    assert merged["servicesDescription"]["extraSvc"] == "keep"
    for k in canonical["servicesDescription"]:
        assert merged["servicesDescription"][k] == canonical["servicesDescription"][k]
    assert merged["versions"]["legacy"] is True
    for k in canonical["versions"]:
        assert merged["versions"][k] == canonical["versions"][k]


def test_merge_preserves_coding_agents_when_already_present() -> None:
    canonical = _canonical_gain_dict()
    existing = {"codingAgents": ["my-agent"]}
    merged = _merge_gain_json(existing, canonical)
    assert merged["codingAgents"] == ["my-agent"]


def test_merge_adds_coding_agents_when_absent() -> None:
    canonical = _canonical_gain_dict()
    existing: dict = {}
    merged = _merge_gain_json(existing, canonical)
    assert merged["codingAgents"] == canonical["codingAgents"]


def test_merge_preserves_rosetta_version_from_existing() -> None:
    canonical = _canonical_gain_dict()
    existing = {"versions": {"rosetta": "2.0.19", "specflow": "0.2.0"}}
    merged = _merge_gain_json(existing, canonical)
    # rosetta has no canonical counterpart, so the existing value survives.
    assert merged["versions"]["rosetta"] == "2.0.19"
    # specflow always reflects the currently running version (canonical wins),
    # so assert against canonical rather than a hardcoded value that only matches
    # when the package happens to be installed at that version.
    assert merged["versions"]["specflow"] == canonical["versions"]["specflow"]
    assert "rosetta" not in canonical["versions"]


def test_new_file_has_no_rosetta_in_versions(tmp_path) -> None:
    ensure_gain_json(tmp_path)
    data = json.loads((tmp_path / SpecFlow_JSON_FILENAME).read_text())
    assert "rosetta" not in data["versions"]
    assert "specflow" in data["versions"]


def test_ensure_merged_file_invalid_json_unchanged(tmp_path) -> None:
    path = tmp_path / SpecFlow_JSON_FILENAME
    path.write_text("not json {{{")
    with patch("services.file_sync.logger") as log:
        ensure_gain_json(tmp_path)
    assert path.read_text() == "not json {{{"
    log.error.assert_called()


def test_ensure_creates_file_when_absent(tmp_path) -> None:
    ensure_gain_json(tmp_path)
    p = tmp_path / SpecFlow_JSON_FILENAME
    assert p.exists()
    data = json.loads(p.read_text())
    assert "description" in data
    assert "servicesDescription" in data
    assert "codingAgents" in data
    assert "versions" in data
