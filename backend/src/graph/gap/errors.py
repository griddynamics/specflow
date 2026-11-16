"""Typed refusals of ``graph.gap`` (docs/graph/gap/api.md "Errors").

Every error is a ``GraphError`` with a stable ``code``; fields carry ids, statuses and counts, never document text,
questions or answers (NFR-GSEC-02). Invalid rules or settings raise the core ``ConfigurationError`` (``GapRulesError``
lists every problem of a rules file).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, override

from graph.core.errors import ConfigurationError, GraphError

GAP_NOT_FOUND: Final = "gap_not_found"
GAP_WRONG_STATE: Final = "gap_wrong_state"
ANSWER_INVALID: Final = "answer_invalid"
GAP_STALE: Final = "gap_stale"
INVALID_GAP_RULES: Final = "invalid_gap_rules"
INVALID_GAP_SETTINGS: Final = "invalid_gap_settings"


class GapNotFoundError(GraphError):
    """The gap or clarification id is unknown in the project; ``gap_id`` or ``clr_id`` names it."""

    def __init__(self, *, gap_id: str | None = None, clr_id: str | None = None) -> None:
        self.gap_id = gap_id
        self.clr_id = clr_id
        super().__init__(GAP_NOT_FOUND, gap_id=gap_id, clr_id=clr_id)


class GapStateError(GraphError):
    """The call does not apply to the current status; ``status`` is the current one, ``allowed`` the ones it needs."""

    def __init__(
        self,
        *,
        status: str,
        allowed: Sequence[str],
        gap_id: str | None = None,
        clr_id: str | None = None,
    ) -> None:
        self.status = status
        self.allowed: tuple[str, ...] = tuple(allowed)
        super().__init__(
            GAP_WRONG_STATE,
            status=status,
            allowed=",".join(self.allowed),
            gap_id=gap_id,
            clr_id=clr_id,
        )


class AnswerInvalidError(GraphError):
    """An answer, a raised gap or a noise rule fails validation; ``field`` and ``reason`` name why."""

    def __init__(self, *, field: str, reason: str, gap_id: str | None = None) -> None:
        self.field = field
        self.reason = reason
        super().__init__(ANSWER_INVALID, field=field, reason=reason, gap_id=gap_id)


class StaleGapError(GraphError):
    """The gap changed after the caller read it: its ``updated_at`` differs from the one the caller saw."""

    def __init__(self, *, gap_id: str) -> None:
        self.gap_id = gap_id
        super().__init__(GAP_STALE, gap_id=gap_id)


class GapRulesError(ConfigurationError):
    """An invalid gap rules file; ``problems`` lists every problem found (FR-GONT-03)."""

    def __init__(self, problems: Sequence[str], **fields: str | int | float | bool | None) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__(INVALID_GAP_RULES, problems=len(self.problems), **fields)

    @override
    def __str__(self) -> str:
        return "\n".join([super().__str__(), *(f"- {problem}" for problem in self.problems)])


class FindingError(GraphError):
    """First slice: a state refusal: ``node_not_found`` (``spec_ids`` names the ids), ``finding_limit``,
    ``unknown_finding`` or ``wrong_state``. Carries ids and counts, never finding text."""

    def __init__(self, code: str, *, spec_ids: Sequence[str] = (), **fields: str | int | None) -> None:
        self.spec_ids: tuple[str, ...] = tuple(spec_ids)
        extra = {"spec_ids": ",".join(self.spec_ids)} if self.spec_ids else {}
        super().__init__(code, **extra, **fields)
