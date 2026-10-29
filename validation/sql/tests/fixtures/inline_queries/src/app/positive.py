"""Fixture: inline SQL, a loader path that resolves to no file."""

LIST_ORDERS = "SELECT id, customer_id FROM customer_order WHERE status = 'OPEN'"
ARCHIVE = f"DELETE FROM customer_order WHERE id = {1}"


def read_query(path: str) -> str: ...


def missing() -> str:
    return read_query("sql/missing.sql")
