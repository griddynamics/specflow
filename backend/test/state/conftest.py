"""
Shared fixtures for state machine tests.
"""
import pytest
from app.database.memory import _apply_dot_notation_update


class FakeTransaction:
    """Minimal transaction context for testing."""
    def __init__(self, db):
        self._db = db
        self._pending_writes = []  # list of ("workspace"|"generation", id, update)

    
class FakeDB:
    """
    Minimal in-memory DB that matches the interface expected by state machines.
    The state machines call:
      - db.get_generation_session(generation_id) -> dict
      - db.update_generation_session(generation_id, update) -> None
      - db.get_workspace(workspace_id) -> dict
      - db.update_workspace(workspace_id, update) -> None
      - db.get_workspace_in_transaction(workspace_id, transaction) -> dict
      - db.update_workspace_in_transaction(workspace_id, update, transaction) -> None
      - db.run_transaction(txn_fn) -> result
    """

    def __init__(self):
        self._generation_sessions = {}
        self._workspaces = {}

    def seed_generation_session(self, generation_id: str, data: dict):
        self._generation_sessions[generation_id] = dict(data)

    def seed_workspace(self, workspace_id: str, data: dict):
        self._workspaces[workspace_id] = dict(data)

    def get_generation_session_data(self, generation_id: str) -> dict:
        """Direct read for test assertions."""
        return dict(self._generation_sessions.get(generation_id, {}))

    def get_workspace_data(self, workspace_id: str) -> dict:
        """Direct read for test assertions."""
        return dict(self._workspaces.get(workspace_id, {}))

    async def get_generation_session(self, generation_id: str) -> dict:
        return dict(self._generation_sessions.get(generation_id, {}))

    async def update_generation_session(self, generation_id: str, update: dict):
        if generation_id not in self._generation_sessions:
            self._generation_sessions[generation_id] = {}
        doc = self._generation_sessions[generation_id]
        for key, value in update.items():
            _apply_dot_notation_update(doc, key, value)

    async def array_union_generation_session(self, generation_id: str, field: str, values: list):
        if generation_id not in self._generation_sessions:
            self._generation_sessions[generation_id] = {}
        doc = self._generation_sessions[generation_id]
        existing = doc.get(field, [])
        for value in values:
            if value not in existing:
                existing.append(value)
        doc[field] = existing

    async def get_workspace(self, workspace_id: str) -> dict:
        return dict(self._workspaces.get(workspace_id, {}))

    async def update_workspace(self, workspace_id: str, update: dict):
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = {}
        self._workspaces[workspace_id].update(update)

    async def get_workspace_in_transaction(self, workspace_id: str, transaction) -> dict:
        return dict(self._workspaces.get(workspace_id, {}))

    async def update_workspace_in_transaction(self, workspace_id: str, update: dict, transaction):
        transaction._pending_writes.append(("workspace", workspace_id, update))

    async def query_workspaces(self, filters: list) -> list:
        """Query workspaces by filter list of (field, op, value) tuples."""
        results = []
        for ws_id, ws_data in self._workspaces.items():
            doc = dict(ws_data)
            doc["_id"] = ws_id
            match = True
            for field, op, value in filters:
                doc_value = doc.get(field)
                if op == "==" and doc_value != value:
                    match = False
                    break
            if match:
                results.append(doc)
        return results

    async def run_transaction(self, txn_fn):
        txn = FakeTransaction(self)
        result = await txn_fn(txn)
        # Apply all pending writes atomically
        for op_type, doc_id, update in txn._pending_writes:
            if op_type == "workspace":
                if doc_id not in self._workspaces:
                    self._workspaces[doc_id] = {}
                self._workspaces[doc_id].update(update)
        return result


@pytest.fixture
def db():
    return FakeDB()
