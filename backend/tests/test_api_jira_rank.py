"""Rank moves by the longest increasing subsequence: FR-JIRA-REL-05 (sync.md#rank), pure."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import permutations

import pytest

from api.jira.rank import RANK_LIMIT, Move, plan_moves
from api.jira.rank import apply_moves as apply_to_order
from jira_sync_support import apply_moves


def longest_increasing(order: Sequence[str], target: Sequence[str]) -> int:
    place = {node: index for index, node in enumerate(target)}
    values = [place[node] for node in order]
    best = [1] * len(values)
    for i, value in enumerate(values):
        best[i] = 1 + max((best[j] for j in range(i) if values[j] < value), default=0)
    return max(best, default=0)


def test_an_unchanged_order_makes_no_move():
    assert plan_moves(["a", "b", "c"], ["a", "b", "c"]) == []


def test_new_items_appended_in_order_make_no_move():
    assert plan_moves(["a", "b"], ["a", "b", "c", "d"]) == []


def test_removed_items_are_ignored():
    assert plan_moves(["a", "x", "b"], ["a", "b"]) == []


def test_a_swap_is_one_call():
    moves = plan_moves(["a", "b"], ["b", "a"])
    assert len(moves) == 1
    assert apply_moves(["a", "b"], moves) == ["b", "a"]


def test_an_insert_in_the_middle_is_one_call():
    moves = plan_moves(["a", "c"], ["a", "b", "c"])
    assert len(moves) == 1
    assert apply_moves(["a", "c", "b"], moves) == ["a", "b", "c"]


def test_a_move_to_the_end_uses_after():
    assert plan_moves(["a", "b", "c"], ["b", "c", "a"]) == [Move(("a",), after="c")]


def test_a_move_to_the_front_uses_before():
    assert plan_moves(["b", "c", "a"], ["a", "b", "c"]) == [Move(("a",), before="b")]


def test_the_last_item_moves_after_its_predecessor():
    previous, new = ["a", "b", "c", "d"], ["a", "c", "d", "b"]
    assert plan_moves(previous, new) == [Move(("b",), after="d")]


def test_more_than_fifty_items_are_split():
    new = [f"n{i:03}" for i in range(120)]
    moves = plan_moves(list(reversed(new)), new)
    assert moves and all(len(move.keys) <= RANK_LIMIT for move in moves)
    assert any(len(move.keys) == RANK_LIMIT for move in moves)
    assert apply_moves(list(reversed(new)), moves) == new


@pytest.mark.parametrize("previous", list(permutations(["a", "b", "c", "d", "e"])))
def test_any_permutation_ends_in_the_new_order_with_minimal_moves(previous):
    new = ["a", "b", "c", "d", "e"]
    moves = plan_moves(previous, new)
    assert apply_moves(previous, moves) == new
    assert sum(len(move.keys) for move in moves) == len(new) - longest_increasing(previous, new)


def test_a_long_scrambled_order_ends_in_the_new_order_in_calls_of_at_most_fifty():
    new = [f"n{i:03}" for i in range(137)]
    previous = [*new[1::2], *new[0::2]]
    moves = plan_moves(previous, new)
    assert apply_moves(previous, moves) == new
    assert all(len(move.keys) <= RANK_LIMIT for move in moves)
    assert sum(len(move.keys) for move in moves) == len(new) - longest_increasing(previous, new)


def test_runs_go_right_to_left():
    previous, new = ["b", "a", "d", "c", "f", "e"], ["a", "b", "c", "d", "e", "f"]
    moves = plan_moves(previous, new)
    anchors = [move.before or move.after for move in moves]
    assert anchors == sorted(anchors, key=new.index, reverse=True)


def test_apply_moves_places_the_keys_before_or_after_their_anchor():
    assert apply_to_order(["a", "b", "c", "d"], [Move(("d",), before="a"), Move(("a", "b"), after="c")]) == [
        "d",
        "c",
        "a",
        "b",
    ]
