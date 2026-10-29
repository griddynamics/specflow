"""Fixture: a docstring may quote SELECT id FROM customer_order WHERE id = 1 freely."""

_STATEMENTS = ("sql/used.sql", "sql/also_used.sql")
LABEL = "SELECT"
PROSE = "Select a customer from the list"


def read_query(path: str) -> str: ...


def used() -> str:
    return read_query("sql/used.sql")


def many() -> list[str]:
    return [read_query(name) for name in _STATEMENTS]


def set_pragmas(cursor: object) -> None:
    cursor.execute("PRAGMA journal_mode=WAL")  # type: ignore[attr-defined]
