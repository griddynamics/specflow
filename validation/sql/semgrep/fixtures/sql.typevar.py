"""sql.yml's `async def` cases beside a TypeVar generic: the generic spelling leaves
semgrep's `async def` patterns matching."""

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def batched(rows: Sequence[T], size: int) -> list[Sequence[T]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


async def retain_state(session: object) -> None:
    # ruleid: kit.sql.commit-outside-service
    await session.commit()


async def flush_buffer(buffer: object) -> None:
    # ok: kit.sql.commit-outside-service
    await buffer.commit()
