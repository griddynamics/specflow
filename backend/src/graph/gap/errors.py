"""Typed refusals of the findings library."""

from __future__ import annotations

from collections.abc import Sequence

from graph.core.errors import GraphError


class FindingError(GraphError):
    """A state refusal: ``node_not_found`` (``spec_ids`` names the ids), ``finding_limit``, ``unknown_finding`` or
    ``wrong_state``. Carries ids and counts, never finding text."""

    def __init__(self, code: str, *, spec_ids: Sequence[str] = (), **fields: str | int | None) -> None:
        self.spec_ids: tuple[str, ...] = tuple(spec_ids)
        extra = {"spec_ids": ",".join(self.spec_ids)} if self.spec_ids else {}
        super().__init__(code, **extra, **fields)
