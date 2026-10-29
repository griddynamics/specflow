"""The graph schema and query policy every check reads, from one YAML file the adopter supplies.

The schema declares node labels, relationship types (with their start and end labels), the
properties of each, uniqueness constraints and range indexes. The optional `policy` section
holds the per-query registers the rules consult: DDL and write-file globs, scan-shaped queries,
capped traversals, banned scan labels, the edge re-point and node-removal allowances, the files
allowed to return vectors, the write-capable locations, the read-ownership register and the
concrete labels each placeholder label stands for. See
`examples/schema.yaml` and the README for the full shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cypher_checks.query_files import documented_name_for, normalize_query_name

__all__ = [
    "CappedTraversal",
    "GraphSchema",
    "NodeLabel",
    "Policy",
    "Property",
    "Relationship",
    "SchemaError",
    "load_schema",
    "parse_schema",
]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VECTOR_TYPE = "vector"


class SchemaError(Exception):
    """The schema file is unreadable or malformed; no check can run against it."""


@dataclass(frozen=True)
class Property:
    name: str
    type: str = ""
    enum: tuple[str, ...] = ()

    @property
    def is_vector(self) -> bool:
        return self.type.strip().lower() == _VECTOR_TYPE


@dataclass(frozen=True)
class NodeLabel:
    name: str
    properties: tuple[Property, ...] = ()


@dataclass(frozen=True)
class Relationship:
    name: str
    start_labels: tuple[str, ...]
    end_labels: tuple[str, ...]
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class CappedTraversal:
    """A query whose variable-length walk has a hard cap and a config key naming it."""

    query: str
    hard_cap: int
    key: str


@dataclass(frozen=True)
class Policy:
    """Per-query registers. Every register is empty by default, so each allowance fails closed."""

    ddl_globs: tuple[str, ...] = ()
    write_globs: tuple[str, ...] = ()
    scan_shaped_queries: tuple[str, ...] = ()
    banned_scan_labels: frozenset[str] = frozenset()
    capped_traversals: tuple[CappedTraversal, ...] = ()
    create_allowed_globs: tuple[str, ...] = ()
    node_removals: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    vector_read_queries: tuple[str, ...] = ()
    write_allowed_globs: tuple[str, ...] = ()
    read_globs: tuple[str, ...] = ()
    read_owners: tuple[str, ...] = ()
    #: Properties every uniqueness constraint is composite with (a tenant key such as
    #: `group_id`): a node `MERGE` may name them beside its one key, each as a `$parameter`.
    merge_scope_keys: tuple[str, ...] = ()
    #: Batch writes whose `MERGE` key may be a property of a row bound by any earlier `UNWIND`
    #: of a `$parameter` (or of a list property of such a row), not only an immediately
    #: preceding one.
    batch_merge_queries: tuple[str, ...] = ()
    #: Placeholder labels (a token a query or DDL template stands for, such as
    #: `__FOREIGN_LABEL__`) mapped to the concrete labels they stand for in this project.
    #: Parity expands a declared `Placeholder.property` to `Concrete.property` for each one: an
    #: empty list requires no DDL, and a DDL template on the placeholder itself is declared.
    placeholder_labels: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def capped_for(self, path: str | Path) -> CappedTraversal | None:
        """The capped-traversal row this file's stem binds to, or `None`."""
        name = documented_name_for(path, [row.query for row in self.capped_traversals])
        if name is None:
            return None
        wanted = normalize_query_name(name)
        return next(
            (row for row in self.capped_traversals if normalize_query_name(row.query) == wanted),
            None,
        )

    def is_scan_shaped(self, path: str | Path) -> bool:
        return documented_name_for(path, self.scan_shaped_queries) is not None

    def is_vector_read(self, path: str | Path) -> bool:
        return documented_name_for(path, self.vector_read_queries) is not None

    def is_batch_merge(self, path: str | Path) -> bool:
        return documented_name_for(path, self.batch_merge_queries) is not None

    def removable(self, path: str | Path) -> tuple[str, ...] | None:
        """The labels and types this file may delete, or `None` when no row names it."""
        name = documented_name_for(path, self.node_removals)
        return None if name is None else self.node_removals[name]


@dataclass(frozen=True)
class GraphSchema:
    nodes: Mapping[str, NodeLabel]
    relationships: Mapping[str, Relationship]
    common_label: str | None = None
    common_properties: tuple[Property, ...] = ()
    unique: tuple[tuple[str, str], ...] = ()
    indexes: tuple[tuple[str, str], ...] = ()
    policy: Policy = field(default_factory=Policy)

    def all_properties(self, label: str) -> tuple[Property, ...] | None:
        """A label's own properties plus the common ones, or `None` for an undeclared label."""
        node = self.nodes.get(label)
        if node is None:
            return None
        return (*self.common_properties, *node.properties)

    def node_property_names(self, label: str) -> set[str] | None:
        props = self.all_properties(label)
        return None if props is None else {p.name for p in props}

    def relationship_property_names(self, rel_type: str) -> set[str] | None:
        rel = self.relationships.get(rel_type)
        return None if rel is None else set(rel.properties)

    @property
    def unique_properties(self) -> frozenset[str]:
        return frozenset(prop for _label, prop in self.unique)

    def range_indexed_properties(self) -> list[tuple[str, str]]:
        """Declared `(label, property)` range indexes on a declared, non-vector node property."""
        pairs: set[tuple[str, str]] = set()
        for label, prop in self.indexes:
            props = {p.name: p for p in self.all_properties(label) or ()}
            if prop in props and not props[prop].is_vector:
                pairs.add((label, prop))
        return sorted(pairs)

    @property
    def anchorable_properties(self) -> frozenset[str]:
        """Every property a read may anchor on: unique constraints and range indexes alike."""
        return self.unique_properties | {prop for _l, prop in self.range_indexed_properties()}

    def vector_properties_by_label(self) -> dict[str, frozenset[str]]:
        return {
            label: frozenset(p.name for p in self.all_properties(label) or () if p.is_vector)
            for label in self.nodes
        }


def _identifier(name: object, kind: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        message = f"{kind} {name!r} is not a plain Cypher identifier"
        raise SchemaError(message)
    return name


def _strings(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        message = f"{where} must be a list of strings"
        raise SchemaError(message)
    return tuple(value)


def _properties(raw: object, where: str) -> tuple[Property, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        message = f"{where} must map property names to {{type, enum}}"
        raise SchemaError(message)
    props = []
    for name, spec in raw.items():
        body = spec if isinstance(spec, Mapping) else {"type": spec or ""}
        props.append(
            Property(
                _identifier(name, "property"),
                str(body.get("type", "")),
                _strings(body.get("enum"), f"{where}.{name}.enum"),
            )
        )
    return tuple(props)


def _pairs(raw: object, where: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for ref in _strings(raw, where):
        label, sep, prop = ref.partition(".")
        if not sep:
            message = f"{where} entry {ref!r} is not `Label.property`"
            raise SchemaError(message)
        pairs.append((_identifier(label, "label"), _identifier(prop, "property")))
    return tuple(pairs)


def _relationships(raw: object) -> dict[str, Relationship]:
    if not isinstance(raw, Mapping):
        message = "relationships must map each type to {from, to, properties}"
        raise SchemaError(message)
    rels: dict[str, Relationship] = {}
    for name, spec in raw.items():
        body: Mapping[str, Any] = spec if isinstance(spec, Mapping) else {}
        rel = Relationship(
            _identifier(name, "relationship type"),
            tuple(_identifier(s, "label") for s in _strings(body.get("from"), f"{name}.from")),
            tuple(_identifier(e, "label") for e in _strings(body.get("to"), f"{name}.to")),
            tuple(_identifier(p, "property") for p in _strings(body.get("properties"), name)),
        )
        if not rel.start_labels or not rel.end_labels:
            message = f"relationship {name} needs at least one `from` and one `to` label"
            raise SchemaError(message)
        rels[rel.name] = rel
    return rels


def _placeholder_labels(raw: object) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        message = "policy.placeholder_labels must map a placeholder label to its concrete labels"
        raise SchemaError(message)
    return {
        _identifier(name, "label"): tuple(
            _identifier(label, "label")
            for label in _strings(labels, f"policy.placeholder_labels.{name}")
        )
        for name, labels in raw.items()
    }


def _policy(raw: object) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, Mapping):
        message = "policy must be a mapping"
        raise SchemaError(message)
    capped = []
    for row in raw.get("capped_traversals") or []:
        if not isinstance(row, Mapping) or not {"query", "hard_cap", "key"} <= set(row):
            message = "each capped_traversals row needs query, hard_cap and key"
            raise SchemaError(message)
        capped.append(CappedTraversal(str(row["query"]), int(row["hard_cap"]), str(row["key"])))
    removals_raw = raw.get("node_removals") or {}
    if not isinstance(removals_raw, Mapping):
        message = "policy.node_removals must map a query name to the labels/types it deletes"
        raise SchemaError(message)
    return Policy(
        ddl_globs=_strings(raw.get("ddl_globs"), "policy.ddl_globs"),
        write_globs=_strings(raw.get("write_globs"), "policy.write_globs"),
        scan_shaped_queries=_strings(raw.get("scan_shaped_queries"), "policy.scan_shaped_queries"),
        banned_scan_labels=frozenset(
            _strings(raw.get("banned_scan_labels"), "policy.banned_scan_labels")
        ),
        capped_traversals=tuple(capped),
        create_allowed_globs=_strings(
            raw.get("create_allowed_globs"), "policy.create_allowed_globs"
        ),
        node_removals={
            str(name): _strings(names, f"policy.node_removals.{name}")
            for name, names in removals_raw.items()
        },
        vector_read_queries=_strings(raw.get("vector_read_queries"), "policy.vector_read_queries"),
        write_allowed_globs=_strings(raw.get("write_allowed_globs"), "policy.write_allowed_globs"),
        read_globs=_strings(raw.get("read_globs"), "policy.read_globs"),
        read_owners=_strings(raw.get("read_owners"), "policy.read_owners"),
        merge_scope_keys=tuple(
            _identifier(name, "property")
            for name in _strings(raw.get("merge_scope_keys"), "policy.merge_scope_keys")
        ),
        batch_merge_queries=_strings(raw.get("batch_merge_queries"), "policy.batch_merge_queries"),
        placeholder_labels=_placeholder_labels(raw.get("placeholder_labels")),
    )


def parse_schema(data: Mapping[str, Any]) -> GraphSchema:
    """A `GraphSchema` from the parsed YAML mapping; every identifier is checked."""
    nodes_raw = data.get("nodes")
    if not isinstance(nodes_raw, Mapping) or not nodes_raw:
        message = "nodes must map at least one label to its properties"
        raise SchemaError(message)
    nodes = {
        _identifier(label, "label"): NodeLabel(
            label,
            _properties(
                (spec or {}).get("properties") if isinstance(spec, Mapping) else None, label
            ),
        )
        for label, spec in nodes_raw.items()
    }
    common_label = data.get("common_label")
    constraints = data.get("constraints") or {}
    relationships = _relationships(data.get("relationships") or {})
    policy = _policy(data.get("policy"))
    for placeholder in policy.placeholder_labels:
        if placeholder not in nodes and placeholder not in relationships:
            message = (
                f"policy.placeholder_labels names {placeholder}, "
                "which is no declared label or relationship type"
            )
            raise SchemaError(message)
    return GraphSchema(
        nodes=nodes,
        relationships=relationships,
        common_label=None if common_label is None else _identifier(common_label, "label"),
        common_properties=_properties(data.get("common_properties"), "common_properties"),
        unique=_pairs(constraints.get("unique"), "constraints.unique"),
        indexes=_pairs(constraints.get("indexes"), "constraints.indexes"),
        policy=policy,
    )


def load_schema(path: Path | str) -> GraphSchema:
    """Read and validate one schema file."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        message = f"cannot read schema {path}: {exc}"
        raise SchemaError(message) from exc
    if not isinstance(data, Mapping):
        message = f"schema {path} is not a mapping"
        raise SchemaError(message)
    return parse_schema(data)
