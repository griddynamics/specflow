"""Pure analysis the checks share: exception-handler coverage and document comparisons.

Nothing here imports the project under check. Every function takes plain data, so the
kit's self-tests drive each one with synthetic input."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable
from typing import Any


def uncovered_handler_classes(
    handler_keys: Iterable[object], probe_types: Iterable[type], ignored: Iterable[type]
) -> list[str]:
    """Handled exception classes no probe instance reaches (a subclass probe covers a base)."""
    probes, skipped = list(probe_types), set(ignored)
    uncovered: list[str] = []
    for key in handler_keys:
        if not isinstance(key, type) or not issubclass(key, BaseException) or key in skipped:
            continue
        if not any(issubclass(probe, key) for probe in probes):
            uncovered.append(key.__name__)
    return sorted(uncovered)


def _canonical_lines(document: Any) -> list[str]:
    return json.dumps(document, indent=2, sort_keys=True).splitlines(keepends=True)


def snapshot_diff(
    committed: Any, live: Any, *, committed_label: str = "committed", live_label: str = "live"
) -> str | None:
    """`None` when equal, otherwise a unified diff of the two canonical JSON texts."""
    if committed == live:
        return None
    return "".join(
        difflib.unified_diff(
            _canonical_lines(committed),
            _canonical_lines(live),
            fromfile=committed_label,
            tofile=live_label,
        )
    )
