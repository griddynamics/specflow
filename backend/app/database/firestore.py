"""
Firestore database implementation for production.

Uses Google Cloud Firestore SDK to connect to production Firestore database.
Supports all Firestore features including transactions, queries, and real-time updates.
"""

from typing import Any, Callable, Dict, List, Optional, TypeVar

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from google.cloud.firestore_v1.types import StructuredQuery

from app.state.db_adapter import COL_API_KEYS, COL_GENERATION_SESSIONS, COL_WORKSPACES

from app.database.interface import (
    DocumentNotFoundError,
    FilterTuple,
    IDatabase,
    ITransactionContext,
)

T = TypeVar("T")


class FirestoreTransactionContext(ITransactionContext):
    """Transaction context for Firestore database."""

    def __init__(self, transaction: firestore.Transaction, client: firestore.Client):
        """
        Initialize transaction context.
        
        Args:
            transaction: Firestore transaction object
            client: Firestore client for document references
        """
        self._transaction = transaction
        self._client = client

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Read document in transaction."""
        doc_ref = self._client.collection(collection).document(doc_id)
        snapshot = doc_ref.get(transaction=self._transaction)
        
        if not snapshot.exists:
            return None
        
        return snapshot.to_dict()

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Write document in transaction."""
        doc_ref = self._client.collection(collection).document(doc_id)
        self._transaction.set(doc_ref, data)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """
        Update document in transaction.
        
        Note: Firestore doesn't allow reading a document after writing it in a transaction,
        so we cannot verify existence here. The transaction will fail if the document
        doesn't exist, which is the desired behavior.
        """
        doc_ref = self._client.collection(collection).document(doc_id)
        self._transaction.update(doc_ref, data)

    def delete(self, collection: str, doc_id: str) -> None:
        """Delete document in transaction."""
        doc_ref = self._client.collection(collection).document(doc_id)
        self._transaction.delete(doc_ref)

    def get_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        ref = (
            self._client.collection(parent_collection)
            .document(parent_doc_id)
            .collection(subcollection)
            .document(doc_id)
        )
        snap = ref.get(transaction=self._transaction)
        if not snap.exists:
            return None
        return snap.to_dict()

    def set_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
        data: Dict[str, Any],
    ) -> None:
        ref = (
            self._client.collection(parent_collection)
            .document(parent_doc_id)
            .collection(subcollection)
            .document(doc_id)
        )
        self._transaction.set(ref, data, merge=True)


class FirestoreDatabase(IDatabase):
    """
    Firestore database implementation for production.
    
    Connects to GCP Firestore using credentials from environment or service account.
    Supports all Firestore features including transactions and complex queries.
    """

    def __init__(self, project_id: Optional[str] = None, database: str = "default"):
        """
        Initialize Firestore client.

        Args:
            project_id: GCP project ID. If None, uses default from credentials.
            database: Firestore database name. "default" is normalized to "(default)" (the Firestore
                default database). Any other value is passed as-is for named databases.
        """
        # The Firestore SDK uses "(default)" for the default database. Normalize the
        # user-friendly alias so FIRESTORE_DATABASE_NAME=default is a no-op vs previous behavior.
        sdk_database = "(default)" if database == "default" else database
        self._client = firestore.Client(project=project_id, database=sdk_database)

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get document by ID."""
        doc_ref = self._client.collection(collection).document(doc_id)
        snapshot = doc_ref.get()
        
        if not snapshot.exists:
            return None
        
        return snapshot.to_dict()

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Create or overwrite document."""
        doc_ref = self._client.collection(collection).document(doc_id)
        doc_ref.set(data)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Update fields in existing document."""
        doc_ref = self._client.collection(collection).document(doc_id)
        
        # Verify document exists
        if not doc_ref.get().exists:
            raise DocumentNotFoundError(collection, doc_id)
        
        doc_ref.update(data)

    def delete(self, collection: str, doc_id: str) -> None:
        """Delete document."""
        doc_ref = self._client.collection(collection).document(doc_id)
        doc_ref.delete()

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query documents with filters and ordering."""
        query = self._client.collection(collection)
        
        # Apply filters
        if filters:
            for field, operator, value in filters:
                query = self._apply_filter(query, field, operator, value)
        
        # Apply ordering
        if order_by:
            if order_by.startswith("-"):
                # Descending order
                field = order_by[1:]
                query = query.order_by(field, direction=StructuredQuery.Direction.DESCENDING)
            else:
                # Ascending order
                query = query.order_by(order_by, direction=StructuredQuery.Direction.ASCENDING)
        
        # Apply limit
        if limit:
            query = query.limit(limit)
        
        # Execute query and return results
        results = []
        for doc in query.stream():
            data = doc.to_dict()
            data["_id"] = doc.id
            results.append(data)
        
        return results

    def run_transaction(self, callback: Callable[[ITransactionContext], T]) -> T:
        """Execute callback in atomic transaction."""
        @firestore.transactional
        def firestore_callback(transaction: firestore.Transaction) -> T:
            tx = FirestoreTransactionContext(transaction, self._client)
            return callback(tx)
        
        transaction = self._client.transaction()
        return firestore_callback(transaction)

    def array_union(
        self, collection: str, doc_id: str, field: str, values: List[Any]
    ) -> None:
        """Add values to array field (no duplicates)."""
        doc_ref = self._client.collection(collection).document(doc_id)
        
        # Verify document exists
        if not doc_ref.get().exists:
            raise DocumentNotFoundError(collection, doc_id)
        
        # Use Firestore's built-in ArrayUnion
        doc_ref.update({field: firestore.ArrayUnion(values)})

    def list_subcollection(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
    ) -> List[Dict[str, Any]]:
        col = (
            self._client.collection(parent_collection)
            .document(parent_doc_id)
            .collection(subcollection)
        )
        out: List[Dict[str, Any]] = []
        for snap in col.stream():
            row = snap.to_dict() or {}
            row["_id"] = snap.id
            out.append(row)
        return out

    def server_timestamp(self) -> Any:
        """Get server timestamp sentinel."""
        return firestore.SERVER_TIMESTAMP

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        """Return the api_keys document whose key_uid field matches."""
        results = self.query("api_keys", filters=[("key_uid", "==", key_uid)])
        return results[0] if results else None

    def clear_all(self, collections: Optional[List[str]] = None) -> None:
        """
        Clear all documents from specified collections.
        
        WARNING: This is a destructive operation. Use only in test environments.
        
        Args:
            collections: List of collection names to clear. If None, clears commonly
                        used collections (api_keys, generations, workspaces).
        
        Example:
            >>> db = EmulatorDatabase()
            >>> db.clear_all()  # Clear all test data
            >>> db.clear_all(["api_keys"])  # Clear only api_keys collection
        """
        if collections is None:
            # Default collections to clear in tests
            collections = [COL_API_KEYS, COL_GENERATION_SESSIONS, COL_WORKSPACES]
        
        for collection_name in collections:
            collection_ref = self._client.collection(collection_name)
            
            # Delete all documents in the collection
            docs = collection_ref.stream()
            for doc in docs:
                doc.reference.delete()

    def _apply_filter(self, query, field: str, operator: str, value: Any):
        """
        Apply a single filter to Firestore query.
        
        Converts our generic filter format to Firestore where() clauses.
        Uses FieldFilter with the filter keyword argument to avoid deprecation warnings.
        """
        match operator:
            case "==":
                return query.where(filter=FieldFilter(field, "==", value))
            case "!=":
                return query.where(filter=FieldFilter(field, "!=", value))
            case "<":
                return query.where(filter=FieldFilter(field, "<", value))
            case "<=":
                return query.where(filter=FieldFilter(field, "<=", value))
            case ">":
                return query.where(filter=FieldFilter(field, ">", value))
            case ">=":
                return query.where(filter=FieldFilter(field, ">=", value))
            case "in":
                return query.where(filter=FieldFilter(field, "in", value))
            case "array_contains":
                return query.where(filter=FieldFilter(field, "array_contains", value))
            case _:
                raise ValueError(f"Unsupported operator: {operator}")
