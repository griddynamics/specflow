"""
Shared fixtures for job tests.

JobFakeDB extends the state machine FakeDB with query_generation_sessions and
query_workspaces so the job functions can filter documents.
"""
# Patch logging configuration BEFORE any app imports to prevent filesystem
# errors when /agent_logs doesn't exist in the test environment.
import pytest
from unittest.mock import Mock

import app.core.logging as _logging_module
from app.state.db_adapter import COL_GENERATION_SESSIONS

_logging_module.configure_logging = Mock()


class FakeTransaction:
    def __init__(self, db):
        self._db = db
        self._pending_writes = []


class JobFakeDB:
    """
    In-memory DB that matches both the state machine interface
    (get_generation_session, update_generation_session, etc.) and the job interface
    (query_generation_sessions, query_workspaces).
    """

    def __init__(self):
        self._generation_sessions: dict = {}
        self._workspaces: dict = {}
        self._api_keys: dict = {}

    # ------------------------------------------------------------------ #
    # Seeding helpers
    # ------------------------------------------------------------------ #

    def seed_generation_session(self, eid: str, data: dict):
        self._generation_sessions[eid] = {"id": eid, **data}

    def seed_workspace(self, wid: str, data: dict):
        self._workspaces[wid] = {"id": wid, **data}

    def get_generation_session_data(self, eid: str) -> dict:
        return dict(self._generation_sessions.get(eid, {}))

    def get_workspace_data(self, wid: str) -> dict:
        return dict(self._workspaces.get(wid, {}))

    def seed_api_key(self, key_id: str, data: dict):
        self._api_keys[key_id] = {"id": key_id, **data}

    def get_api_key_data(self, key_id: str) -> dict:
        return dict(self._api_keys.get(key_id, {}))

    # ------------------------------------------------------------------ #
    # Sync IDatabase-compatible interface for legacy job tests
    # ------------------------------------------------------------------ #

    def update(self, collection: str, doc_id: str, data: dict):
        store = {
            COL_GENERATION_SESSIONS: self._generation_sessions,
            "workspaces": self._workspaces,
            "api_keys": self._api_keys,
        }.get(collection)
        if store is not None and doc_id in store:
            store[doc_id].update(data)

    async def update_api_key(self, api_key_id: str, data: dict):
        """Async api_keys write — mirrors StateMachineDBAdapter.update_api_key."""
        self.update("api_keys", api_key_id, data)

    async def get_api_key_by_uid(self, key_uid: str) -> dict | None:
        for doc in self._api_keys.values():
            if doc.get("key_uid") == key_uid:
                out = dict(doc)
                out["_id"] = doc.get("id", doc.get("api_key"))
                return out
        return None

    async def run_api_key_generation_session_txn(self, sync_txn_fn):
        """Mirrors StateMachineDBAdapter.run_api_key_generation_session_txn."""
        return await self.run_sync_transaction(sync_txn_fn)

    # ------------------------------------------------------------------ #
    # Async state-machine interface
    # ------------------------------------------------------------------ #

    async def get_generation_session(self, eid: str) -> dict:
        return dict(self._generation_sessions.get(eid, {}))

    async def update_generation_session(self, eid: str, update: dict):
        if eid not in self._generation_sessions:
            self._generation_sessions[eid] = {"id": eid}
        self._generation_sessions[eid].update(update)

    async def get_workspace(self, wid: str) -> dict:
        return dict(self._workspaces.get(wid, {}))

    async def update_workspace(self, wid: str, update: dict):
        if wid not in self._workspaces:
            self._workspaces[wid] = {"id": wid}
        self._workspaces[wid].update(update)

    async def get_workspace_in_transaction(self, wid: str, transaction) -> dict:
        return dict(self._workspaces.get(wid, {}))

    async def update_workspace_in_transaction(self, wid: str, update: dict, transaction):
        transaction._pending_writes.append(("workspace", wid, update))

    async def run_sync_transaction(self, sync_txn_fn):
        """
        Sync transaction for atomic check-and-set (e.g. begin_analysis).
        Provides a minimal ITransactionContext so the sync callback can call
        tx.get(collection, doc_id) and tx.update(collection, doc_id, data).
        Single-threaded in tests — no actual locking needed.
        """
        stores = {
            COL_GENERATION_SESSIONS: self._generation_sessions,
            "workspaces": self._workspaces,
            "api_keys": self._api_keys,
        }

        class _SyncTx:
            def get(_, collection, doc_id):  # noqa: N805
                store = stores.get(collection, {})
                doc = store.get(doc_id)
                return dict(doc) if doc is not None else None

            def update(_, collection, doc_id, data):  # noqa: N805
                store = stores.get(collection)
                if store is not None:
                    if doc_id not in store:
                        store[doc_id] = {"id": doc_id}
                    store[doc_id].update(data)

        return sync_txn_fn(_SyncTx())

    async def run_transaction(self, txn_fn):
        txn = FakeTransaction(self)
        result = await txn_fn(txn)
        for op_type, doc_id, update in txn._pending_writes:
            if op_type == "workspace":
                if doc_id not in self._workspaces:
                    self._workspaces[doc_id] = {"id": doc_id}
                self._workspaces[doc_id].update(update)
        return result

    # ------------------------------------------------------------------ #
    # Query interface (used by job functions)
    # ------------------------------------------------------------------ #

    async def query_generation_sessions(self, filters: list) -> list:
        return [
            dict(doc) for doc in self._generation_sessions.values()
            if self._matches(doc, filters)
        ]

    async def query_workspaces(self, filters: list) -> list:
        return [
            dict(doc) for doc in self._workspaces.values()
            if self._matches(doc, filters)
        ]

    async def query_collection(self, collection: str, filters: list) -> list:
        store = {
            COL_GENERATION_SESSIONS: self._generation_sessions,
            "workspaces": self._workspaces,
            "api_keys": self._api_keys,
        }.get(collection, {})
        return [dict(doc) for doc in store.values() if self._matches(doc, filters)]

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _matches(doc: dict, filters: list) -> bool:
        for field, op, value in filters:
            doc_val = doc.get(field)
            if op == "==":
                if doc_val != value:
                    return False
            elif op == "<":
                if doc_val is None or doc_val >= value:
                    return False
            elif op == ">":
                if doc_val is None or doc_val <= value:
                    return False
            elif op == "<=":
                if doc_val is None or doc_val > value:
                    return False
            elif op == ">=":
                if doc_val is None or doc_val < value:
                    return False
        return True


@pytest.fixture
def db():
    return JobFakeDB()
