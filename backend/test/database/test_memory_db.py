"""
Tests for InMemoryDatabase implementation.

Runs the shared IDatabase contract (db_contract.py) against the in-memory backend.
"""

import pytest
from app.database.memory import InMemoryDatabase

from test.database.db_contract import (
    TestArrayOperations as _TestArrayOperations,
    TestBasicCRUD as _TestBasicCRUD,
    TestIsolation as _TestIsolation,
    TestQuery as _TestQuery,
    TestTransactions as _TestTransactions,
)


@pytest.fixture
def db():
    """Create a fresh in-memory database for each test."""
    database = InMemoryDatabase()
    yield database
    database.clear()


class TestBasicCRUD(_TestBasicCRUD):
    pass


class TestQuery(_TestQuery):
    pass


class TestTransactions(_TestTransactions):
    pass


class TestArrayOperations(_TestArrayOperations):
    pass


class TestIsolation(_TestIsolation):
    pass
