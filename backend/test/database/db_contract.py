"""
Shared IDatabase contract tests.

Every concrete backend (InMemoryDatabase, SqliteDatabase, ...) must satisfy the exact
same behavior for every IDatabase method. Each test module here has no ``db`` fixture —
concrete test modules (test_memory_db.py, test_sqlite_db.py) supply their own fixture
and subclass these classes so pytest collects them once per backend.
"""

import pytest

from app.database.interface import DocumentNotFoundError

# Backends that map collections onto fixed relational tables (SqliteDatabase) only accept
# registered collections, so the shared contract uses two real ones instead of ad-hoc
# names. Transparent to the generic backends (memory/Firestore), and it makes the query
# tests also exercise SQLite's promoted `status` column path.
_C1 = "generation_sessions"
_C2 = "workspaces"


class TestBasicCRUD:
    """Test basic CRUD operations."""

    def test_set_and_get(self, db):
        """Test setting and getting a document."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30})

        result = db.get(_C1, "user-1")
        assert result is not None
        assert result["name"] == "Alice"
        assert result["age"] == 30

    def test_get_nonexistent(self, db):
        """Test getting a document that doesn't exist."""
        result = db.get(_C1, "nonexistent")
        assert result is None

    def test_set_overwrites(self, db):
        """Test that set overwrites existing data."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30})
        db.set(_C1, "user-1", {"name": "Bob", "age": 25})

        result = db.get(_C1, "user-1")
        assert result["name"] == "Bob"
        assert result["age"] == 25

    def test_update_existing(self, db):
        """Test updating an existing document."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30, "city": "NYC"})
        db.update(_C1, "user-1", {"age": 31})

        result = db.get(_C1, "user-1")
        assert result["name"] == "Alice"
        assert result["age"] == 31
        assert result["city"] == "NYC"

    def test_update_nonexistent(self, db):
        """Test updating a document that doesn't exist."""
        with pytest.raises(DocumentNotFoundError) as exc_info:
            db.update(_C1, "nonexistent", {"age": 30})

        assert exc_info.value.collection == _C1
        assert exc_info.value.doc_id == "nonexistent"

    def test_delete(self, db):
        """Test deleting a document."""
        db.set(_C1, "user-1", {"name": "Alice"})
        db.delete(_C1, "user-1")

        result = db.get(_C1, "user-1")
        assert result is None

    def test_delete_nonexistent(self, db):
        """Test deleting a document that doesn't exist (should not raise)."""
        db.delete(_C1, "nonexistent")  # Should not raise

    def test_isolation_between_collections(self, db):
        """Test that collections are isolated."""
        db.set(_C1, "id-1", {"type": "user"})
        db.set(_C2, "id-1", {"type": "post"})

        user = db.get(_C1, "id-1")
        post = db.get(_C2, "id-1")

        assert user["type"] == "user"
        assert post["type"] == "post"


class TestQuery:
    """Test query operations."""

    def test_query_all(self, db):
        """Test querying all documents in collection."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30})
        db.set(_C1, "user-2", {"name": "Bob", "age": 25})
        db.set(_C1, "user-3", {"name": "Charlie", "age": 35})

        results = db.query(_C1)
        assert len(results) == 3

        # All results should have _id field
        ids = {r["_id"] for r in results}
        assert ids == {"user-1", "user-2", "user-3"}

    def test_query_empty_collection(self, db):
        """Test querying an empty collection."""
        results = db.query(_C1)
        assert results == []

    def test_query_filter_equals(self, db):
        """Test query with equals filter."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30})
        db.set(_C1, "user-2", {"name": "Bob", "age": 25})
        db.set(_C1, "user-3", {"name": "Alice", "age": 35})

        results = db.query(_C1, filters=[("name", "==", "Alice")])
        assert len(results) == 2
        assert all(r["name"] == "Alice" for r in results)

    def test_query_filter_not_equals(self, db):
        """Test query with not equals filter."""
        db.set(_C1, "user-1", {"name": "Alice", "status": "active"})
        db.set(_C1, "user-2", {"name": "Bob", "status": "inactive"})

        results = db.query(_C1, filters=[("status", "!=", "inactive")])
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_query_filter_comparison(self, db):
        """Test query with comparison filters."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30})
        db.set(_C1, "user-2", {"name": "Bob", "age": 25})
        db.set(_C1, "user-3", {"name": "Charlie", "age": 35})

        # Greater than
        results = db.query(_C1, filters=[("age", ">", 30)])
        assert len(results) == 1
        assert results[0]["name"] == "Charlie"

        # Greater than or equal
        results = db.query(_C1, filters=[("age", ">=", 30)])
        assert len(results) == 2

        # Less than
        results = db.query(_C1, filters=[("age", "<", 30)])
        assert len(results) == 1
        assert results[0]["name"] == "Bob"

        # Less than or equal
        results = db.query(_C1, filters=[("age", "<=", 30)])
        assert len(results) == 2

    def test_query_filter_in(self, db):
        """Test query with 'in' filter."""
        db.set(_C1, "user-1", {"name": "Alice", "role": "admin"})
        db.set(_C1, "user-2", {"name": "Bob", "role": "user"})
        db.set(_C1, "user-3", {"name": "Charlie", "role": "moderator"})

        results = db.query(_C1, filters=[("role", "in", ["admin", "moderator"])])
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"Alice", "Charlie"}

    def test_query_filter_array_contains(self, db):
        """Test query with array_contains filter."""
        db.set(_C1, "user-1", {"name": "Alice", "tags": ["python", "react"]})
        db.set(_C1, "user-2", {"name": "Bob", "tags": ["java", "spring"]})
        db.set(_C1, "user-3", {"name": "Charlie", "tags": ["python", "django"]})

        results = db.query(_C1, filters=[("tags", "array_contains", "python")])
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"Alice", "Charlie"}

    def test_query_multiple_filters(self, db):
        """Test query with multiple filters (AND logic)."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30, "status": "active"})
        db.set(_C1, "user-2", {"name": "Bob", "age": 25, "status": "active"})
        db.set(_C1, "user-3", {"name": "Charlie", "age": 30, "status": "inactive"})

        results = db.query(_C1, filters=[
            ("age", "==", 30),
            ("status", "==", "active")
        ])
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_query_order_by_ascending(self, db):
        """Test query with ascending order."""
        db.set(_C1, "user-1", {"name": "Charlie", "age": 35})
        db.set(_C1, "user-2", {"name": "Alice", "age": 30})
        db.set(_C1, "user-3", {"name": "Bob", "age": 25})

        results = db.query(_C1, order_by="age")
        assert len(results) == 3
        assert results[0]["name"] == "Bob"
        assert results[1]["name"] == "Alice"
        assert results[2]["name"] == "Charlie"

    def test_query_order_by_descending(self, db):
        """Test query with descending order."""
        db.set(_C1, "user-1", {"name": "Charlie", "age": 35})
        db.set(_C1, "user-2", {"name": "Alice", "age": 30})
        db.set(_C1, "user-3", {"name": "Bob", "age": 25})

        results = db.query(_C1, order_by="-age")
        assert len(results) == 3
        assert results[0]["name"] == "Charlie"
        assert results[1]["name"] == "Alice"
        assert results[2]["name"] == "Bob"

    def test_query_limit(self, db):
        """Test query with limit."""
        db.set(_C1, "user-1", {"name": "Alice"})
        db.set(_C1, "user-2", {"name": "Bob"})
        db.set(_C1, "user-3", {"name": "Charlie"})

        results = db.query(_C1, limit=2)
        assert len(results) == 2

    def test_query_combined(self, db):
        """Test query with filters, ordering, and limit."""
        db.set(_C1, "user-1", {"name": "Alice", "age": 30, "status": "active"})
        db.set(_C1, "user-2", {"name": "Bob", "age": 25, "status": "active"})
        db.set(_C1, "user-3", {"name": "Charlie", "age": 35, "status": "active"})
        db.set(_C1, "user-4", {"name": "Dave", "age": 28, "status": "inactive"})

        results = db.query(
            _C1,
            filters=[("status", "==", "active")],
            order_by="-age",
            limit=2
        )

        assert len(results) == 2
        assert results[0]["name"] == "Charlie"
        assert results[1]["name"] == "Alice"


class TestTransactions:
    """Test transaction operations."""

    def test_transaction_commit(self, db):
        """Test successful transaction commit."""
        db.set(_C1, "user-1", {"name": "Alice", "balance": 100})
        db.set(_C1, "user-2", {"name": "Bob", "balance": 50})

        def transfer(tx):
            alice = tx.get(_C1, "user-1")
            bob = tx.get(_C1, "user-2")

            tx.update(_C1, "user-1", {"balance": alice["balance"] - 20})
            tx.update(_C1, "user-2", {"balance": bob["balance"] + 20})

            return "success"

        result = db.run_transaction(transfer)
        assert result == "success"

        alice = db.get(_C1, "user-1")
        bob = db.get(_C1, "user-2")

        assert alice["balance"] == 80
        assert bob["balance"] == 70

    def test_transaction_rollback(self, db):
        """Test transaction rollback on error."""
        db.set(_C1, "user-1", {"name": "Alice", "balance": 100})

        def failing_transaction(tx):
            tx.update(_C1, "user-1", {"balance": 50})
            raise ValueError("Something went wrong")

        with pytest.raises(ValueError):
            db.run_transaction(failing_transaction)

        # Balance should not have changed
        alice = db.get(_C1, "user-1")
        assert alice["balance"] == 100

    def test_transaction_set(self, db):
        """Test transaction with set operation."""
        def create_user(tx):
            tx.set(_C1, "user-1", {"name": "Alice", "age": 30})
            return "created"

        result = db.run_transaction(create_user)
        assert result == "created"

        user = db.get(_C1, "user-1")
        assert user["name"] == "Alice"

    def test_transaction_delete(self, db):
        """Test transaction with delete operation."""
        db.set(_C1, "user-1", {"name": "Alice"})

        def delete_user(tx):
            tx.delete(_C1, "user-1")

        db.run_transaction(delete_user)

        user = db.get(_C1, "user-1")
        assert user is None

    def test_transaction_update_nonexistent(self, db):
        """Test transaction fails when updating nonexistent document."""
        def failing_update(tx):
            tx.update(_C1, "nonexistent", {"name": "Alice"})

        with pytest.raises(DocumentNotFoundError):
            db.run_transaction(failing_update)


class TestArrayOperations:
    """Test array operations."""

    def test_array_union_new_field(self, db):
        """Test array_union creates new array field."""
        db.set(_C1, "user-1", {"name": "Alice"})
        db.array_union(_C1, "user-1", "tags", ["python", "react"])

        user = db.get(_C1, "user-1")
        assert "tags" in user
        assert set(user["tags"]) == {"python", "react"}

    def test_array_union_existing_field(self, db):
        """Test array_union adds to existing array."""
        db.set(_C1, "user-1", {"name": "Alice", "tags": ["python"]})
        db.array_union(_C1, "user-1", "tags", ["react", "vue"])

        user = db.get(_C1, "user-1")
        assert set(user["tags"]) == {"python", "react", "vue"}

    def test_array_union_no_duplicates(self, db):
        """Test array_union doesn't add duplicates."""
        db.set(_C1, "user-1", {"name": "Alice", "tags": ["python", "react"]})
        db.array_union(_C1, "user-1", "tags", ["python", "vue"])

        user = db.get(_C1, "user-1")
        assert set(user["tags"]) == {"python", "react", "vue"}

    def test_array_union_nonexistent_doc(self, db):
        """Test array_union fails on nonexistent document."""
        with pytest.raises(DocumentNotFoundError):
            db.array_union(_C1, "nonexistent", "tags", ["python"])


class TestIsolation:
    """Test data isolation and cleanup."""

    def test_clear(self, db):
        """Test clearing database."""
        db.set(_C1, "user-1", {"name": "Alice"})
        db.set(_C2, "post-1", {"title": "Hello"})

        db.clear()

        assert db.get(_C1, "user-1") is None
        assert db.get(_C2, "post-1") is None

    def test_get_returns_copy(self, db):
        """Test that get returns a copy, not reference."""
        db.set(_C1, "user-1", {"name": "Alice"})

        user1 = db.get(_C1, "user-1")
        user1["name"] = "Bob"

        user2 = db.get(_C1, "user-1")
        assert user2["name"] == "Alice"
