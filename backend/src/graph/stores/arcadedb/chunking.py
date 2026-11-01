"""Row chunking: a statement whose ``rows`` parameter is large runs as several statements of smaller messages.

ArcadeDB's Bolt plugin rejects a very large request message (``Neo.ClientError.Request.Invalid``, "Expected
structure, got: Long"): the library's persist batches of 500 rows that each carry two 3072-float vectors are
about 27 MB and fail, 100 such rows pass. The dialect therefore cuts the ``rows`` list of every planned statement
into chunks whose estimated Bolt payload stays under ``MAX_MESSAGE_BYTES`` (and at most ``MAX_ROWS_PER_MESSAGE``
rows). The chunks of one statement are consecutive statements of the same plan, so the executor runs them in the
caller's transaction (a managed write or an explicit ``tx``), in order, and ``chunked`` folds their results back
into the rows the unchunked statement would have returned, before the plan's own reducer sees them.

The mechanism is generic: it knows nothing of a template. It relies on one property of every template that takes
``rows`` (they are all ``UNWIND $rows``): rows are independent and processed in order, and the statement returns at
most one aggregate row (``count``, ``collect``) or none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from graph.core.dialect import ExecutionPlan, PlannedStatement, Record

#: The estimated size, in bytes, one chunk of rows may reach. A Bolt float is 9 bytes, so this is about 230 rows of
#: two 3072-float vectors; measured on ArcadeDB 26.10.2 the server accepts at least twice as much, so this leaves a
#: wide margin for the parts of a message the estimate does not count.
MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024
#: The most rows of one chunk, whatever their size (the library's persist batch).
MAX_ROWS_PER_MESSAGE: Final = 500

_SCALAR_BYTES: Final = 9  # a Bolt float64 (marker + 8 bytes); an int, bool or null is smaller
_CONTAINER_BYTES: Final = 5  # marker and size


def estimate_bytes(value: Any) -> int:
    """The approximate size of ``value`` as a Bolt message part: 9 bytes per number, the UTF-8 length plus a
    header per string, the sum of the parts for a list or a map. An estimate, kept slightly high on purpose."""

    if isinstance(value, str):
        return len(value.encode("utf-8")) + _CONTAINER_BYTES
    if isinstance(value, Mapping):
        return _CONTAINER_BYTES + sum(estimate_bytes(key) + estimate_bytes(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _CONTAINER_BYTES + sum(estimate_bytes(item) for item in value)
    return _SCALAR_BYTES


def split_rows(
    rows: Sequence[Any], *, max_bytes: int = MAX_MESSAGE_BYTES, max_rows: int = MAX_ROWS_PER_MESSAGE
) -> list[list[Any]]:
    """``rows`` cut, in order, into chunks of at most ``max_rows`` rows and about ``max_bytes`` bytes (a chunk
    ends before the row that would pass the budget; a single row over it is a chunk of its own). Never empty
    chunks; no rows is no chunk."""

    chunks: list[list[Any]] = []
    current: list[Any] = []
    size = 0
    for row in rows:
        row_bytes = estimate_bytes(row)
        if current and (len(current) >= max_rows or size + row_bytes > max_bytes):
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += row_bytes
    if current:
        chunks.append(current)
    return chunks


def combine(results: Sequence[list[Record]]) -> list[Record]:
    """The result of one statement from the results of its chunks, as if it had run once.

    Every chunk returned one aggregate row: numbers add up (``written``, ``moved``, ``left``), lists
    concatenate in chunk order (``refs``), another value keeps the first chunk's. A chunk that returned no
    row, or several, makes the rows concatenate instead (a statement that returns a row per input row)."""

    if all(len(rows) == 1 for rows in results):
        combined: Record = {}
        for (row,) in results:
            for column, value in row.items():
                if column not in combined:
                    combined[column] = list(value) if isinstance(value, list) else value
                elif isinstance(value, list) and isinstance(combined[column], list):
                    combined[column].extend(value)
                elif _is_count(value) and _is_count(combined[column]):
                    combined[column] += value
        return [combined]
    return [row for rows in results for row in rows]


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _chunks(statement: PlannedStatement, max_bytes: int, max_rows: int) -> list[PlannedStatement]:
    rows = statement.params.get("rows")
    if not isinstance(rows, list):
        return [statement]
    parts = split_rows(rows, max_bytes=max_bytes, max_rows=max_rows)
    if len(parts) <= 1:
        return [statement]
    return [PlannedStatement(statement.text, {**statement.params, "rows": part}) for part in parts]


def chunked(
    plan: ExecutionPlan, *, max_bytes: int = MAX_MESSAGE_BYTES, max_rows: int = MAX_ROWS_PER_MESSAGE
) -> ExecutionPlan:
    """``plan`` with every statement whose ``rows`` exceed the budget replaced by one statement per chunk, in
    order; the plan's reducer gets the combined result of each original statement. A plan with nothing to cut
    is returned as it is."""

    groups = [_chunks(statement, max_bytes, max_rows) for statement in plan.statements]
    if all(len(group) == 1 for group in groups):
        return plan
    statements = tuple(statement for group in groups for statement in group)
    sizes = [len(group) for group in groups]

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        folded: list[list[Record]] = []
        start = 0
        for size in sizes:
            part = results[start : start + size]
            folded.append(part[0] if size == 1 else combine(part))
            start += size
        return plan.reduce(folded)

    return ExecutionPlan(statements, reduce)
