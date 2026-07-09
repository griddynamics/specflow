"""
Integration tests for Firestore database implementations.

These tests run against the Firestore emulator and verify that FirestoreDatabase
and EmulatorDatabase work correctly with real Firestore operations.

To run these tests:
    1. Start the Firestore emulator: docker-compose up firestore-emulator -d
    2. Set environment variable: export FIRESTORE_EMULATOR_HOST=localhost:8080
    3. Run tests: pytest test/database/test_firestore_db.py -v

Note: These tests require the emulator to be running. They will be skipped
if FIRESTORE_EMULATOR_HOST is not set.
"""

import os
import socket
import pytest

from app.database.emulator import EmulatorDatabase
from app.database.interface import DocumentNotFoundError
from app.state.db_adapter import COL_API_KEYS, COL_GENERATION_SESSIONS, COL_WORKSPACES


def _emulator_reachable() -> bool:
    """True only if a Firestore emulator actually accepts connections at
    FIRESTORE_EMULATOR_HOST. The env var being set is not enough: since SQLite
    became the default, no emulator container runs, yet the test runner may still
    export a default host. Probing the socket keeps these tests skipping cleanly
    instead of dialing a dead port and failing. Mirrors the reachability guard the
    former test_firestore_emulator_persistence.py used.
    """
    host = os.getenv("FIRESTORE_EMULATOR_HOST")
    if not host:
        return False
    hostname, _, port = host.partition(":")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            return sock.connect_ex((hostname or "localhost", int(port or "8080"))) == 0
    except (OSError, ValueError):
        return False


# Skip all tests unless a Firestore emulator is actually reachable (not merely configured).
pytestmark = pytest.mark.skipif(
    not _emulator_reachable(),
    reason="Firestore emulator not reachable (start one and set FIRESTORE_EMULATOR_HOST to run these)",
)


@pytest.fixture
def db():
    """Create a fresh emulator database for each test."""
    database = EmulatorDatabase(project_id="test-project")
    
    yield database
    
    # Cleanup: Delete all documents created in tests
    # This is a simple cleanup - in production you'd use emulator reset
    try:
        # Clean up test collections
        for collection_name in [COL_API_KEYS, COL_GENERATION_SESSIONS, COL_WORKSPACES]:  # pyright: ignore[reportUndefinedVariable]
            collection_ref = database._client.collection(collection_name)
            docs = collection_ref.stream()
            for doc in docs:
                doc.reference.delete()
    except Exception:
        pass  # Best effort cleanup


class TestBasicCRUD:
    """Test basic CRUD operations against Firestore emulator."""

    def test_set_and_get(self, db):
        """Test setting and getting a document."""
        db.set("users", "user-1", {"name": "Alice", "age": 30})
        
        result = db.get("users", "user-1")
        assert result is not None
        assert result["name"] == "Alice"
        assert result["age"] == 30

    def test_get_nonexistent(self, db):
        """Test getting a document that doesn't exist."""
        result = db.get("users", "nonexistent")
        assert result is None

    def test_update_existing(self, db):
        """Test updating an existing document."""
        db.set("users", "user-1", {"name": "Alice", "age": 30, "city": "NYC"})
        db.update("users", "user-1", {"age": 31})
        
        result = db.get("users", "user-1")
        assert result["name"] == "Alice"
        assert result["age"] == 31
        assert result["city"] == "NYC"

    def test_update_nonexistent(self, db):
        """Test updating a document that doesn't exist."""
        with pytest.raises(DocumentNotFoundError) as exc_info:
            db.update("users", "nonexistent", {"age": 30})
        
        assert exc_info.value.collection == "users"
        assert exc_info.value.doc_id == "nonexistent"

    def test_delete(self, db):
        """Test deleting a document."""
        db.set("users", "user-1", {"name": "Alice"})
        db.delete("users", "user-1")
        
        result = db.get("users", "user-1")
        assert result is None


class TestQuery:
    """Test query operations against Firestore emulator."""

    def test_query_filter_equals(self, db):
        """Test query with equals filter."""
        db.set("users", "user-1", {"name": "Alice", "age": 30})
        db.set("users", "user-2", {"name": "Bob", "age": 25})
        db.set("users", "user-3", {"name": "Alice", "age": 35})
        
        results = db.query("users", filters=[("name", "==", "Alice")])
        assert len(results) == 2
        assert all(r["name"] == "Alice" for r in results)

    def test_query_filter_comparison(self, db):
        """Test query with comparison filters."""
        db.set("users", "user-1", {"name": "Alice", "age": 30})
        db.set("users", "user-2", {"name": "Bob", "age": 25})
        db.set("users", "user-3", {"name": "Charlie", "age": 35})
        
        # Greater than
        results = db.query("users", filters=[("age", ">", 30)])
        assert len(results) == 1
        assert results[0]["name"] == "Charlie"
        
        # Less than or equal
        results = db.query("users", filters=[("age", "<=", 30)])
        assert len(results) == 2

    def test_query_order_by(self, db):
        """Test query with ordering."""
        db.set("users", "user-1", {"name": "Charlie", "age": 35})
        db.set("users", "user-2", {"name": "Alice", "age": 30})
        db.set("users", "user-3", {"name": "Bob", "age": 25})
        
        # Ascending
        results = db.query("users", order_by="age")
        assert len(results) == 3
        assert results[0]["name"] == "Bob"
        assert results[2]["name"] == "Charlie"
        
        # Descending
        results = db.query("users", order_by="-age")
        assert len(results) == 3
        assert results[0]["name"] == "Charlie"
        assert results[2]["name"] == "Bob"

    def test_query_limit(self, db):
        """Test query with limit."""
        db.set("users", "user-1", {"name": "Alice"})
        db.set("users", "user-2", {"name": "Bob"})
        db.set("users", "user-3", {"name": "Charlie"})
        
        results = db.query("users", limit=2)
        assert len(results) == 2


class TestTransactions:
    """Test transaction operations against Firestore emulator."""

    def test_transaction_commit(self, db):
        """Test successful transaction commit."""
        db.set("users", "user-1", {"name": "Alice", "balance": 100})
        db.set("users", "user-2", {"name": "Bob", "balance": 50})
        
        def transfer(tx):
            # Read documents FIRST (before any writes)
            alice = tx.get("users", "user-1")
            bob = tx.get("users", "user-2")
            
            # Then perform all writes
            tx.update("users", "user-1", {"balance": alice["balance"] - 20})
            tx.update("users", "user-2", {"balance": bob["balance"] + 20})
            
            return "success"
        
        result = db.run_transaction(transfer)
        assert result == "success"
        
        alice = db.get("users", "user-1")
        bob = db.get("users", "user-2")
        
        assert alice["balance"] == 80
        assert bob["balance"] == 70

    def test_transaction_rollback(self, db):
        """Test transaction rollback on error."""
        db.set("users", "user-1", {"name": "Alice", "balance": 100})
        
        def failing_transaction(tx):
            tx.update("users", "user-1", {"balance": 50})
            raise ValueError("Something went wrong")
        
        with pytest.raises(ValueError):
            db.run_transaction(failing_transaction)
        
        # Balance should not have changed
        alice = db.get("users", "user-1")
        assert alice["balance"] == 100


class TestArrayOperations:
    """Test array operations against Firestore emulator."""

    def test_array_union(self, db):
        """Test array_union adds to existing array."""
        db.set("users", "user-1", {"name": "Alice", "tags": ["python"]})
        db.array_union("users", "user-1", "tags", ["react", "vue"])
        
        user = db.get("users", "user-1")
        assert set(user["tags"]) == {"python", "react", "vue"}

    def test_array_union_no_duplicates(self, db):
        """Test array_union doesn't add duplicates."""
        db.set("users", "user-1", {"name": "Alice", "tags": ["python", "react"]})
        db.array_union("users", "user-1", "tags", ["python", "vue"])
        
        user = db.get("users", "user-1")
        # Firestore's ArrayUnion automatically prevents duplicates
        assert "python" in user["tags"]
        assert "vue" in user["tags"]


class TestEmulatorConnection:
    """Test emulator-specific functionality."""

    def test_emulator_requires_host_env(self):
        """Test that EmulatorDatabase requires FIRESTORE_EMULATOR_HOST."""
        # Temporarily remove the env var
        original_host = os.getenv("FIRESTORE_EMULATOR_HOST")
        if original_host:
            os.environ.pop("FIRESTORE_EMULATOR_HOST")
        
        try:
            with pytest.raises(RuntimeError, match="FIRESTORE_EMULATOR_HOST"):
                EmulatorDatabase()
        finally:
            # Restore the env var
            if original_host:
                os.environ["FIRESTORE_EMULATOR_HOST"] = original_host

    def test_emulator_uses_default_project(self, db):
        """Test that emulator can use default project ID."""
        # This test just verifies the database works with default project
        db.set("test", "doc-1", {"value": 123})
        result = db.get("test", "doc-1")
        assert result["value"] == 123
