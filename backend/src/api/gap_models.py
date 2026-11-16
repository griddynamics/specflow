"""Response models of the gap routes (``api.routers.gaps``) beyond the ``graph.gap`` read models they return as is
(``GapPage``, ``GapView``, ``NoiseReport``, ``GapRunResult``; docs/graph/gap/api.md "Models")."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from graph.gap import GapRunResult
from graph.gap.names import GapKind, GapOrigin, GapRoute, GapSeverity, GapStatus

MAX_ID: Final = 200
MAX_FILTER_VALUES: Final = 50
MAX_CURSOR: Final = 500
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200


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
