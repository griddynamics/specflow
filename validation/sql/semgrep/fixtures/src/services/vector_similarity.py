"""Real-scan fixture for store_work.yml's `paths:` scoping: the one module that compares
vectors in Python is outside the hand-rolled vector maths rule."""

from __future__ import annotations


def dot(first: list[float], second: list[float]) -> float:
    # ok: kit.store_work.hand-rolled-vector-maths
    return sum(a * b for a, b in zip(first, second, strict=True))
