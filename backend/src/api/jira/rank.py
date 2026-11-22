"""Rank moves by the longest increasing subsequence (docs/jira-sync/sync.md#rank). Pure: node ids in, moves out."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

RANK_LIMIT: Final = 50


@dataclass(frozen=True)
class Move:
    """One rank call: ``keys`` (in their final order) go before the item ``before`` or after the item ``after``."""

    keys: tuple[str, ...]
    before: str | None = None
    after: str | None = None


def _increasing(values: Sequence[int]) -> set[int]:
    """The indexes of one longest strictly increasing subsequence of ``values``."""

    tails: list[int] = []
    tail_at: list[int] = []
    parent: list[int | None] = []
    for index, value in enumerate(values):
        place = bisect_left(tails, value)
        if place == len(tails):
            tails.append(value)
            tail_at.append(index)
        else:
            tails[place], tail_at[place] = value, index
        parent.append(tail_at[place - 1] if place else None)
    found: set[int] = set()
    cursor = tail_at[-1] if tail_at else None
    while cursor is not None:
        found.add(cursor)
        cursor = parent[cursor]
    return found


def _runs(order: Sequence[str], moved: Collection[str]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, node in enumerate([*order, ""]):
        if node in moved and start is None:
            start = index
        elif node not in moved and start is not None:
            runs.append((start, index))
            start = None
    return runs


def plan_moves(previous: Sequence[str], new: Sequence[str]) -> list[Move]:
    """The rank calls that turn ``previous`` into ``new``. Items of ``new`` missing from ``previous`` count as
    appended at its end; items of ``previous`` missing from ``new`` are ignored. Runs go right to left."""

    known = set(previous) & set(new)
    prior = [node for node in previous if node in known]
    prior += [node for node in new if node not in known]
    place = {node: index for index, node in enumerate(new)}
    kept = {prior[index] for index in _increasing([place[node] for node in prior])}
    moves: list[Move] = []
    for start, end in reversed(_runs(new, set(new) - kept)):
        if end == len(new):
            moves += [
                Move(tuple(new[low : min(end, low + RANK_LIMIT)]), after=new[low - 1])
                for low in range(start, end, RANK_LIMIT)
            ]
        else:
            moves += [
                Move(tuple(new[max(start, stop - RANK_LIMIT) : stop]), before=new[stop])
                for stop in range(end, start, -RANK_LIMIT)
            ]
    return moves


def apply_moves(order: Sequence[str], moves: Sequence[Move]) -> list[str]:
    """What the rank calls do to ``order``: the keys leave, then land before or after their anchor."""

    result = list(order)
    for move in moves:
        result = [node for node in result if node not in move.keys]
        at = result.index(move.before) if move.before is not None else result.index(str(move.after)) + 1
        result[at:at] = move.keys
    return result
