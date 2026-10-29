"""Builders of the JSON Schemas under ``contracts/schemas/`` exported from code. No tests here.

``spec-ontology`` comes from ``graph.core.ontology.model``, ``temporal-history`` from the Temporal API
protos; ``test_contract_schemas`` keeps the committed files equal. Regenerate:
``uv run python tests/contract_schemas_support.py``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Final

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from pydantic import ConfigDict, Field
from temporalio.api.history.v1 import HistoryEvent

from graph.core.ontology.model import (
    CorpusDef,
    EdgeTypeDef,
    FileHeaderDef,
    InferenceDef,
    NodeGroupDef,
    NodeTypeDef,
    SpineDef,
)

SCHEMA_DIR: Final = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
ID_BASE: Final = "https://specflow.local/contracts/schemas/"
ONTOLOGY_SCHEMA: Final = "spec-ontology.schema.json"
TEMPORAL_SCHEMA: Final = "temporal-history.schema.json"

VocabularyList = Annotated[list[str], Field(min_length=1, json_schema_extra={"uniqueItems": True})]
VocabularyMap = Annotated[dict[str, str], Field(min_length=1)]


class OntologyFile(FileHeaderDef):
    """One ontology file with every section typed by the model the loader validates its entries with."""

    model_config = ConfigDict(title="Ontology file")

    corpus: CorpusDef | None = None
    vocabularies: dict[str, VocabularyList | VocabularyMap] = Field(default_factory=dict)
    node_groups: dict[str, NodeGroupDef] = Field(default_factory=dict)
    node_types: dict[str, NodeTypeDef] = Field(default_factory=dict)
    edge_types: dict[str, EdgeTypeDef] = Field(default_factory=dict)
    spine: SpineDef | None = None
    inference: InferenceDef | None = None


def ontology_schema() -> dict[str, Any]:
    """The JSON Schema of one ontology YAML file (docs/graph/ontology.md "Format")."""

    body = OntologyFile.model_json_schema(by_alias=True)
    return {"$schema": DIALECT, "$id": ID_BASE + ONTOLOGY_SCHEMA, **body}


_INTEGER_64: Final[dict[str, Any]] = {"type": "string", "pattern": "^-?[0-9]+$"}
_INTEGER_32: Final[dict[str, Any]] = {"type": "integer"}
# The proto3 JSON mapping of each scalar field type (64-bit integers are decimal strings, bytes base64).
_SCALARS: Final[dict[int, dict[str, Any]]] = {
    FieldDescriptor.TYPE_INT64: _INTEGER_64,
    FieldDescriptor.TYPE_UINT64: _INTEGER_64,
    FieldDescriptor.TYPE_SINT64: _INTEGER_64,
    FieldDescriptor.TYPE_FIXED64: _INTEGER_64,
    FieldDescriptor.TYPE_SFIXED64: _INTEGER_64,
    FieldDescriptor.TYPE_INT32: _INTEGER_32,
    FieldDescriptor.TYPE_UINT32: _INTEGER_32,
    FieldDescriptor.TYPE_SINT32: _INTEGER_32,
    FieldDescriptor.TYPE_FIXED32: _INTEGER_32,
    FieldDescriptor.TYPE_SFIXED32: _INTEGER_32,
    FieldDescriptor.TYPE_BOOL: {"type": "boolean"},
    FieldDescriptor.TYPE_STRING: {"type": "string"},
    FieldDescriptor.TYPE_BYTES: {"type": "string"},
    FieldDescriptor.TYPE_DOUBLE: {"type": "number"},
    FieldDescriptor.TYPE_FLOAT: {"type": "number"},
}
# Well-known message types with a string JSON mapping; every other message is an object.
_MESSAGES: Final[dict[str, dict[str, Any]]] = {
    "google.protobuf.Timestamp": {"type": "string", "format": "date-time"},
    "google.protobuf.Duration": {"type": "string", "pattern": "^-?[0-9]+(\\.[0-9]+)?s$"},
}


def _proto_value(field: FieldDescriptor) -> dict[str, Any]:
    """The proto3 JSON mapping of one singular field value."""

    if field.type == FieldDescriptor.TYPE_ENUM:
        return {"enum": [value.name for value in field.enum_type.values]}
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return dict(_MESSAGES.get(field.message_type.full_name, {"type": "object"}))
    return dict(_SCALARS[field.type])


def _proto_field(field: FieldDescriptor) -> dict[str, Any]:
    if field.is_repeated:
        return {"type": "array", "items": _proto_value(field)}
    return _proto_value(field)


def _event_type_of(attributes: FieldDescriptor) -> str:
    """``workflow_task_started_event_attributes`` -> ``EVENT_TYPE_WORKFLOW_TASK_STARTED``."""

    return "EVENT_TYPE_" + attributes.name.removesuffix("_event_attributes").upper()


_EVENT_DESCRIPTION: Final = (
    "One HistoryEvent: its common fields and exactly one attributes object, the one its eventType names."
)


def _event_schema(event: Descriptor) -> dict[str, Any]:
    attributes = list(event.oneofs_by_name["attributes"].fields)
    common = [field for field in event.fields if field.containing_oneof is None]
    properties: dict[str, Any] = {field.json_name: _proto_field(field) for field in common}
    properties.update({field.json_name: {"type": "object"} for field in attributes})
    return {
        "description": _EVENT_DESCRIPTION,
        "type": "object",
        "additionalProperties": False,
        "required": ["eventId", "eventTime", "eventType"],
        "properties": properties,
        "oneOf": [{"required": [field.json_name]} for field in attributes],
        "allOf": [
            {
                "if": {"properties": {"eventType": {"const": _event_type_of(field)}}},
                "then": {"required": [field.json_name]},
            }
            for field in attributes
        ],
    }


def temporal_history_schema() -> dict[str, Any]:
    """The JSON Schema of a Temporal workflow history (``temporal workflow show --output json``)."""

    return {
        "$schema": DIALECT,
        "$id": ID_BASE + TEMPORAL_SCHEMA,
        "title": "Temporal workflow history",
        "description": "temporal.api.history.v1.History in the proto3 JSON mapping, as the replay fixtures store it.",
        "type": "object",
        "additionalProperties": False,
        "required": ["events"],
        "properties": {"events": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/HistoryEvent"}}},
        "$defs": {"HistoryEvent": _event_schema(HistoryEvent.DESCRIPTOR)},
    }


def generated() -> dict[str, dict[str, Any]]:
    """File name -> schema of every generated schema."""

    return {ONTOLOGY_SCHEMA: ontology_schema(), TEMPORAL_SCHEMA: temporal_history_schema()}


def render(schema: dict[str, Any]) -> str:
    """The committed text of a schema: two-space indent, trailing newline."""

    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def write_schemas() -> None:
    for name, schema in generated().items():
        (SCHEMA_DIR / name).write_text(render(schema), encoding="utf-8")


if __name__ == "__main__":
    write_schemas()
