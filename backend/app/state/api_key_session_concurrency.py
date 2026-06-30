"""
Generation-session concurrency on api_keys — single SSOT for per-key parallel work.

All writes to ``active_generation_sessions`` / ``max_concurrent_sessions`` go through
:class:`ApiKeySessionConcurrency` (see CI guard). Public surface is typed only.

**Background-task lifecycle**:
  - Endpoint: use ``begin_or_raise`` to acquire the slot; it rolls back (BEGIN_ROLLBACK)
    if setup raises before the background task is spawned.
  - Background task body: wrap with ``task_slot`` — it calls ``end(COMPLETED)`` on
    normal exit and ``end(FAILED)`` on exception, so the slot is always released.
    ``end()`` is idempotent: safe to call even if the state machine already released
    the slot via ``complete()`` or ``fail()``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, AsyncGenerator

from app.database.interface import ITransactionContext

from app.core.ttl_config import GenerationLifecyclePolicy
from app.state.db_adapter import COL_API_KEYS

if TYPE_CHECKING:
    from app.state.db_adapter import StateMachineDBAdapter

logger = logging.getLogger(__name__)


class OperationKind(StrEnum):
    ANALYSIS = "analysis"
    PLANNING = "planning"
    GENERATION = "generation"


class SessionBeginOutcome(StrEnum):
    ACQUIRED = "acquired"
    ALREADY_HELD = "already_held"
    AT_CAPACITY = "at_capacity"


class SessionEndReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STUCK_DETECTED = "stuck_detected"
    FORCE_RELEASED = "force_released"
    BEGIN_ROLLBACK = "begin_rollback"


_TTL_MINUTES: dict[OperationKind, int] = {
    OperationKind.ANALYSIS: GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES,
    OperationKind.PLANNING: GenerationLifecyclePolicy.SESSION_PLANNING_MINUTES,
    OperationKind.GENERATION: GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES,
}


class SessionAtCapacityError(Exception):
    """Raised by ``begin_or_raise`` when the key has no free generation-session slots."""


class ApiKeyDocMissingError(Exception):
    """Raised when ``api_keys/{api_key_doc_id}`` does not exist."""

    def __init__(self, api_key_doc_id: str) -> None:
        self.api_key_doc_id = api_key_doc_id
        super().__init__(f"API key document not found: {api_key_doc_id[:15]}...")


@dataclass(frozen=True, slots=True)
class ActiveSession:
    generation_id: str
    operation: OperationKind
    lease_started_at: datetime
    lease_ttl_minutes: int


@dataclass(frozen=True, slots=True)
class AuthSessionSnapshot:
    api_key_doc_id: str
    max_concurrent_sessions: int
    active: tuple[ActiveSession, ...]

    @property
    def at_capacity(self) -> bool:
        return len(self.active) >= self.max_concurrent_sessions


@dataclass(frozen=True, slots=True)
class SessionBeginResult:
    outcome: SessionBeginOutcome
    session: ActiveSession | None
    snapshot: AuthSessionSnapshot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc_aware(value)
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        return datetime.fromtimestamp(float(ts()), tz=timezone.utc)
    return None


def _session_expired(s: ActiveSession, now: datetime) -> bool:
    return now >= s.lease_started_at + timedelta(minutes=s.lease_ttl_minutes)


def _active_session_to_firestore(s: ActiveSession) -> dict:
    return {
        "generation_id": s.generation_id,
        "operation": s.operation.value,
        "lease_started_at": s.lease_started_at,
        "lease_ttl_minutes": s.lease_ttl_minutes,
    }


def _active_session_from_firestore(row: dict) -> ActiveSession | None:
    gid = row.get("generation_id")
    if not gid or not isinstance(gid, str):
        return None
    op_raw = row.get("operation") or OperationKind.ANALYSIS.value
    try:
        op = OperationKind(op_raw)
    except ValueError:
        logger.warning("_active_session_from_firestore: unknown operation kind %r — defaulting to ANALYSIS", op_raw)
        op = OperationKind.ANALYSIS
    started = _parse_datetime(row.get("lease_started_at"))
    if started is None:
        return None
    ttl_raw = row.get("lease_ttl_minutes")
    ttl = int(ttl_raw) if ttl_raw is not None else _TTL_MINUTES[op]
    return ActiveSession(
        generation_id=gid,
        operation=op,
        lease_started_at=started,
        lease_ttl_minutes=ttl,
    )


def _deserialize_sessions(raw: object) -> list[ActiveSession]:
    if not isinstance(raw, list):
        return []
    out: list[ActiveSession] = []
    for item in raw:
        if isinstance(item, dict):
            s = _active_session_from_firestore(item)
            if s is not None:
                out.append(s)
    return out


def _snapshot_from_doc(api_key_doc_id: str, doc: dict, *, now: datetime) -> AuthSessionSnapshot:
    max_concurrent_raw = doc.get("max_concurrent_sessions")
    max_n = int(max_concurrent_raw) if max_concurrent_raw is not None else 5
    sessions = [
        s for s in _deserialize_sessions(doc.get("active_generation_sessions")) if not _session_expired(s, now)
    ]
    return AuthSessionSnapshot(
        api_key_doc_id=api_key_doc_id,
        max_concurrent_sessions=max_n,
        active=tuple(sessions),
    )


def _prune_and_find(
    sessions: list[ActiveSession],
    generation_id: str,
    max_n: int,
    now: datetime,
) -> tuple[list[ActiveSession], bool, ActiveSession | None]:
    """Filter expired sessions; find any existing session for generation_id.

    Returns: (live_sessions, was_pruned, existing_session_or_None)
    """
    live = [s for s in sessions if not _session_expired(s, now)]
    was_pruned = len(live) != len(sessions)
    existing = next((s for s in live if s.generation_id == generation_id), None)
    return live, was_pruned, existing


def _build_begin_result(
    outcome: SessionBeginOutcome,
    session: ActiveSession | None,
    api_key_doc_id: str,
    live_sessions: list[ActiveSession],
    max_n: int,
    now: datetime,
) -> SessionBeginResult:
    """Construct a SessionBeginResult from known outcome + live session list."""
    snap = AuthSessionSnapshot(
        api_key_doc_id=api_key_doc_id,
        max_concurrent_sessions=max_n,
        active=tuple(live_sessions),
    )
    return SessionBeginResult(outcome=outcome, session=session, snapshot=snap)


class ApiKeySessionConcurrency:
    """Facade for generation-session slots stored on ``api_keys`` documents."""

    def __init__(self, db: "StateMachineDBAdapter") -> None:
        self._db = db

    async def _api_key_doc_id_for_generation(self, generation_id: str) -> str | None:
        est = await self._db.get_generation_session(generation_id)
        if not est:
            logger.warning(
                "api_key_session_concurrency: generation %s not found — cannot resolve api_keys doc",
                generation_id,
            )
            return None
        key_uid = est.get("key_uid")
        if not key_uid:
            logger.error(
                "api_key_session_concurrency: generation %s has no key_uid — cannot update sessions",
                generation_id,
            )
            return None
        key_doc = await self._db.get_api_key_by_uid(str(key_uid))
        if not key_doc:
            return None
        return str(key_doc.get("_id"))

    async def try_begin(
        self,
        *,
        api_key_doc_id: str,
        generation_id: str,
        operation: OperationKind,
    ) -> SessionBeginResult:
        now = _utc_now()

        def _txn(tx: ITransactionContext) -> SessionBeginResult:
            doc = tx.get(COL_API_KEYS, api_key_doc_id)
            if doc is None:
                raise ApiKeyDocMissingError(api_key_doc_id)

            max_concurrent_raw = doc.get("max_concurrent_sessions")
            max_n = int(max_concurrent_raw) if max_concurrent_raw is not None else 5
            raw = doc.get("active_generation_sessions") or []
            live, was_pruned, existing = _prune_and_find(_deserialize_sessions(raw), generation_id, max_n, now)

            if was_pruned:
                payload = [_active_session_to_firestore(s) for s in live]
                tx.update(COL_API_KEYS, api_key_doc_id, {"active_generation_sessions": payload})

            if existing is not None:
                return _build_begin_result(SessionBeginOutcome.ALREADY_HELD, existing, api_key_doc_id, live, max_n, now)

            if len(live) >= max_n:
                return _build_begin_result(SessionBeginOutcome.AT_CAPACITY, None, api_key_doc_id, live, max_n, now)

            ttl = _TTL_MINUTES[operation]
            new_session = ActiveSession(
                generation_id=generation_id,
                operation=operation,
                lease_started_at=now,
                lease_ttl_minutes=ttl,
            )
            live.append(new_session)
            payload = [_active_session_to_firestore(s) for s in live]
            tx.update(COL_API_KEYS, api_key_doc_id, {"active_generation_sessions": payload})
            return _build_begin_result(SessionBeginOutcome.ACQUIRED, new_session, api_key_doc_id, live, max_n, now)

        return await self._db.run_api_key_generation_session_txn(_txn)

    async def try_begin_for_generation(
        self,
        *,
        generation_id: str,
        operation: OperationKind,
    ) -> SessionBeginResult | None:
        """Acquire a slot, resolving the api_keys doc from the generation itself.

        For the rerun path (manual retry + boot recovery): the originating HTTP
        request's ``api_key`` is not in scope, so the doc id is resolved from the
        session like ``end``/``refresh_lease`` do. Registration is best-effort for
        TUI visibility — it must never gate resuming sacred code — so an
        unresolvable or vanished api_keys doc returns ``None`` instead of raising.
        """
        doc_id = await self._api_key_doc_id_for_generation(generation_id)
        if not doc_id:
            return None
        try:
            return await self.try_begin(
                api_key_doc_id=doc_id,
                generation_id=generation_id,
                operation=operation,
            )
        except ApiKeyDocMissingError:
            logger.warning(
                "try_begin_for_generation: api_keys doc %s vanished mid-resolve for %s — "
                "skipping registration",
                doc_id,
                generation_id,
            )
            return None

    async def end(self, *, generation_id: str, reason: SessionEndReason) -> None:
        doc_id = await self._api_key_doc_id_for_generation(generation_id)
        if not doc_id:
            return
        now = _utc_now()

        def _txn(tx: ITransactionContext) -> None:
            doc = tx.get(COL_API_KEYS, doc_id)
            if not doc:
                return
            sessions = [s for s in _deserialize_sessions(doc.get("active_generation_sessions")) if not _session_expired(s, now)]
            filtered = [s for s in sessions if s.generation_id != generation_id]
            if len(filtered) == len(sessions):
                return
            tx.update(
                COL_API_KEYS,
                doc_id,
                {"active_generation_sessions": [_active_session_to_firestore(x) for x in filtered]},
            )

        await self._db.run_api_key_generation_session_txn(_txn)
        logger.debug(
            "generation session end reason=%s generation_id=%s",
            reason.value,
            generation_id,
        )

    async def refresh_lease(
        self,
        *,
        generation_id: str,
        operation: OperationKind,
    ) -> None:
        doc_id = await self._api_key_doc_id_for_generation(generation_id)
        if not doc_id:
            return
        now = _utc_now()
        ttl = _TTL_MINUTES[operation]

        def _txn(tx: ITransactionContext) -> None:
            doc = tx.get(COL_API_KEYS, doc_id)
            if not doc:
                return
            sessions = [s for s in _deserialize_sessions(doc.get("active_generation_sessions")) if not _session_expired(s, now)]
            if not any(s.generation_id == generation_id for s in sessions):
                return
            replaced = [
                ActiveSession(generation_id=s.generation_id, operation=operation, lease_started_at=now, lease_ttl_minutes=ttl)
                if s.generation_id == generation_id
                else s
                for s in sessions
            ]
            tx.update(
                COL_API_KEYS,
                doc_id,
                {"active_generation_sessions": [_active_session_to_firestore(x) for x in replaced]},
            )

        await self._db.run_api_key_generation_session_txn(_txn)

    @asynccontextmanager
    async def begin_or_raise(
        self,
        *,
        api_key_doc_id: str,
        generation_id: str,
        operation: OperationKind,
    ) -> AsyncGenerator[ActiveSession | None, None]:
        """Async CM: acquire slot or raise; roll back ACQUIRED slot on exception exit.

        Enter: calls ``try_begin``; raises ``SessionAtCapacityError`` if no slot available.
               On ``ALREADY_HELD`` refreshes the lease.
        Exit without exception: no-op (background task owns the slot).
        Exit with exception (ACQUIRED only): calls ``end(BEGIN_ROLLBACK)``.
        """
        result = await self.try_begin(
            api_key_doc_id=api_key_doc_id,
            generation_id=generation_id,
            operation=operation,
        )
        if result.outcome == SessionBeginOutcome.AT_CAPACITY:
            raise SessionAtCapacityError()
        if result.outcome == SessionBeginOutcome.ALREADY_HELD:
            await self.refresh_lease(generation_id=generation_id, operation=operation)
        acquired = result.outcome == SessionBeginOutcome.ACQUIRED
        try:
            yield result.session
        except Exception:
            if acquired:
                await self.end(generation_id=generation_id, reason=SessionEndReason.BEGIN_ROLLBACK)
            raise

    @asynccontextmanager
    async def task_slot(self, *, generation_id: str) -> AsyncGenerator[None, None]:
        """Async CM for background task bodies: always ends the slot on exit.

        Wrap the entire background-task body with this CM after the slot has been
        acquired by ``begin_or_raise`` (or ``try_begin``) in the endpoint:

            async with session.task_slot(generation_id=generation_id):
                await long_running_workflow(...)
            # slot ended — COMPLETED on normal exit, FAILED on exception

        ``end()`` is idempotent, so it is safe even when the state machine already
        released the slot via ``complete()`` or ``fail()``.
        """
        try:
            yield
        except Exception:
            await self.end(generation_id=generation_id, reason=SessionEndReason.FAILED)
            raise
        else:
            await self.end(generation_id=generation_id, reason=SessionEndReason.COMPLETED)

    async def snapshot(self, *, api_key_doc_id: str) -> AuthSessionSnapshot:
        """Read-only snapshot of active sessions for the given API key doc."""
        now = _utc_now()
        doc = await self._db.get_api_key_doc(api_key_doc_id)
        if doc is None:
            raise ApiKeyDocMissingError(api_key_doc_id)
        return _snapshot_from_doc(api_key_doc_id, doc, now=now)
