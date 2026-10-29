"""Statements live in query files below `src/app` and are loaded by name."""

from __future__ import annotations

from pathlib import Path

_BASE = Path(__file__).parent


def read_query(path: str) -> str:
    return (_BASE / path).read_text(encoding="utf-8")


def list_open_orders() -> str:
    return read_query("sql/list_open_orders.sql")
