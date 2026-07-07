"""
Database abstraction interface.

Defines the contract that all database implementations must follow.
This allows the application to swap between different database backends
(Firestore, Emulator, In-memory) without changing application code.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, TypeVar, runtime_checkable

# Type for query filter: (field, operator, value)
FilterTuple = Tuple[str, str, Any]

# Type variable for transaction callback return type
T = TypeVar("T")


@runtime_checkable
class ReadOnlyDatabase(Protocol):
    """Read-only view over a database — the narrow subset a document reader needs.

    Consumers that must never write status/checkpoint/workspace_phases (e.g. the
    notifications report renderer) depend on this protocol instead of the full
    ``IDatabase``, so those writes are impossible through the handle they hold.
    Any ``IDatabase`` satisfies it structurally; ``StateMachineDBAdapter`` hands
    out a view that exposes *only* ``get`` — keeping Commandment VII enforced,
    not merely documented.
    """

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        ...


class ITransactionContext(ABC):
    """
    Transaction context interface for atomic database operations.
    
    All operations within a transaction are atomic - either all succeed or all fail.
    Transactions provide isolation and consistency guarantees.
    """

    @abstractmethod
    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        Read a document within the transaction.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            
        Returns:
            Document data as dict, or None if not found
        """
        pass

    @abstractmethod
    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """
        Create or overwrite a document within the transaction.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            data: Document data to write
        """
        pass

    @abstractmethod
    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """
        Update fields in an existing document within the transaction.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            data: Fields to update (partial document)
            
        Raises:
            DocumentNotFoundError: If document doesn't exist (behavior may vary by implementation)
        
        Note:
            Some implementations (like Firestore) don't allow reading a document after
            writing it in a transaction. Always perform all reads before any writes.
        """
        pass

    @abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """
        Delete a document within the transaction.
        
        Args:
            collection: Collection name
            doc_id: Document ID
        """
        pass

    @abstractmethod
    def get_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Read a document under ``parent_collection/parent_doc_id/subcollection/doc_id``."""

    @abstractmethod
    def set_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Create or replace a subcollection document (caller supplies merged full payload)."""


class IDatabase(ABC):
    """
    Database abstraction interface.
    
    Provides a unified API for document-based database operations.
    All implementations must support:
    - CRUD operations (get, set, update, delete)
    - Queries with filters and ordering
    - Atomic transactions
    - Array operations
    """

    @abstractmethod
    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a document by ID.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            
        Returns:
            Document data as dict, or None if not found
            
        Example:
            >>> db.get("generation_sessions", "est-123")
            {"status": "running", "created_at": ...}
        """
        pass

    @abstractmethod
    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """
        Create or overwrite a document.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            data: Document data to write
            
        Example:
            >>> db.set("generation_sessions", "est-123", {
            ...     "status": "pending",
            ...     "created_at": datetime.now(UTC)
            ... })
        """
        pass

    @abstractmethod
    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """
        Update fields in an existing document.
        
        Only the fields present in data will be updated. Other fields remain unchanged.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            data: Fields to update (partial document)
            
        Raises:
            DocumentNotFoundError: If document doesn't exist
            
        Example:
            >>> db.update("generation_sessions", "est-123", {"status": "running"})
        """
        pass

    @abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """
        Delete a document.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            
        Example:
            >>> db.delete("generation_sessions", "est-123")
        """
        pass

    @abstractmethod
    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query documents with filters and ordering.
        
        Args:
            collection: Collection name
            filters: List of (field, operator, value) tuples
            order_by: Field name to order by (prefix with '-' for descending)
            limit: Maximum number of documents to return
            
        Filter operators:
            - "==" : Equal
            - "!=" : Not equal
            - "<"  : Less than
            - "<=" : Less than or equal
            - ">"  : Greater than
            - ">=" : Greater than or equal
            - "in" : Value in list
            - "array_contains" : Array field contains value
            
        Returns:
            List of documents (each document includes "_id" field)
            
        Example:
            >>> # Find all running generations, newest first
            >>> db.query(
            ...     "generation_sessions",
            ...     filters=[("status", "==", "running")],
            ...     order_by="-created_at",
            ...     limit=10
            ... )
            [
                {"_id": "est-123", "status": "running", ...},
                {"_id": "est-124", "status": "running", ...}
            ]
        """
        pass

    @abstractmethod
    def run_transaction(self, callback: Callable[[ITransactionContext], T]) -> T:
        """
        Execute a callback function within an atomic transaction.
        
        The callback receives a transaction context and can perform multiple
        read and write operations. All operations are atomic - either all
        succeed or all fail.
        
        If a conflict is detected (e.g., another transaction modified the same
        document), the transaction will be automatically retried.
        
        **IMPORTANT RULE**: Always perform all reads before any writes in the transaction.
        Some implementations (like Firestore) don't allow reading after writing.
        
        Args:
            callback: Function to execute in transaction context
            
        Returns:
            Return value of the callback function
            
        Example:
            >>> def allocate_workspace(tx: ITransactionContext) -> str:
            ...     # 1. Read all documents FIRST
            ...     workspaces = db.query("workspaces", [("status", "==", "available")], limit=1)
            ...     if not workspaces:
            ...         raise ValueError("No workspaces available")
            ...     
            ...     ws_id = workspaces[0]["_id"]
            ...     
            ...     # 2. Then perform all writes
            ...     tx.update("workspaces", ws_id, {
            ...         "status": "allocated",
            ...         "locked_at": datetime.now(UTC)
            ...     })
            ...     
            ...     return ws_id
            ...
            >>> workspace_id = db.run_transaction(allocate_workspace)
        """
        pass

    @abstractmethod
    def array_union(
        self, collection: str, doc_id: str, field: str, values: List[Any]
    ) -> None:
        """
        Add values to an array field (no duplicates).
        
        If the array doesn't exist, it will be created. Values that already
        exist in the array will not be added again.
        
        Args:
            collection: Collection name
            doc_id: Document ID
            field: Array field name
            values: Values to add to the array
            
        Example:
            >>> db.array_union("generation_sessions", "est-123", "tags", ["urgent", "high-priority"])
        """
        pass

    @abstractmethod
    def list_subcollection(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
    ) -> List[Dict[str, Any]]:
        """
        List all documents in a subcollection.

        Each returned dict includes ``_id`` with the child document id.
        """

    @abstractmethod
    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        """
        Return the api_keys document whose key_uid field matches.

        This is the only sanctioned path for key_uid lookups. Callers must not
        issue raw db.query("api_keys", [("key_uid", "==", ...)]) directly.

        Args:
            key_uid: The stable non-secret UUID assigned at key-creation time.

        Returns:
            Document dict (including "_id") or None if not found.
        """
        pass


class DocumentNotFoundError(Exception):
    """Raised when attempting to update a document that doesn't exist."""

    def __init__(self, collection: str, doc_id: str):
        self.collection = collection
        self.doc_id = doc_id
        super().__init__(f"Document not found: {collection}/{doc_id}")


class TransactionConflictError(Exception):
    """Raised when a transaction conflict cannot be resolved after retries."""

    pass
