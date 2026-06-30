"""
In-memory database implementation for testing.

Provides a fast, dict-based database for unit tests that don't require
external dependencies. Data is stored in Python dicts and cleared between tests.
"""

import threading
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.database.interface import (
    DocumentNotFoundError,
    FilterTuple,
    IDatabase,
    ITransactionContext,
)

T = TypeVar("T")


def _apply_dot_notation_update(doc: Dict[str, Any], key: str, value: Any) -> None:
    """Write *value* into *doc* using a Firestore-style dot-notation *key*.

    Plain keys are written directly.  Dotted keys (e.g. ``"map.sub.field"``)
    walk (and create) intermediate dicts.  Mirrors Firestore: if an intermediate
    node exists but is not a dict, raises ValueError instead of silently
    overwriting it.
    """
    if "." not in key:
        doc[key] = value
        return
    parts = key.split(".")
    target = doc
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        elif not isinstance(target[part], dict):
            raise ValueError(
                f"Cannot set '{key}': intermediate field '{part}' is not a map"
            )
        target = target[part]
    target[parts[-1]] = value


class _ServerTimestamp:
    """Sentinel class for server timestamps in memory database."""

    pass


class InMemoryTransactionContext(ITransactionContext):
    """Transaction context for in-memory database."""

    def __init__(
        self,
        data: Dict[str, Dict[str, Dict[str, Any]]],
        subcollection_data: Dict[tuple, Dict[str, Any]],
    ):
        """
        Initialize transaction context.
        
        Args:
            data: Reference to the main data store
            subcollection_data: Reference to nested doc store keyed by
                ``(parent_collection, parent_doc_id, subcollection, doc_id)``
        """
        self._data = data
        self._subcollection_data = subcollection_data
        self._reads: Dict[tuple, Any] = {}  # Track reads for consistency
        self._writes: List[tuple] = []  # Track writes to apply atomically
        self._sub_overlay: Dict[tuple, Dict[str, Any]] = {}

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Read document in transaction."""
        key = (collection, doc_id)
        
        # Check if we've already read this in the transaction
        if key in self._reads:
            return self._reads[key]
        
        # Read from main data store
        result = self._data.get(collection, {}).get(doc_id)
        self._reads[key] = result.copy() if result else None
        return self._reads[key]

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Write document in transaction."""
        self._writes.append(("set", collection, doc_id, data.copy()))

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Update document in transaction."""
        # Verify document exists
        existing = self.get(collection, doc_id)
        if existing is None:
            raise DocumentNotFoundError(collection, doc_id)
        
        self._writes.append(("update", collection, doc_id, data.copy()))

    def delete(self, collection: str, doc_id: str) -> None:
        """Delete document in transaction."""
        self._writes.append(("delete", collection, doc_id, None))

    def get_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        key = (parent_collection, parent_doc_id, subcollection, doc_id)
        if key in self._sub_overlay:
            return self._sub_overlay[key].copy()
        base = self._subcollection_data.get(key)
        return base.copy() if base else None

    def set_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
        data: Dict[str, Any],
    ) -> None:
        key = (parent_collection, parent_doc_id, subcollection, doc_id)
        d = self._process_timestamps(data.copy())
        self._sub_overlay[key] = d
        self._writes.append(("sub_set", key, d))

    def _apply_writes(self) -> None:
        """Apply all writes atomically."""
        for write in self._writes:
            op = write[0]
            if op == "sub_set":
                _, sk, sdata = write
                self._subcollection_data[sk] = sdata.copy()
                continue

            _, collection, doc_id, data = write

            if collection not in self._data:
                self._data[collection] = {}

            if op == "set":
                # Replace server timestamps with actual timestamps
                processed_data = self._process_timestamps(data)
                self._data[collection][doc_id] = processed_data
            elif op == "update":
                if doc_id not in self._data[collection]:
                    raise DocumentNotFoundError(collection, doc_id)
                processed_data = self._process_timestamps(data)
                doc = self._data[collection][doc_id]
                for k, v in processed_data.items():
                    _apply_dot_notation_update(doc, k, v)
            elif op == "delete":
                self._data[collection].pop(doc_id, None)

    def _process_timestamps(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace server timestamp sentinels with actual timestamps."""
        result = {}
        for key, value in data.items():
            if isinstance(value, _ServerTimestamp):
                result[key] = datetime.now(UTC).replace(tzinfo=None)
            elif isinstance(value, dict):
                result[key] = self._process_timestamps(value)
            else:
                result[key] = value
        return result


class InMemoryDatabase(IDatabase):
    """
    In-memory database implementation for testing.
    
    Stores data in Python dicts with structure: {collection: {doc_id: data}}
    Thread-safe using locks for concurrent access.
    """

    def __init__(self):
        """Initialize empty in-memory database."""
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._subcollection_data: Dict[tuple, Dict[str, Any]] = {}
        self._lock = threading.RLock()  # Reentrant lock for nested operations

    def clear(self) -> None:
        """Clear all data from database. Useful for test cleanup."""
        with self._lock:
            self._data.clear()
            self._subcollection_data.clear()

    def clear_all(self, collections: Optional[List[str]] = None) -> None:
        """
        Clear all documents from specified collections.
        
        Args:
            collections: List of collection names to clear. If None, clears all collections.
        
        Example:
            >>> db = InMemoryDatabase()
            >>> db.clear_all()  # Clear all collections
            >>> db.clear_all(["api_keys"])  # Clear only api_keys collection
        """
        with self._lock:
            if collections is None:
                # Clear all collections
                self._data.clear()
            else:
                # Clear only specified collections
                for collection in collections:
                    if collection in self._data:
                        self._data[collection].clear()
                    # Subcollections parented under cleared top-level collections
                    to_del = [
                        k
                        for k in self._subcollection_data
                        if isinstance(k, tuple) and len(k) >= 1 and k[0] in collections
                    ]
                    for k in to_del:
                        self._subcollection_data.pop(k, None)

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        with self._lock:
            result = self._data.get(collection, {}).get(doc_id)
            return result.copy() if result else None

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Create or overwrite document."""
        with self._lock:
            if collection not in self._data:
                self._data[collection] = {}
            
            # Process server timestamps
            processed_data = self._process_timestamps(data)
            self._data[collection][doc_id] = processed_data

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Update fields in existing document. Dot-notation keys (e.g. 'map.key') update
        nested fields without overwriting sibling keys, matching Firestore semantics."""
        with self._lock:
            if collection not in self._data or doc_id not in self._data[collection]:
                raise DocumentNotFoundError(collection, doc_id)

            processed_data = self._process_timestamps(data)
            doc = self._data[collection][doc_id]
            for key, value in processed_data.items():
                _apply_dot_notation_update(doc, key, value)

    def delete(self, collection: str, doc_id: str) -> None:
        """Delete document."""
        with self._lock:
            if collection in self._data:
                self._data[collection].pop(doc_id, None)

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query documents with filters and ordering."""
        with self._lock:
            # Get all documents in collection
            docs = []
            for doc_id, doc_data in self._data.get(collection, {}).items():
                doc_copy = doc_data.copy()
                doc_copy["_id"] = doc_id
                docs.append(doc_copy)
            
            # Apply filters
            if filters:
                for field, operator, value in filters:
                    docs = self._apply_filter(docs, field, operator, value)
            
            # Apply ordering
            if order_by:
                descending = order_by.startswith("-")
                field = order_by[1:] if descending else order_by
                docs.sort(
                    key=lambda d: self._get_nested_field(d, field),
                    reverse=descending
                )
            
            # Apply limit
            if limit:
                docs = docs[:limit]
            
            return docs

    def run_transaction(self, callback: Callable[[ITransactionContext], T]) -> T:
        """Execute callback in atomic transaction."""
        with self._lock:
            tx = InMemoryTransactionContext(self._data, self._subcollection_data)
            
            try:
                result = callback(tx)
                tx._apply_writes()
                return result
            except Exception:
                # Transaction failed, don't apply writes
                raise

    def array_union(
        self, collection: str, doc_id: str, field: str, values: List[Any]
    ) -> None:
        """Add values to array field (no duplicates)."""
        with self._lock:
            if collection not in self._data or doc_id not in self._data[collection]:
                raise DocumentNotFoundError(collection, doc_id)
            
            doc = self._data[collection][doc_id]
            
            # Initialize array if it doesn't exist
            if field not in doc:
                doc[field] = []
            
            # Add values that don't already exist.
            # Use a set for hashable types (fast); fall back to linear scan for
            # unhashable types like dicts (state_history entries).
            existing = doc[field] if isinstance(doc[field], list) else []
            try:
                existing_set = set(existing)
                for value in values:
                    if value not in existing_set:
                        doc[field].append(value)
                        existing_set.add(value)
            except TypeError:
                for value in values:
                    if value not in existing:
                        doc[field].append(value)

    def list_subcollection(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for key, doc in self._subcollection_data.items():
                if (
                    isinstance(key, tuple)
                    and len(key) == 4
                    and key[0] == parent_collection
                    and key[1] == parent_doc_id
                    and key[2] == subcollection
                ):
                    row = doc.copy()
                    row["_id"] = key[3]
                    out.append(row)
            return out

    def server_timestamp(self) -> Any:
        """Get server timestamp sentinel."""
        return _ServerTimestamp()

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        """Return the api_keys document whose key_uid field matches."""
        with self._lock:
            for doc_id, doc_data in self._data.get("api_keys", {}).items():
                if doc_data.get("key_uid") == key_uid:
                    result = doc_data.copy()
                    result["_id"] = doc_id
                    return result
        return None

    def _process_timestamps(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Replace server timestamp sentinels with actual timestamps."""
        result = {}
        for key, value in data.items():
            if isinstance(value, _ServerTimestamp):
                result[key] = datetime.now(UTC).replace(tzinfo=None)
            elif isinstance(value, dict):
                result[key] = self._process_timestamps(value)
            else:
                result[key] = value
        return result

    def _apply_filter(
        self, docs: List[Dict[str, Any]], field: str, operator: str, value: Any
    ) -> List[Dict[str, Any]]:
        """
        Apply a single filter to document list.
        
        Note: This manual filtering is only needed for in-memory database (unit tests).
        Real Firestore implementations use the native query engine.
        """
        filtered = []
        
        for doc in docs:
            field_value = self._get_nested_field(doc, field)
            
            match operator:
                case "==":
                    if field_value == value:
                        filtered.append(doc)
                case "!=":
                    if field_value != value:
                        filtered.append(doc)
                case "<":
                    if field_value is not None and field_value < value:
                        filtered.append(doc)
                case "<=":
                    if field_value is not None and field_value <= value:
                        filtered.append(doc)
                case ">":
                    if field_value is not None and field_value > value:
                        filtered.append(doc)
                case ">=":
                    if field_value is not None and field_value >= value:
                        filtered.append(doc)
                case "in":
                    if field_value in value:
                        filtered.append(doc)
                case "array_contains":
                    if isinstance(field_value, list) and value in field_value:
                        filtered.append(doc)
        
        return filtered

    def _get_nested_field(self, doc: Dict[str, Any], field: str) -> Any:
        """Get nested field value using dot notation."""
        parts = field.split(".")
        value = doc
        
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        
        return value
