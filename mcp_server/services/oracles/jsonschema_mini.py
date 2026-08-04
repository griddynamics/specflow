"""A JSON Schema validator covering exactly the keywords SpecFlow's schemas use.

Why not the ``jsonschema`` package: this library ships inside a Claude Code
marketplace plugin and runs on whatever Python the user happens to have. A
``pip install`` step turns a working skill into a support ticket, so the
validator is stdlib-only.

The trade is deliberate: we own both the schemas and the validator, so the
supported keyword set is closed. Anything used in ``schema/*.json`` is
implemented here; anything not implemented raises rather than silently passing,
so a schema author cannot accidentally write a constraint that is never checked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).parent / "schema"

# Keywords this validator understands. A schema using anything else is a bug in
# the schema, not an input we should quietly accept.
_SUPPORTED = frozenset({
    "$schema", "$id", "$ref", "$defs", "title", "description",
    "type", "required", "properties", "additionalProperties",
    "items", "minItems", "maxItems",
    "minLength", "maxLength", "pattern",
    "enum", "const", "minimum", "maximum",
    "anyOf", "oneOf",
})

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
    "null": type(None),
}


@dataclass
class Problem:
    """One validation failure, addressed by JSON path so it can be acted on."""

    path: str
    message: str

    def __str__(self) -> str:
        where = self.path or "<root>"
        return f"{where}: {self.message}"


@dataclass
class Result:
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))

    def merge(self, other: "Result") -> None:
        self.problems.extend(other.problems)


class SchemaStore:
    """Loads and resolves the bundled schemas by ``$id``."""

    def __init__(self, schema_dir: Path = SCHEMA_DIR) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        for path in sorted(schema_dir.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schema_id = schema.get("$id")
            if not schema_id:
                raise ValueError(f"{path.name} has no $id — cannot be referenced")
            self._by_id[schema_id] = schema

    def get(self, schema_id: str) -> dict[str, Any]:
        try:
            return self._by_id[schema_id]
        except KeyError:
            raise KeyError(
                f"Unknown schema '{schema_id}'. Known: {sorted(self._by_id)}"
            ) from None

    def resolve(self, ref: str, current: dict[str, Any]) -> dict[str, Any]:
        """Resolve a ``$ref``. Supports whole-schema ids and local ``#/$defs/x``."""
        if ref.startswith("#/"):
            node: Any = current
            for part in ref[2:].split("/"):
                node = node[part]
            return node
        return self.get(ref)


@lru_cache(maxsize=1)
def default_store() -> SchemaStore:
    """The bundled schemas, read once per process.

    Every lens artifact in a round is validated against the same three files;
    building a store per call re-read and re-parsed all of them each time. The
    store is never mutated, so sharing it is safe.
    """
    return SchemaStore()


def _type_name(value: Any) -> str:
    for name, py in _TYPE_MAP.items():
        # bool is a subclass of int; check it before number/integer.
        if name in ("number", "integer") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return name
    return type(value).__name__


def _check_type(value: Any, expected: str) -> bool:
    py = _TYPE_MAP.get(expected)
    if py is None:
        raise ValueError(f"Unsupported type keyword: {expected!r}")
    if expected in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, py)


def validate(
    instance: Any,
    schema: dict[str, Any],
    *,
    store: SchemaStore | None = None,
    root: dict[str, Any] | None = None,
    path: str = "",
) -> Result:
    """Validate ``instance`` against ``schema``. Returns every problem found."""
    store = store or default_store()
    root = root if root is not None else schema
    result = Result()

    unsupported = set(schema) - _SUPPORTED
    if unsupported:
        raise ValueError(
            f"Schema at {path or '<root>'} uses unimplemented keywords: "
            f"{sorted(unsupported)}. Implement them in jsonschema_mini or "
            f"remove them from the schema — a silently-ignored constraint is worse "
            f"than no constraint."
        )

    if "$ref" in schema:
        target = store.resolve(schema["$ref"], root)
        # A cross-schema ref carries its own $defs, so it becomes the new root.
        new_root = target if schema["$ref"].startswith("specflow/") else root
        return validate(instance, target, store=store, root=new_root, path=path)

    if "anyOf" in schema or "oneOf" in schema:
        branches = schema.get("anyOf") or schema.get("oneOf") or []
        for branch in branches:
            if validate(instance, branch, store=store, root=root, path=path).ok:
                return result
        result.add(path, "matches none of the allowed shapes")
        return result

    expected = schema.get("type")
    if expected and not _check_type(instance, expected):
        result.add(path, f"expected {expected}, got {_type_name(instance)}")
        return result  # Wrong type — downstream checks would be noise.

    if "const" in schema and instance != schema["const"]:
        result.add(path, f"must be {schema['const']!r}")

    if "enum" in schema and instance not in schema["enum"]:
        result.add(path, f"{instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            result.add(path, f"must be at least {schema['minLength']} character(s)")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            result.add(path, f"must be at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            result.add(path, f"{instance!r} does not match {schema['pattern']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            result.add(path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            result.add(path, f"must be <= {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            result.add(path, f"needs at least {schema['minItems']} item(s), has {len(instance)}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            result.add(path, f"allows at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                result.merge(
                    validate(item, item_schema, store=store, root=root, path=f"{path}[{i}]")
                )

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                result.add(path, f"missing required property '{key}'")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            child = f"{path}.{key}" if path else key
            if key in properties:
                result.merge(
                    validate(value, properties[key], store=store, root=root, path=child)
                )
                continue
            extra = schema.get("additionalProperties")
            if extra is False:
                result.add(path, f"unexpected property '{key}'")
            elif isinstance(extra, dict):
                result.merge(validate(value, extra, store=store, root=root, path=child))

    return result


def validate_as(instance: Any, schema_id: str) -> Result:
    """Validate against a bundled schema by ``$id``."""
    store = default_store()
    schema = store.get(schema_id)
    return validate(instance, schema, store=store, root=schema)
