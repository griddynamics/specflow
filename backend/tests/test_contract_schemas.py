"""The exported JSON Schemas under ``contracts/schemas/`` stay equal to the code they describe."""

from __future__ import annotations

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent

from contract_schemas_support import SCHEMA_DIR, generated, render


@pytest.mark.parametrize("name", sorted(generated()))
def test_the_committed_schema_matches_its_source(name: str) -> None:
    committed = (SCHEMA_DIR / name).read_text(encoding="utf-8")
    assert committed == render(generated()[name]), (
        f"contracts/schemas/{name} is stale: run `uv run python tests/contract_schemas_support.py`"
    )


def test_every_history_attributes_field_names_an_event_type() -> None:
    """The eventType -> attributes pairing of the Temporal schema is derived from field names."""

    event_types = {value.name for value in EventType.DESCRIPTOR.values}
    attributes = HistoryEvent.DESCRIPTOR.oneofs_by_name["attributes"].fields
    derived = {"EVENT_TYPE_" + field.name.removesuffix("_event_attributes").upper() for field in attributes}
    assert derived <= event_types
    assert len(derived) == len(attributes)


def test_the_ontology_schema_types_every_section() -> None:
    sections = generated()["spec-ontology.schema.json"]["properties"]
    for section in ("corpus", "node_groups", "node_types", "edge_types", "spine", "inference"):
        assert "$ref" in str(sections[section]), section
