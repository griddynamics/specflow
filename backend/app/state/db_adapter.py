"""
StateMachineDBAdapter — bridges IDatabase (sync) to the async interface
expected by GenerationSessionStateMachine and WorkspaceStateMachine.

State machines call:
  - await db.get_generation_session(id)           → dict
  - await db.update_generation_session(id, upd)   → None
  - await db.get_workspace(id)            → dict
  - await db.update_workspace(id, upd)    → None
  - await db.get_workspace_in_transaction(id, tx) → dict
  - await db.update_workspace_in_transaction(id, upd, tx) → None
  - await db.run_transaction(async_fn)    → result

IDatabase (existing) provides synchronous methods:
  db.get(collection, id), db.update(collection, id, data),
  db.set(collection, id, data), db.run_transaction(sync_fn)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from app.database.interface import IDatabase, ReadOnlyDatabase

# Firestore collection names — use these constants instead of repeating string literals
COL_WORKSPACES = "workspaces"
COL_GENERATION_SESSIONS = "generation_sessions"
COL_API_KEYS = "api_keys"


class _ReadOnlyDatabaseView:
    """Read-only façade over ``IDatabase`` exposing only ``get``.

    Handed to read-only consumers (the notifications report renderer) so they
    physically cannot perform status/checkpoint/workspace_phases writes — the
    encapsulation of Commandment VII is enforced by construction, not by a
    docstring warning. Satisfies the ``ReadOnlyDatabase`` protocol structurally.
    """

    __slots__ = ("_db",)

    def __init__(self, db: "IDatabase") -> None:
        self._db = db

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        return self._db.get(collection, doc_id)


class StateMachineDBAdapter:
    """
    Adapts the synchronous IDatabase interface to the async interface
    expected by GenerationSessionStateMachine and WorkspaceStateMachine.

    Transaction semantics: writes within run_transaction are buffered and
    applied atomically after the async callback completes. For the
    InMemoryDatabase (used in tests) this is equivalent to the full
    IDatabase.run_transaction guarantee. For Firestore production this
    adapter is sufficient for Phase 2; a native async Firestore client
    is planned for a later phase.
    """

    def __init__(self, db: "IDatabase") -> None:
        self._db = db

    @property
    def read_only_db(self) -> "ReadOnlyDatabase":
        """A read-only view over the underlying database.

        For consumers that only read (e.g. the notifications report renderer,
        which does a plain ``db.get("workspaces", id)``). The returned view
        exposes *only* ``get`` — writes are unreachable through it, so
        status/checkpoint/workspace_phases writes still have to go through this
        adapter's async methods.
        """
        return _ReadOnlyDatabaseView(self._db)

    # ------------------------------------------------------------------
    # Generation methods
    # ------------------------------------------------------------------

    async def get_generation_session(self, generation_id: str) -> dict:
        return self._db.get(COL_GENERATION_SESSIONS, generation_id) or {}

    async def update_generation_session(self, generation_id: str, update: dict) -> None:
        doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        if doc is None:
            self._db.set(COL_GENERATION_SESSIONS, generation_id, update)
        else:
            self._db.update(COL_GENERATION_SESSIONS, generation_id, update)

    async def array_union_generation_session(
        self, generation_id: str, field: str, values: list
    ) -> None:
        """Atomically append *values* to an array field, matching Firestore ArrayUnion semantics."""
        self._db.array_union(COL_GENERATION_SESSIONS, generation_id, field, values)

    # ------------------------------------------------------------------
    # Workspace methods
    # ------------------------------------------------------------------

    async def get_workspace(self, workspace_id: str) -> dict:
        return self._db.get(COL_WORKSPACES, workspace_id) or {}

    async def update_workspace(self, workspace_id: str, update: dict) -> None:
        self._db.update(COL_WORKSPACES, workspace_id, update)

    # ------------------------------------------------------------------
    # Transaction-aware workspace methods
    # The "transaction" parameter IS the _BridgeTransaction instance
    # returned by run_transaction to the async callback.
    # ------------------------------------------------------------------

    async def get_workspace_in_transaction(
        self, workspace_id: str, transaction: "_BridgeTransaction"
    ) -> dict:
        return transaction.read_workspace(workspace_id)

    async def update_workspace_in_transaction(
        self, workspace_id: str, update: dict, transaction: "_BridgeTransaction"
    ) -> None:
        transaction.write_workspace(workspace_id, update)

    # ------------------------------------------------------------------
    # Transaction runner
    # ------------------------------------------------------------------

    async def query_generation_sessions(self, filters: list) -> list:
        """
        Query generations by filter list of (field, op, value) tuples.
        Delegates to the underlying sync db.query().
        """
        results = self._db.query(COL_GENERATION_SESSIONS, filters=filters)
        # Ensure each result has an 'id' field (some backends use '_id')
        for r in results:
            if "id" not in r and "_id" in r:
                r["id"] = r["_id"]
        return results

    async def query_workspaces(self, filters: list) -> list:
        """
        Query workspaces by filter list of (field, op, value) tuples.
        Delegates to the underlying sync db.query().
        """
        results = self._db.query(COL_WORKSPACES, filters=filters)
        for r in results:
            if "id" not in r and "_id" in r:
                r["id"] = r["_id"]
        return results

    async def get_api_key_doc(self, api_key_doc_id: str) -> dict | None:
        """Non-transactional read of an api_keys document by doc id."""
        return await asyncio.to_thread(self._db.get, COL_API_KEYS, api_key_doc_id)

    async def list_subcollection(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
    ) -> list:
        """List documents in a Firestore subcollection (sync driver wrapped for async callers)."""
        return await asyncio.to_thread(
            self._db.list_subcollection, parent_collection, parent_doc_id, subcollection
        )

    async def get_api_key_by_uid(self, key_uid: str) -> dict | None:
        """Async read of an API key document by key_uid. Returns None if not found."""
        return await asyncio.to_thread(self._db.get_api_key_by_uid, key_uid)

    async def run_api_key_generation_session_txn(self, sync_txn_fn) -> object:
        """
        Synchronous Firestore-style transaction for ``api_keys`` generation-session fields.

        Only :mod:`app.state.api_key_session_concurrency` may use this entry point for
        ``active_generation_sessions`` / ``max_concurrent_sessions`` writes.
        """
        return await self.run_sync_transaction(sync_txn_fn)

    async def query_collection(self, collection: str, filters: list) -> list:
        """
        Generic collection query. Allows callers that use the adapter as a
        drop-in for IDatabase (e.g. release_locks_for_generation_session) to query
        any collection without needing a collection-specific method.
        """
        results = self._db.query(collection, filters=filters)
        for r in results:
            if "id" not in r and "_id" in r:
                r["id"] = r["_id"]
        return results

    async def run_sync_transaction(self, sync_txn_fn) -> object:
        """
        Run a synchronous IDatabase transaction function asynchronously.

        Wraps self._db.run_transaction in asyncio.to_thread so callers don't need
        direct access to the underlying IDatabase.
        This is the only sanctioned path for sync transactions outside the state
        machines — keeps raw_db out of service and API layers.
        """
        return await asyncio.to_thread(self._db.run_transaction, sync_txn_fn)

    async def run_transaction(self, async_txn_fn) -> object:
        """
        Execute an async transaction function.

        A _BridgeTransaction is passed to async_txn_fn. All workspace
        reads are served from the underlying db; writes are buffered and
        committed atomically after the callback returns.
        """
        bridge = _BridgeTransaction(self._db)
        result = await async_txn_fn(bridge)
        bridge.commit()
        return result


class _BridgeTransaction:
    """
    Passed as the 'transaction' argument to state machine transaction
    callbacks. Buffers writes so they can be applied after the callback.
    """

    def __init__(self, db: "IDatabase") -> None:
        self._db = db
        self._writes: list[tuple[str, str, dict]] = []

    def read_workspace(self, workspace_id: str) -> dict:
        return self._db.get(COL_WORKSPACES, workspace_id) or {}

    def write_workspace(self, workspace_id: str, update: dict) -> None:
        self._writes.append((COL_WORKSPACES, workspace_id, update))

    def commit(self) -> None:
        for collection, doc_id, update in self._writes:
            self._db.update(collection, doc_id, update)
