"""Sample engine factories for the `sqlite-pragmas` self-test: one that sets the runtime
pragmas on every connection (sync and async), one that sets none."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def _pragma_listener(busy_timeout_s: int) -> Any:
    def set_pragmas(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_s) * 1000}")
        cursor.close()

    return set_pragmas


def build_engine(url: str, *, busy_timeout_s: int = 5) -> Engine:
    engine = create_engine(url)
    event.listen(engine, "connect", _pragma_listener(busy_timeout_s))
    return engine


def build_async_engine(url: str, *, busy_timeout_s: int = 5) -> AsyncEngine:
    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "connect", _pragma_listener(busy_timeout_s))
    return engine


def build_plain_engine(url: str, *, busy_timeout_s: int = 5) -> Engine:
    del busy_timeout_s
    return create_engine(url)
