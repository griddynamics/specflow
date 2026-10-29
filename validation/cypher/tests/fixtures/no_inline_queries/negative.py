"""Fixture: clean under no_inline_queries -- no query text, no loader calls."""

from __future__ import annotations

GREETING = "hello, world"


def add(a: int, b: int) -> int:
    return a + b
