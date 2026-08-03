"""The contracts oracle: keep the compiler, drop the application.

A real build gives you an oracle that does not care what the agent believes —
the schema either loads or it does not. Dropping the build loses that, but not
all of it: you can still emit the data model and API contract as *real*
artifacts and check them mechanically, at no runtime cost.

Two kinds of check:

  ``check_model``    contradictions derivable from the interpretation alone.
                     Always runnable, no emitted files needed.
  ``check_emitted``  cross-checks emitted SQL DDL and API JSON against the
                     model, so the agent cannot describe one system and emit
                     another.

The interesting finds are contradictions rather than omissions. Spec ambiguity
tends to surface as a structural impossibility — a field that must be both
supplied and computed, or two entities that each require the other to exist
first. Nobody writes those on purpose; they are what an underdetermined spec
looks like once you make it concrete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`\[]?(\w+)[\"`\]]?\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_REFERENCES = re.compile(r"references\s+[\"`\[]?(\w+)[\"`\]]?", re.IGNORECASE)
_PRIMARY_KEY = re.compile(r"primary\s+key", re.IGNORECASE)


@dataclass
class ContractIssue:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass
class ContractReport:
    issues: list[ContractIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, kind: str, detail: str) -> None:
        self.issues.append(ContractIssue(kind, detail))


def check_model(interpretation: dict[str, Any]) -> ContractReport:
    """Structural contradictions in the model itself."""
    report = ContractReport()
    entities = {e.get("name"): e for e in interpretation.get("entities", [])}

    for name, entity in entities.items():
        for field_def in entity.get("fields", []):
            fname = f"{name}.{field_def.get('name')}"

            # Supplied by the caller AND computed by the system: impossible.
            if field_def.get("required") and field_def.get("derived"):
                report.add(
                    "contradiction",
                    f"{fname} is both required (caller supplies it) and derived "
                    "(system computes it) — the spec does not say which",
                )

            target = field_def.get("references")
            if target and target not in entities:
                report.add("dangling-reference", f"{fname} references undefined entity '{target}'")

        if not entity.get("identity"):
            report.add("no-identity", f"{name} has no primary key — rows cannot be addressed")

    _check_circular_requirements(entities, report)
    _check_operation_fields(interpretation, entities, report)
    return report


def _check_circular_requirements(
    entities: dict[str, Any], report: ContractReport
) -> None:
    """Two entities that each require a reference to the other cannot be created.

    A real insert would deadlock on this. It is a common shape when a spec
    describes a relationship without saying which side comes first.
    """
    required_edges: dict[str, set[str]] = {}
    for name, entity in entities.items():
        targets = {
            f.get("references")
            for f in entity.get("fields", [])
            if f.get("references") and f.get("required")
        }
        required_edges[name] = {t for t in targets if t and t != name}

    seen_pairs = set()
    for name, targets in required_edges.items():
        for target in targets:
            if name in required_edges.get(target, set()):
                pair = tuple(sorted((name, target)))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    report.add(
                        "circular-requirement",
                        f"{pair[0]} and {pair[1]} each require a reference to the other — "
                        "neither can be created first",
                    )


def _check_operation_fields(
    interpretation: dict[str, Any], entities: dict[str, Any], report: ContractReport
) -> None:
    """Operation inputs and outputs must name fields that exist on the entity."""
    for operation in interpretation.get("operations", []):
        entity_name = operation.get("entity")
        entity = entities.get(entity_name)
        if not entity:
            continue  # totality.py reports the missing entity.
        known = {f.get("name") for f in entity.get("fields", [])}
        for direction in ("inputs", "outputs"):
            for ref in operation.get(direction, []):
                bare = ref.split(".")[-1]
                if bare not in known:
                    report.add(
                        "unknown-field",
                        f"{operation.get('name')} {direction[:-1]} '{ref}' is not a field "
                        f"of {entity_name}",
                    )

        if operation.get("kind") in ("create", "update", "delete", "command"):
            if not operation.get("authorization"):
                report.add(
                    "unguarded-mutation",
                    f"{operation.get('name')} changes data but the spec does not say who may call it",
                )


def parse_ddl(sql: str) -> dict[str, dict[str, Any]]:
    """Extract table names, referenced tables and primary-key presence from DDL.

    Not a SQL parser — a contradiction detector. It needs to know what tables
    exist, what they point at, and whether they are addressable.
    """
    tables: dict[str, dict[str, Any]] = {}
    for match in _CREATE_TABLE.finditer(sql):
        name, body = match.group(1), match.group(2)
        tables[name] = {
            "references": {m.group(1) for m in _REFERENCES.finditer(body)},
            "has_primary_key": bool(_PRIMARY_KEY.search(body)),
            "body": body,
        }
    return tables


def check_emitted(
    interpretation: dict[str, Any],
    *,
    sql: str | None = None,
    api: str | None = None,
) -> ContractReport:
    """Cross-check emitted artifacts against the model they claim to implement."""
    report = ContractReport()
    entities = {e.get("name") for e in interpretation.get("entities", [])}

    if sql is not None:
        tables = parse_ddl(sql)
        if not tables:
            report.add("empty-ddl", "no CREATE TABLE statements found in the emitted SQL")
        lowered = {t.lower() for t in tables}
        for name in entities:
            if name and name.lower() not in lowered and f"{name.lower()}s" not in lowered:
                report.add("missing-table", f"entity '{name}' has no table in the emitted DDL")
        for table, meta in tables.items():
            if not meta["has_primary_key"]:
                report.add("no-primary-key", f"table '{table}' declares no primary key")
            for target in meta["references"]:
                if target.lower() not in lowered:
                    report.add(
                        "dangling-fk",
                        f"table '{table}' references '{target}', which is not created",
                    )

    if api is not None:
        try:
            document = json.loads(api)
        except json.JSONDecodeError as exc:
            report.add("invalid-api-json", f"emitted API contract is not valid JSON: {exc}")
            return report
        _check_api(document, interpretation, report)

    return report


def _check_api(
    document: Any, interpretation: dict[str, Any], report: ContractReport
) -> None:
    """Validate an OpenAPI-shaped JSON document structurally."""
    if not isinstance(document, dict):
        report.add("invalid-api-json", "API contract should be a JSON object")
        return

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        report.add("no-paths", "API contract declares no paths")
        return

    schemas = (document.get("components") or {}).get("schemas") or {}

    # Every $ref must resolve — a dangling ref means the contract describes a
    # shape it never defines.
    def refs(node: Any):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    yield value
                else:
                    yield from refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from refs(item)

    for ref in set(refs(document)):
        if ref.startswith("#/components/schemas/"):
            if ref.rsplit("/", 1)[-1] not in schemas:
                report.add("dangling-ref", f"{ref} does not resolve")
        else:
            report.add("external-ref", f"{ref} points outside the document")

    for name, schema in schemas.items():
        if isinstance(schema, dict):
            for prop, definition in (schema.get("properties") or {}).items():
                if isinstance(definition, dict) and not (
                    definition.get("type") or definition.get("$ref") or definition.get("allOf")
                ):
                    report.add("untyped-property", f"{name}.{prop} has no type")

    operation_count = sum(
        1
        for methods in paths.values()
        if isinstance(methods, dict)
        for method in methods
        if method.lower() in ("get", "post", "put", "patch", "delete")
    )
    declared = len(interpretation.get("operations", []))
    if declared and operation_count < declared:
        report.add(
            "incomplete-api",
            f"model declares {declared} operation(s) but the contract exposes "
            f"{operation_count} — some are unreachable",
        )
