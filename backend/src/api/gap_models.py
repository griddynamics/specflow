"""Response models of the gap routes (``api.routers.gaps``) beyond the ``graph.gap`` read models they return as is
(``GapPage``, ``GapView``, ``NoiseReport``, ``GapRunResult``; docs/graph/gap/api.md "Models")."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from graph.gap import GapRunResult
from graph.gap.models import Edit, NoiseRuleRow, Quote
from graph.gap.names import EditPlacement, GapKind, GapOrigin, GapRoute, GapSeverity, GapStatus, NoiseScope

MAX_ID: Final = 200
MAX_FILTER_VALUES: Final = 50
MAX_CURSOR: Final = 500
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
MAX_TEXT: Final = 4000
MAX_REASON: Final = 500
MAX_QUESTION: Final = 1000
MAX_ABOUT: Final = 20
MAX_QUOTES: Final = 6
MAX_EDITS: Final = 12


class GapListQuery(BaseModel):
    """The query parameters of ``GET .../gaps`` (``GapService.list_gaps``); a list filter repeats its parameter."""

    status: list[GapStatus] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    kind: list[GapKind] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    severity: list[GapSeverity] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    origin: list[GapOrigin] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    route: list[GapRoute] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    about: list[str] | None = Field(default=None, max_length=MAX_FILTER_VALUES)
    story_id: str | None = Field(default=None, min_length=1, max_length=MAX_ID)
    document: str | None = Field(default=None, min_length=1, max_length=MAX_ID)
    held_back: bool | None = None
    cursor: str | None = Field(default=None, min_length=1, max_length=MAX_CURSOR)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


class GapRunStatus(BaseModel):
    """``.../gap/runs``: the project's latest gap run; ids, step names, counts and codes only.

    ``status`` is ``queued`` while runs wait and none runs, ``running`` while one runs, else the latest run's outcome
    (``completed``, ``degraded``, ``failed``) or ``idle`` before the first run. ``step`` is the step that runs (or the
    last one that ran); ``steps_done`` of ``steps_total`` are done; while a step reports items, ``step_items_done`` of
    ``step_items_total``. ``queued`` counts the runs waiting. ``result`` is the latest finished run's result.
    """

    status: Literal["idle", "queued", "running", "completed", "degraded", "failed"]
    run_id: str | None = None
    mode: str | None = None
    build_id: str | None = None
    step: str | None = None
    steps_done: int = 0
    steps_total: int = 0
    step_items_done: int | None = None
    step_items_total: int | None = None
    queued: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: dict[str, Any] = {}
    error_code: str | None = None
    result: GapRunResult | None = None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GapEditsPreviewBody(_Body):
    """``POST .../gaps/{gap_id}/edits_preview``: an offered option by rank, or the person's own text (exactly one), and
    an optional placement; exactly one of the two, else ``400 answer_invalid`` (``field`` ``answer``)."""

    option_rank: int | None = Field(default=None, ge=1, le=4)
    own_text: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    placement: EditPlacement | None = None

    @property
    def one_answer(self) -> bool:
        """Exactly one of ``option_rank`` and ``own_text`` (else the route answers ``400 answer_invalid``)."""

        return (self.option_rank is None) != (self.own_text is None)


class GapAnswerBody(GapEditsPreviewBody):
    """``POST .../gaps/{gap_id}/answer``: the answer, the edits confirmed in the preview (optional), and the gap's
    ``updated_at`` as the card showed it (a gap changed since then answers ``409 gap_stale``)."""

    edits: list[Edit] | None = Field(default=None, min_length=1, max_length=MAX_EDITS)
    updated_at: datetime


class GapDismissBody(_Body):
    """``POST .../gaps/{gap_id}/dismiss``: why the gap is not one ("not a gap", "not spec", "duplicate", "won't
    decide now" or free text)."""

    reason: str = Field(min_length=1, max_length=MAX_REASON)


class GapRevertBody(_Body):
    """``POST .../gaps/clarifications/{clr_id}/revert``: why the answer is withdrawn (kept in the event log)."""

    reason: str = Field(min_length=1, max_length=MAX_REASON)


class GapRaiseBody(_Body):
    """``POST .../gaps``: a person's question about spec ids, with optional quoted lines, kind and severity."""

    question: str = Field(min_length=10, max_length=MAX_QUESTION)
    kind: GapKind = "missing"
    about: list[str] = Field(default_factory=list, max_length=MAX_ABOUT)
    quotes: list[Quote] = Field(default_factory=list, max_length=MAX_QUOTES)
    severity: GapSeverity | None = None


class NoiseRuleBody(_Body):
    """``POST .../gaps/noise_rules``: a document (lineage or path), a section or a pattern that is not spec."""

    scope: NoiseScope
    value: str = Field(min_length=1, max_length=MAX_REASON)
    reason: str = Field(min_length=1, max_length=MAX_REASON)


class NoiseRuleCreated(BaseModel):
    """The id of the added noise rule (the same scope and value is one rule)."""

    rule_id: str


class NoiseRuleList(BaseModel):
    """The project's noise rules, oldest first."""

    rules: list[NoiseRuleRow]
