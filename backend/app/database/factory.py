"""
Database factory for creating the appropriate database implementation.

This factory function reads the DATABASE_TYPE environment variable and returns
the corresponding database implementation. It implements a singleton pattern to
ensure only one database instance exists per process.
"""

from typing import Optional

from app.core.config import settings
from app.core.enums import DatabaseType
from app.database.interface import IDatabase
from app.database.memory import InMemoryDatabase
from app.database.emulator import EmulatorDatabase
from app.database.firestore import FirestoreDatabase


# Singleton instance
_database_instance: Optional[IDatabase] = None


def get_database() -> IDatabase:
    """
    Get the database instance based on configuration.
    
    This function implements a singleton pattern - it creates the database
    instance on first call and returns the same instance on subsequent calls.
    
    The implementation is selected based on the DATABASE_TYPE setting:
    - "memory": InMemoryDatabase (for unit tests)
    - "emulator": EmulatorDatabase (for local development with Firestore Emulator)
    - "firestore": FirestoreDatabase (for production GCP Firestore)
    
    Returns:
        IDatabase: Configured database instance
        
    Raises:
        ValueError: If DATABASE_TYPE is invalid
        
    Environment Variables:
        DATABASE_TYPE: Type of database to use (memory|emulator|firestore)
        FIRESTORE_EMULATOR_HOST: Emulator host:port (required for emulator mode)
        GCP_PROJECT_ID: GCP project ID (optional for firestore mode)
        
    Example:
        >>> from app.database.factory import get_database
        >>> db = get_database()
        >>> db.set("test", "doc1", {"foo": "bar"})
    """
    global _database_instance
    
    # Return existing instance if already created
    if _database_instance is not None:
        return _database_instance
    
    # Create new instance based on configuration
    db_type = settings.DATABASE_TYPE

    if db_type == DatabaseType.MEMORY:
        _database_instance = InMemoryDatabase()
    elif db_type == DatabaseType.EMULATOR:
        if not settings.FIRESTORE_EMULATOR_HOST:
            raise ValueError(
                "FIRESTORE_EMULATOR_HOST must be set when DATABASE_TYPE=emulator"
            )
        # EmulatorDatabase reads FIRESTORE_EMULATOR_HOST from environment
        # and uses "local-dev" as default project_id if not provided
        _database_instance = EmulatorDatabase(
            project_id=settings.GCP_PROJECT_ID,
            database=settings.FIRESTORE_DATABASE_NAME
        )
    elif db_type == DatabaseType.FIRESTORE:
        # For production Firestore, project_id is optional (uses ADC if not provided)
        _database_instance = FirestoreDatabase(
            project_id=settings.GCP_PROJECT_ID,
            database=settings.FIRESTORE_DATABASE_NAME
        )
    else:
        raise ValueError(
            f"Invalid DATABASE_TYPE: {db_type}. "
            f"Must be one of: memory, emulator, firestore"
        )
    
    return _database_instance


def reset_database() -> None:
    """
    Reset the singleton database instance.
    
    This is primarily useful for testing, allowing tests to create a fresh
    database instance with different configuration.
    
    Warning:
        This should only be used in test code. In production, the database
        instance should be created once and reused throughout the application
        lifetime.
        
    Example:
        >>> # In test setup
        >>> reset_database()
        >>> db = get_database()  # Creates new instance
    """
    global _database_instance
    _database_instance = None


def clear_test_data(collections: Optional[list[str]] = None) -> None:
    """
    Clear all data from the database (for testing only).
    
    This function clears data from the current database instance. It's safe to use
    with in-memory and emulator databases. Should NOT be used in production.
    
    Args:
        collections: List of collection names to clear. If None, clears common
                    test collections (api_keys, generations, workspaces).
    
    Warning:
        This is a destructive operation. Only use in test environments.
    
    Example:
        >>> # In test fixture
        >>> clear_test_data()  # Clear all test data
        >>> clear_test_data(["api_keys"])  # Clear only api_keys
    """
    db = get_database()
    
    # Check if we're in a safe environment
    db_type = settings.DATABASE_TYPE
    if db_type not in (DatabaseType.MEMORY, DatabaseType.EMULATOR):
        raise RuntimeError(
            f"clear_test_data() should only be used with 'memory' or 'emulator' databases. "
            f"Current database type: {db_type}"
        )
    
    # Call clear_all if the database supports it
    if hasattr(db, "clear_all"):
        db.clear_all(collections)
    else:
        # For in-memory database, just reset the instance
        reset_database()
