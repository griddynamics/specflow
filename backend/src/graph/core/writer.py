"""Owned writes: idempotent, ownership-checked node and relationship writes (api.md "Owned writes").

Keys (docs/graph/data-model.md):

- A node is addressed by ``NodeRef(label, key)``. For a declared label the key is the node's
  ``uuid``; for ``Document`` it is ``document_id``; for ``Fragment`` it is ``fragment_id``. A label
  owned by ``build`` is a spec node: stored with ``SpecNode`` plus that one domain label. A declared
  label of another owner is a foreign node: stored with its own label only, never ``SpecNode``.
- A relationship is addressed by its type and ``key``, stored as its ``edge_key`` property: for a
  domain edge the data-model edge key (``<source identity key>|<RELATION>|<target identity key>``),
  for any other declared type a deterministic key its owner chooses. At most one relationship per
  type and key: an ``edge_key`` names one pair of endpoints. Only relation types the ontology
  declares are written here (each has a ``(group_id, edge_key)`` index); the built-in structural
  relationships (``CONTAINS``, ``REFERENCED_BY``, ``MENTIONED_IN``) have no ``edge_key`` and are
  written by build's own persist queries, so this writer refuses them.

Every operation first checks that the label or type belongs to the writer's owner
(``Ontology.owner_of``; built-in names belong to ``build``) and raises ``OwnershipError`` before
writing anything otherwise (W1). Writes stamp ``group_id`` (W5), MERGE on the key so a repeat is a
no-op, and set only the given keys with ``SET += ``: other properties stay, a ``None`` value
removes that property. W2 (a write sets only its owner's properties) is the caller's
responsibility: the writer does not know which property belongs to which owner, so a caller must
pass only its own properties. Each operation is its own transaction unless run inside
``transaction()``.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from graph.core.db import Executor, ScopedTransaction
from graph.core.errors import ConfigurationError, ConformanceError, OwnershipError
from graph.core.identity import validate_project_id
from graph.core.ontology import BUILT_IN_RELATIONS, Ontology
from graph.core.queries import (
    DELETE_SPEC_NODE,
    MOVE_FOREIGN_RELATIONSHIPS,
    SPEC_NODE,
    SPEC_NODE_EXISTS,
    SPEC_NODE_RELATIONSHIP_TYPES,
    UPSERT_DOCUMENT,
    UPSERT_FRAGMENT,
    UPSERT_SPEC_NODE,
    NodeShape,
    Query,
    delete_keyed_node,
    delete_relationship,
    keyed_node_relationship_types,
    upsert_foreign_node,
    upsert_relationship,
)
from logger.logger import DEBUG, get_logger, log_event

logger = get_logger("graph.core.writer")

OWNERS: Final = frozenset({"build", "gap", "plan"})
BUILD: Final = "build"
DOCUMENT: Final = "Document"
FRAGMENT: Final = "Fragment"
SPEC_KEY: Final = "uuid"
EDGE_KEY: Final = "edge_key"
GROUP_ID: Final = "group_id"
_KEYED_LABELS: Final = {DOCUMENT: "document_id", FRAGMENT: "fragment_id"}
_SPEC_SHAPE: Final = NodeShape(SPEC_NODE, SPEC_KEY, check_label=True)


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A node of the project: its label and key (``uuid``, or ``document_id``/``fragment_id``)."""

    label: str
    key: str


class OwnedWriter:
    """Writes of one owner (``build``, ``gap`` or ``plan``) in one project (W1-W5).

    The writer enforces W1 (owned labels and types only), W3 (``move_foreign_relationships``) and
    W5 (``group_id``). W2 is the caller's job: the writer sets exactly the property keys it is
    given (``SET +=``) and never checks whose they are, so pass only the owner's own properties.
    """

    def __init__(
        self,
        driver_or_executor: Any,
        ontology: Ontology,
        owner: str,
        project_id: str,
        *,
        database: str | None = None,
    ) -> None:
        if owner not in OWNERS:
            raise ConfigurationError("unknown_owner", owner=str(owner))
        self._executor = (
            driver_or_executor if isinstance(driver_or_executor, Executor) else Executor(driver_or_executor, database)
        )
        self._ontology = ontology
        self._owner = owner
        self._project_id = validate_project_id(project_id)
        self.active_transaction: ScopedTransaction | None = None
        """The open group transaction of a writer yielded by ``transaction()``; None on the writer itself."""
        self._spec_labels = frozenset(label for label in ontology.labels if ontology.owner_of(label) == BUILD)

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def project_id(self) -> str:
        return self._project_id

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[OwnedWriter]:
        """Group operations into one transaction: ``async with writer.transaction() as w: ...``.

        The transaction commits when the block ends and rolls back when it raises (including an
        ``OwnershipError``), so a refused operation leaves nothing of the group behind.
        """

        if self.active_transaction is not None:
            raise ConformanceError("writer_transaction_nested")
        async with self._executor.tx(self._project_id) as tx:
            bound = copy.copy(self)
            bound.active_transaction = tx
            yield bound

    # Nodes

    async def upsert_node(self, label: str, key: str, properties: Mapping[str, Any]) -> None:
        """Create or update the node ``label``/``key``; set only ``properties``.

        Only the given keys are written (``SET +=``); a ``None`` value removes that property. The
        writer does not check that the keys belong to its owner: W2 is the caller's responsibility.
        A spec node gets ``SpecNode`` and its domain label at creation; labels never change, so an
        existing node with this ``uuid`` and another domain label raises
        ``ConformanceError("node_label_mismatch")``. ``uuid`` and ``spec_id`` are fixed at creation
        (data-model.md): ``uuid`` is the key (``property_reserved`` in ``properties``), and a
        ``spec_id`` that differs from the node's existing one (or ``None`` for it) raises
        ``ConformanceError("spec_id_immutable")``. Either way nothing is written.
        """

        self._require_owned(label)
        shape = self._shape(label)
        props = self._properties(properties, shape.key)
        if not shape.check_label:
            rows = await self._write(self._keyed_upsert(shape.label), key=key, props=props)
            if not rows or rows[0]["written"] == 0:
                raise ConformanceError("node_label_mismatch", label=label)
            return
        rows = await self._write(UPSERT_SPEC_NODE, key=key, label=label, props=props)
        if not rows or rows[0]["label_mismatch"]:
            raise ConformanceError("node_label_mismatch", label=label)
        if rows[0]["spec_id_changed"]:
            raise ConformanceError("spec_id_immutable", label=label)

    async def delete_node(self, label: str, key: str) -> bool:
        """Delete the node and its relationships; absent is a no-op. Return whether it existed.

        A writer other than ``build`` may not delete a node that has relationships of another owner
        (``OwnershipError``).
        """

        self._require_owned(label)
        shape = self._shape(label)
        if shape.check_label:
            types_query, delete_query = SPEC_NODE_RELATIONSHIP_TYPES, DELETE_SPEC_NODE
        else:
            types_query = keyed_node_relationship_types(shape.label, shape.key)
            delete_query = delete_keyed_node(shape.label, shape.key)
        async with self._atomic() as tx:
            if self._owner != BUILD:
                rows = await tx.run(types_query, n_key=key, n_label=label)
                foreign = self._foreign_types(rows[0]["types"] if rows else [])
                if foreign:
                    raise OwnershipError(
                        "node_has_foreign_relationships",
                        owner=self._owner,
                        label=label,
                        types=",".join(foreign),
                    )
            rows = await tx.run(delete_query, key=key, label=label)
        return bool(rows and rows[0]["deleted"])

    # Relationships

    async def upsert_relationship(
        self,
        rel_type: str,
        source: NodeRef,
        target: NodeRef,
        key: str,
        properties: Mapping[str, Any],
    ) -> None:
        """Create or update relationship ``rel_type`` with ``edge_key = key`` from source to target.

        ``rel_type`` must be a declared relation (a built-in structural type raises
        ``ConformanceError("structural_relationship_not_supported")``), both nodes must exist and
        the pair must be legal for the type (``ConformanceError`` otherwise); only ``properties``
        are set (W2 is the caller's responsibility). An ``edge_key`` names one pair of endpoints:
        when a ``rel_type`` relationship with this key already joins other nodes, the write raises
        ``ConformanceError("edge_key_endpoint_mismatch")`` and changes nothing. Build re-keys an
        edge (an endpoint changed) by deleting the old relationship first.
        """

        self._refuse_structural(rel_type)
        self._require_owned(rel_type)
        self._require_legal(rel_type, source.label, target.label)
        source_shape, target_shape = self._shape(source.label), self._shape(target.label)
        rows = await self._write(
            upsert_relationship(rel_type, source_shape, target_shape),
            key=key,
            props=self._properties(properties, EDGE_KEY),
            s_key=source.key,
            s_label=source.label,
            t_key=target.key,
            t_label=target.label,
        )
        if not rows:
            raise ConformanceError("relationship_end_missing", relation=rel_type)
        if rows[0]["conflict"]:
            raise ConformanceError("edge_key_endpoint_mismatch", relation=rel_type)

    async def delete_relationship(self, rel_type: str, key: str) -> bool:
        """Delete the ``rel_type`` relationship with ``edge_key = key``; absent is a no-op.

        A built-in structural type raises ``ConformanceError("structural_relationship_not_supported")``.
        """

        self._refuse_structural(rel_type)
        self._require_owned(rel_type)
        self._require_relation(rel_type)
        rows = await self._write(delete_relationship(rel_type), key=key)
        return bool(rows and rows[0]["deleted"])

    async def move_foreign_relationships(self, from_node: NodeRef, to_node: NodeRef) -> int:
        """Build only (W3): move each relationship of ``from_node`` that build does not own.

        Each moved relationship keeps its type, direction, other end and properties; one equal to a
        relationship ``to_node`` already has is not duplicated; a self-loop of ``from_node`` becomes
        a self-loop of ``to_node``; one between the two nodes is deleted, not turned into a
        self-loop. Both must be spec nodes and ``to_node`` must exist. Returns the number of
        relationships taken off ``from_node`` (0 when both refs name one node).
        """

        if self._owner != BUILD:
            raise OwnershipError("move_requires_build", owner=self._owner)
        for node in (from_node, to_node):
            if node.label not in self._spec_labels:
                raise ConformanceError("move_needs_spec_nodes", label=node.label)
        owned = sorted(
            BUILT_IN_RELATIONS | {name for name in self._ontology.relations if self._ontology.owner_of(name) == BUILD}
        )
        async with self._atomic() as tx:
            found = await tx.run(SPEC_NODE_EXISTS, n_key=to_node.key, n_label=to_node.label)
            if not found or found[0]["found"] == 0:
                raise ConformanceError("move_target_missing", label=to_node.label)
            rows = await tx.run(
                MOVE_FOREIGN_RELATIONSHIPS,
                from_key=from_node.key,
                from_label=from_node.label,
                to_key=to_node.key,
                to_label=to_node.label,
                owned_types=owned,
            )
        moved = int(rows[0]["moved"]) if rows else 0
        log_event(logger, DEBUG, "graph.writer.relationships_moved", moved=moved)
        return moved

    # Checks and plumbing

    def _foreign_types(self, type_names: Iterable[str]) -> list[str]:
        """Return the sorted relationship types in ``type_names`` that another writer owns."""

        return sorted(name for name in type_names if self._ontology.owner_of(name) != self._owner)

    def _require_owned(self, name: str) -> None:
        if self._ontology.owner_of(name) != self._owner:
            raise OwnershipError("type_not_owned", owner=self._owner, name=name)

    @staticmethod
    def _refuse_structural(rel_type: str) -> None:
        """The built-in structural types have no ``edge_key``; build's persist queries write them."""

        if rel_type in BUILT_IN_RELATIONS:
            raise ConformanceError("structural_relationship_not_supported", relation=rel_type)

    def _require_relation(self, rel_type: str) -> None:
        if rel_type not in self._ontology.relations:
            raise ConformanceError("not_a_relationship_type", name=rel_type)

    def _require_legal(self, rel_type: str, source_label: str, target_label: str) -> None:
        self._require_relation(rel_type)
        if not self._ontology.is_legal(rel_type, source_label, target_label):
            raise ConformanceError(
                "relationship_pair_illegal",
                relation=rel_type,
                source=source_label,
                target=target_label,
            )

    def _shape(self, label: str) -> NodeShape:
        """How a node of ``label`` is stored and found."""

        if label in _KEYED_LABELS:
            return NodeShape(label, _KEYED_LABELS[label])
        if label in self._spec_labels:
            return _SPEC_SHAPE
        if label == SPEC_NODE or label not in self._ontology.labels:
            raise ConformanceError("not_a_node_label", label=label)
        return NodeShape(label, SPEC_KEY)

    @staticmethod
    def _keyed_upsert(label: str) -> Query:
        """The upsert of a node without domain label, MERGEd on its uniquely constrained key."""

        if label == DOCUMENT:
            return UPSERT_DOCUMENT
        if label == FRAGMENT:
            return UPSERT_FRAGMENT
        return upsert_foreign_node(label)

    @staticmethod
    def _properties(properties: Mapping[str, Any], key_property: str) -> dict[str, Any]:
        props = dict(properties)
        for reserved in (GROUP_ID, key_property):
            if reserved in props:
                raise ConformanceError("property_reserved", name=reserved)
        return props

    async def _write(self, query: Query, /, **params: Any) -> list[dict[str, Any]]:
        if self.active_transaction is not None:
            return await self.active_transaction.run(query, **params)
        return await self._executor.write(query, self._project_id, **params)

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[ScopedTransaction]:
        """The open group transaction, or a new one for an operation of several queries."""

        if self.active_transaction is not None:
            yield self.active_transaction
            return
        async with self._executor.tx(self._project_id) as tx:
            yield tx
