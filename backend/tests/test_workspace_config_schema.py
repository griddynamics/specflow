"""The workspace binding file contract: the valid example passes and every invalid shape fails."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

BACKEND = Path(__file__).resolve().parent.parent
SCHEMA_PATH = BACKEND / "contracts" / "schemas" / "specflow-workspace-config.schema.json"
EXAMPLE_PATH = BACKEND / "tests" / "fixtures" / "mcp" / "specflow-workspace-config.json"
PROJECT_ID = "6f1c2a9e-3b0d-4c55-9a51-0d6c1e2f7a10"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _valid(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    document.update(changes)
    return document


def test_the_example_file_is_valid(validator: Draft202012Validator) -> None:
    assert list(validator.iter_errors(_valid())) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": PROJECT_ID.upper()},
        {"server": "http://localhost:8002/mcp"},
        {"project_name": "x"},
        {"project_name": "x" * 200},
    ],
    ids=["uppercase-id", "http-server", "name-of-1", "name-of-200"],
)
def test_a_valid_variation_passes(validator: Draft202012Validator, changes: dict[str, Any]) -> None:
    assert list(validator.iter_errors(_valid(**changes))) == []


@pytest.mark.parametrize(
    "document",
    [
        _valid(extra="key"),
        _valid(version=2),
        _valid(version="1"),
        _valid(version=True),
        _valid(project_id="not-a-uuid"),
        _valid(project_id=PROJECT_ID.replace("-", "")),
        _valid(project_id=PROJECT_ID[:-1] + "g"),
        _valid(project_id=PROJECT_ID + "0"),
        _valid(project_id=None),
        _valid(server="/mcp"),
        _valid(server="specflow.example.com/mcp"),
        _valid(server="ftp://specflow.example.com/mcp"),
        _valid(server=7),
        _valid(project_name=""),
        _valid(project_name="x" * 201),
        _valid(project_name=None),
        [],
    ],
    ids=[
        "extra-key",
        "version-2",
        "version-string",
        "version-bool",
        "id-not-uuid",
        "id-without-dashes",
        "id-not-hex",
        "id-too-long",
        "id-null",
        "server-relative",
        "server-without-scheme",
        "server-not-http",
        "server-not-string",
        "name-empty",
        "name-201-characters",
        "name-null",
        "not-an-object",
    ],
)
def test_an_invalid_document_fails(validator: Draft202012Validator, document: Any) -> None:
    assert list(validator.iter_errors(document)) != []


@pytest.mark.parametrize("key", ["version", "server", "project_id", "project_name"])
def test_every_key_is_required(validator: Draft202012Validator, key: str) -> None:
    document = _valid()
    del document[key]
    assert list(validator.iter_errors(document)) != []
