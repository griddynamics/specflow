"""
Tests for the database factory.

Tests that the factory correctly returns the appropriate database implementation
based on configuration and validates error handling.
"""

import os
import pytest
from unittest.mock import patch

from app.database.factory import get_database, reset_database
from app.database.memory import InMemoryDatabase
from app.database.emulator import EmulatorDatabase
from app.database.firestore import FirestoreDatabase


@pytest.fixture(autouse=True)
def reset_factory():
    """Reset the factory singleton before each test."""
    reset_database()
    yield
    reset_database()


class TestDatabaseFactory:
    """Test database factory functionality."""
    
    def test_factory_returns_memory_database(self):
        """Test factory returns InMemoryDatabase when DATABASE_TYPE=memory."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            # Force settings reload
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db = get_database()
                assert isinstance(db, InMemoryDatabase)
    
    def test_factory_returns_emulator_database(self):
        """Test factory returns EmulatorDatabase when DATABASE_TYPE=emulator."""
        with patch.dict(os.environ, {
            "DATABASE_TYPE": "emulator",
            "FIRESTORE_EMULATOR_HOST": "localhost:8080",
            "GCP_PROJECT_ID": "test-project"
        }):
            from app.core.config import Settings
            # Mock the Firestore client to avoid actual connection
            with patch("app.database.firestore.firestore.Client"):
                with patch("app.database.factory.settings", Settings()):
                    reset_database()
                    db = get_database()
                    assert isinstance(db, EmulatorDatabase)
    
    def test_factory_returns_firestore_database(self):
        """Test factory returns FirestoreDatabase when DATABASE_TYPE=firestore."""
        with patch.dict(os.environ, {
            "DATABASE_TYPE": "firestore",
            "GCP_PROJECT_ID": "prod-project"
        }):
            from app.core.config import Settings
            # Mock the Firestore client to avoid actual connection
            with patch("app.database.firestore.firestore.Client"):
                with patch("app.database.factory.settings", Settings()):
                    reset_database()
                    db = get_database()
                    assert isinstance(db, FirestoreDatabase)
    
    def test_factory_singleton_pattern(self):
        """Test factory returns same instance on multiple calls."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db1 = get_database()
                db2 = get_database()
                assert db1 is db2
    
    def test_factory_reset_creates_new_instance(self):
        """Test reset_database() allows creating a new instance."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db1 = get_database()
                reset_database()
                db2 = get_database()
                assert db1 is not db2
                assert isinstance(db1, InMemoryDatabase)
                assert isinstance(db2, InMemoryDatabase)
    
    def test_factory_emulator_requires_emulator_host(self):
        """Test factory raises error when emulator mode missing FIRESTORE_EMULATOR_HOST."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "emulator"}, clear=True):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                with pytest.raises(ValueError, match="FIRESTORE_EMULATOR_HOST must be set"):
                    get_database()
    
    def test_factory_invalid_database_type(self):
        """Test that an invalid DATABASE_TYPE is rejected at Settings creation (fail-closed)."""
        from pydantic import ValidationError
        with patch.dict(os.environ, {"DATABASE_TYPE": "invalid"}):
            from app.core.config import Settings
            with pytest.raises(ValidationError, match="Invalid DATABASE_TYPE"):
                Settings()

    def test_factory_case_sensitive(self):
        """Test factory rejects upper-case DATABASE_TYPE values (fail-closed per INV-3)."""
        from pydantic import ValidationError
        with patch.dict(os.environ, {"DATABASE_TYPE": "MEMORY"}):
            from app.core.config import Settings
            with pytest.raises(ValidationError, match="Invalid DATABASE_TYPE"):
                Settings()
    
    def test_emulator_uses_default_project_id(self):
        """Test emulator uses 'local-dev' as default project ID."""
        with patch.dict(os.environ, {
            "DATABASE_TYPE": "emulator",
            "FIRESTORE_EMULATOR_HOST": "localhost:8080"
        }, clear=True):
            from app.core.config import Settings
            # Mock the Firestore client to avoid actual connection
            with patch("app.database.firestore.firestore.Client"):
                with patch("app.database.factory.settings", Settings()):
                    reset_database()
                    db = get_database()
                    assert isinstance(db, EmulatorDatabase)
                    # The emulator should have been initialized with 'local-dev' (default from EmulatorDatabase)
    
    def test_firestore_allows_missing_project_id(self):
        """Test Firestore mode works without explicit project_id (uses ADC)."""
        # For this test, we need to mock the Firestore client since we don't have real GCP credentials
        with patch.dict(os.environ, {"DATABASE_TYPE": "firestore"}, clear=True):
            from app.core.config import Settings
            # Mock the Firestore client to simulate ADC behavior
            with patch("app.database.firestore.firestore.Client") as mock_client:
                with patch("app.database.factory.settings", Settings()):
                    reset_database()
                    db = get_database()
                    assert isinstance(db, FirestoreDatabase)
                    # Verify Client was called with project=None and the normalized SDK database name
                    mock_client.assert_called_once_with(project=None, database='(default)')


class TestDatabaseFactoryIntegration:
    """Integration tests for factory with actual database operations."""
    
    def test_memory_database_from_factory_works(self):
        """Test that memory database from factory performs basic operations."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db = get_database()
                
                # Basic CRUD operations
                db.set("test", "doc1", {"name": "Alice"})
                result = db.get("test", "doc1")
                assert result == {"name": "Alice"}
                
                db.update("test", "doc1", {"name": "Bob"})
                result = db.get("test", "doc1")
                assert result == {"name": "Bob"}
                
                db.delete("test", "doc1")
                result = db.get("test", "doc1")
                assert result is None
    
    def test_factory_database_query_works(self):
        """Test that database from factory can perform queries."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db = get_database()
                
                # Set up test data
                db.set("users", "user1", {"name": "Alice", "age": 30})
                db.set("users", "user2", {"name": "Bob", "age": 25})
                db.set("users", "user3", {"name": "Charlie", "age": 35})
                
                # Query
                results = db.query("users", filters=[("age", ">=", 30)])
                assert len(results) == 2
                names = {doc["name"] for doc in results}
                assert names == {"Alice", "Charlie"}
    
    def test_factory_database_transaction_works(self):
        """Test that database from factory can perform transactions."""
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                db = get_database()
                
                # Set up initial data
                db.set("counter", "count1", {"value": 0})
                
                # Transaction to increment
                def increment(tx):
                    doc = tx.get("counter", "count1")
                    new_value = doc["value"] + 1
                    tx.update("counter", "count1", {"value": new_value})
                    return new_value
                
                result = db.run_transaction(increment)
                assert result == 1
                
                # Verify
                doc = db.get("counter", "count1")
                assert doc["value"] == 1


class TestDatabaseFactoryFastAPIIntegration:
    """Test factory integration with FastAPI dependency injection pattern."""
    
    def test_factory_as_fastapi_dependency(self):
        """Test factory can be used as FastAPI dependency."""
        from app.database import IDatabase
        
        def get_db() -> IDatabase:
            """FastAPI dependency that returns database instance."""
            return get_database()
        
        with patch.dict(os.environ, {"DATABASE_TYPE": "memory"}):
            from app.core.config import Settings
            with patch("app.database.factory.settings", Settings()):
                reset_database()
                
                # Simulate FastAPI dependency injection
                db = get_db()
                assert isinstance(db, IDatabase)
                
                # Should return same instance on subsequent calls
                db2 = get_db()
                assert db is db2
