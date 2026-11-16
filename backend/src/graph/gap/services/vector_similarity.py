"""The one module of the project that compares vectors in Python (the store-work rule's sanctioned module).

Only vectors no store holds are compared here: the embeddings of gap questions taken for the duplicate check
(docs/graph/gap/investigation.md "Checks"), which are never stored as vectors. Every comparison of stored vectors
goes through the store's vector index or similarity function.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    """The cosine of two vectors; 0 when either has no length."""

    norm = math.hypot(*first) * math.hypot(*second)
    return math.sumprod(first, second) / norm if norm else 0.0
