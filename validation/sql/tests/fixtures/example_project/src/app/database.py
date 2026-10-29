"""The one module that builds engines: every connection gets the runtime pragmas."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event


def _set_pragmas(busy_timeout_s: int) -> Any:
    def listener(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_s) * 1000}")
        cursor.close()

    return listener


def build_engine(url: str, *, busy_timeout_s: int = 5) -> Engine:
    engine = create_engine(url)
    event.listen(engine, "connect", _set_pragmas(busy_timeout_s))
    return engine
