"""
Database abstraction layer for SpecFlow backend.

Provides a unified interface for database operations with support for:
- Production: Cloud Firestore
- Development: Firestore Emulator
- Testing: In-memory database

The factory pattern selects the appropriate implementation based on environment configuration.

Usage:
    >>> from app.database import get_database
    >>> db = get_database()
    >>> db.set("collection", "doc_id", {"field": "value"})
    
FastAPI Usage:
    >>> from fastapi import APIRouter, Depends
    >>> from app.database import IDatabase, get_db
    >>> 
    >>> router = APIRouter()
    >>> 
    >>> @router.get("/items")
    >>> async def list_items(db: IDatabase = Depends(get_db)):
    >>>     items = db.query("items")
    >>>     return {"items": items}
"""

from app.database.interface import (
    IDatabase,
    ITransactionContext,
    DocumentNotFoundError,
    TransactionConflictError,
)
from app.database.memory import InMemoryDatabase
from app.database.firestore import FirestoreDatabase
from app.database.emulator import EmulatorDatabase
from app.database.factory import get_database, reset_database, clear_test_data
from app.database.dependencies import get_db

__all__ = [
    # Interface and exceptions
    "IDatabase",
    "ITransactionContext",
    "DocumentNotFoundError",
    "TransactionConflictError",
    # Implementations (for direct instantiation if needed)
    "InMemoryDatabase",
    "FirestoreDatabase",
    "EmulatorDatabase",
    # Factory functions (recommended way to get database instance)
    "get_database",
    "reset_database",
    "clear_test_data",
    # FastAPI dependencies
    "get_db",
]
