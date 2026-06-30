"""Pure formatters turning a ``/status`` payload into display-ready data.

Every function here is pure (no I/O, no Rich/Textual import) so it is unit
testable from fixture payloads alone. ``app.py`` consumes these structures and
turns them into Textual widgets; this keeps all formatting logic in one place
and out of the UI layer.

Field names match the payload built by
``backend/app/api/v1/generation_sessions.py`` ``get_generation_session_status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tui.constants import (
    CHECKPOINT_STEPS,
    DEPLOY_CHECKPOINT,
    LOCAL_ONLY_READINESS,
    STATUS_PILLS,
    STEP_SYMBOLS,
    StepState,
)

_IN_PROGRESS_STATUSES = frozenset({"running", "initializing", "pending"})


@dataclass(frozen=True)
class PipelineStep:
    """One step in the checkpoint stepper."""

    label: str
    state: StepState

    @property
    def symbol(self) -> str:
        return STEP_SYMBOLS[self.state]


@dataclass(frozen=True)
class WorkspaceBar:
    """Per-workspace progress row."""

    workspace_id: str
    phase_name: str
    last_completed_phase: int
    total_phases: int | None

    @property
    def fraction(self) -> float:
        """Completed fraction in [0, 1]; 0 when total is unknown/zero."""
        if not self.total_phases or self.total_phases <= 0:
            return 0.0
        return max(0.0, min(1.0, self.last_completed_phase / self.total_phases))

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)

    @property
    def phase_label(self) -> str:
        """e.g. 'Phase 6/9' — '?' for total when unknown."""
        total = self.total_phases if self.total_phases else "?"
        return f"Phase {self.last_completed_phase}/{total}"


@dataclass(frozen=True)
class EstimatePanel:
    """Completed-run estimation summary, flattened for display."""

    average_hours: float | None
    min_hours: float | None
    max_hours: float | None
    coefficient_of_variation: float | None
    variance_assessment: str | None
    risk_status: str | None
    total_buffer_pct: float | None
    final_estimate: float | None
    per_workspace: list[tuple[str, float]] = field(default_factory=list)
    total_usd_cost: float | None = None


def status_pill(status: str | None) -> tuple[str, str]:
    """Return (text, Rich style) for a lifecycle status string."""
    key = (status or "unknown").lower()
    return STATUS_PILLS.get(key, STATUS_PILLS["unknown"])


def _relevant_steps(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Checkpoint steps relevant to this run.

    Local-only runs (the primary TUI audience) never deploy or run E2E, so that
    step is dropped — leaving it as a permanently-pending row is misleading.
    """
    readiness = (payload.get("last_spec_readiness") or "").upper()
    if readiness == LOCAL_ONLY_READINESS:
        return [step for step in CHECKPOINT_STEPS if step[0] != DEPLOY_CHECKPOINT]
    return list(CHECKPOINT_STEPS)


def pipeline_steps(payload: dict[str, Any]) -> list[PipelineStep]:
    """Build the checkpoint stepper from ``status`` + ``checkpoint``.

    Steps at or before the current checkpoint are DONE; the next step is ACTIVE
    while the run is still in progress; the rest are PENDING. A COMPLETED run
    marks every step DONE; an unknown checkpoint leaves the first step ACTIVE.
    Deploy & E2E is omitted for local-only runs (see ``_relevant_steps``), and
    done/active indices are computed within the relevant set so the next real
    step is highlighted even when deploy is skipped.
    """
    steps_def = _relevant_steps(payload)
    status = (payload.get("status") or "").lower()
    if status == "completed":
        return [PipelineStep(label, StepState.DONE) for _, label in steps_def]

    order = [key for key, _ in steps_def]
    checkpoint = payload.get("checkpoint") or ""
    try:
        current_idx = order.index(checkpoint)
    except ValueError:
        current_idx = -1

    in_progress = status in _IN_PROGRESS_STATUSES
    active_idx = current_idx + 1 if in_progress else -1

    steps: list[PipelineStep] = []
    for idx, (_, label) in enumerate(steps_def):
        if idx <= current_idx:
            state = StepState.DONE
        elif idx == active_idx:
            state = StepState.ACTIVE
        else:
            state = StepState.PENDING
        steps.append(PipelineStep(label, state))
    return steps


def _workspace_phases(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the per-workspace phase map, top-level first then legacy nesting."""
    workspace_phases = payload.get("workspace_phases")
    if not workspace_phases:
        workspace_phases = (payload.get("progress") or {}).get("workspace_phases") or {}
    return workspace_phases or {}


def workspace_bars(payload: dict[str, Any]) -> list[WorkspaceBar]:
    """Build per-workspace progress rows from ``workspace_phases``.

    Reads the top-level ``workspace_phases`` the status endpoint returns, with a
    fallback to the older ``progress.workspace_phases`` nesting. Returns an empty
    list when the backend has not yet reported phases. Rows are ordered by
    workspace id for stable rendering.
    """
    workspace_phases = _workspace_phases(payload)
    bars: list[WorkspaceBar] = []
    for ws_id in sorted(workspace_phases):
        data = workspace_phases[ws_id] or {}
        bars.append(
            WorkspaceBar(
                workspace_id=ws_id,
                phase_name=data.get("phase_name") or "",
                last_completed_phase=int(data.get("last_completed_phase") or 0),
                total_phases=data.get("total_phases"),
            )
        )
    return bars


def progress_bar(fraction: float, width: int = 12) -> str:
    """Render an ASCII progress bar of ``width`` cells for a [0, 1] fraction."""
    width = max(1, width)
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


def tokens_summary(payload: dict[str, Any]) -> str:
    """One-line token/turns summary using the display string the backend sends."""
    display = payload.get("total_tokens_used_display")
    turns = payload.get("num_turns")
    parts: list[str] = []
    if display:
        parts.append(f"Tokens {display}")
    if turns:
        parts.append(f"{turns} turns")
    return "   ".join(parts)


def set_number_from_workspace_id(ws_id: str) -> int | None:
    """Parse set number from a workspace id like ``ws-01-1`` → ``1``."""
    parts = ws_id.split("-")
    if len(parts) < 3 or parts[0] != "ws":
        return None
    try:
        return int(parts[1], 10)
    except ValueError:
        return None


def run_set_number(payload: dict[str, Any] | None) -> int | None:
    """Derive the workspace set number for a run from its workspace phase keys."""
    if not payload:
        return None
    set_numbers: set[int] = set()
    for bar in workspace_bars(payload):
        set_no = set_number_from_workspace_id(bar.workspace_id)
        if set_no is None:
            return None
        set_numbers.add(set_no)
    if len(set_numbers) != 1:
        return None
    return next(iter(set_numbers))


def clear_ws_eligible(set_no: int | None, cleaning_set_numbers: set[int]) -> bool:
    """True when the run's set is present in the pool's ``cleaning_sets``."""
    return set_no is not None and set_no in cleaning_set_numbers


def clear_ws_ineligible_message(payload: dict[str, Any] | None) -> str:
    """Explanatory text when clear-ws is pressed but the set is not in CLEANING."""
    status = ((payload or {}).get("status") or "").lower()
    if status in _IN_PROGRESS_STATUSES:
        return (
            "This generation is still running and its workspaces are "
            "allocated. They cannot be cleared until the run is complete."
        )
    return "Nothing to clear — these workspaces are not awaiting cleanup."


def estimate_panel(result: dict[str, Any] | None) -> EstimatePanel | None:
    """Flatten a ``MultiWorkspaceEstimationResponse`` dict for display.

    Returns None when no result is present (run not COMPLETED). Tolerant of
    missing nested fields — every access is defensive so a partial result still
    renders what it has.
    """
    if not result:
        return None

    summary = result.get("summary") or {}
    risk = summary.get("risk_assessment") or {}

    per_workspace: list[tuple[str, float]] = []
    for ws in result.get("workspace_estimations") or []:
        name = ws.get("workspace_name")
        hours = ws.get("total_hours")
        if name is not None and hours is not None:
            per_workspace.append((name, float(hours)))

    return EstimatePanel(
        average_hours=summary.get("average_hours"),
        min_hours=summary.get("min_hours"),
        max_hours=summary.get("max_hours"),
        coefficient_of_variation=summary.get("coefficient_of_variation"),
        variance_assessment=summary.get("variance_assessment"),
        risk_status=risk.get("status"),
        total_buffer_pct=risk.get("total_buffer_pct"),
        final_estimate=risk.get("final_estimate"),
        per_workspace=per_workspace,
        total_usd_cost=result.get("total_usd_cost"),
    )


# ---------------------------------------------------------------------------
# Workspace drill-in: live message stream + per-workspace stats
# ---------------------------------------------------------------------------


# Rich style name per message kind for the live-feed row. Kept here (as plain
# strings, no Rich import) so this module stays the single source of truth for
# how the message flow is presented; app.py only assembles the Text from these.
KIND_STYLES: dict[str, str] = {
    "assistant_text": "white",
    "tool_use": "cyan",
    "tool_result": "green",
    "result": "bold green",
    "system": "dim",
    "unknown": "dim",
}


def kind_style(kind: str) -> str:
    """Rich style name for a message kind ("" when unknown to keep callers simple)."""
    return KIND_STYLES.get(kind, "")


@dataclass(frozen=True)
class StreamRow:
    """A single compact row in the live message feed, display-ready."""

    time: str
    kind: str
    label: str  # tool / subagent name, or "" when not applicable
    message: str


def _hhmmss(timestamp: str | None) -> str:
    """Format an ISO timestamp as HH:MM:SS; "" when missing/unparseable."""
    if not timestamp:
        return ""
    try:
        return datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
    except ValueError:
        return ""


def stream_row(event: Any) -> StreamRow:
    """Flatten an ``AgentStreamEvent`` (or attr-compatible object) into a row.

    The label prefers a subagent name (Task/Agent calls) over the raw tool name
    so subagent activity reads clearly; both are optional.
    """
    label = getattr(event, "subagent_name", None) or getattr(event, "tool_name", None) or ""
    return StreamRow(
        time=_hhmmss(getattr(event, "timestamp", None)),
        kind=str(getattr(event, "kind", None) or "unknown"),
        label=str(label),
        message=str(getattr(event, "message", None) or ""),
    )


@dataclass(frozen=True)
class WorkspaceStats:
    """Per-workspace stats for the drill-in panel, flattened for display.

    Token/turn fields are optional: they come from the status payload's
    per-workspace ``usage`` block which is only present once the backend has
    recorded agent usage for that workspace.
    """

    workspace_id: str
    models: list[str]
    phase_name: str
    last_completed_phase: int
    total_phases: int | None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def fraction(self) -> float:
        if not self.total_phases or self.total_phases <= 0:
            return 0.0
        return max(0.0, min(1.0, self.last_completed_phase / self.total_phases))

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)

    @property
    def phase_label(self) -> str:
        total = self.total_phases if self.total_phases else "?"
        return f"Phase {self.last_completed_phase}/{total}"

    @property
    def has_usage(self) -> bool:
        """True once any turn/token figure has been recorded for the workspace.

        Token/turn usage is only recorded when an agent step *completes* (the SDK
        reports cumulative usage in its terminal message), so a step that is still
        running has no usage yet — callers show a "pending" hint instead of zeros.
        """
        return any(
            v is not None
            for v in (
                self.num_turns,
                self.input_tokens,
                self.output_tokens,
                self.cache_write_tokens,
                self.cache_read_tokens,
                self.total_tokens,
            )
        )


def workspace_stats(payload: dict[str, Any], workspace_id: str) -> WorkspaceStats | None:
    """Build per-workspace stats from the status payload, or None if absent.

    Reads the same ``workspace_phases`` map the dashboard polls (no extra call),
    including the optional per-workspace ``usage`` token buckets and ``models``
    list added to the status endpoint. Returns None when the workspace has not
    been reported yet so the caller can show a placeholder.
    """
    phases = _workspace_phases(payload)
    data = phases.get(workspace_id)
    if data is None:
        return None
    data = data or {}
    usage = data.get("usage") or {}
    models = [str(m) for m in (data.get("models") or [])]

    def _opt(key: str) -> int | None:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    return WorkspaceStats(
        workspace_id=workspace_id,
        models=models,
        phase_name=data.get("phase_name") or "",
        last_completed_phase=int(data.get("last_completed_phase") or 0),
        total_phases=data.get("total_phases"),
        num_turns=_opt("num_turns"),
        input_tokens=_opt("input_tokens"),
        output_tokens=_opt("output_tokens"),
        cache_write_tokens=_opt("cache_write_tokens"),
        cache_read_tokens=_opt("cache_read_tokens"),
        total_tokens=_opt("total_tokens"),
    )


def format_tokens(value: int | None) -> str:
    """Compact human token count (e.g. 12_400_000 → '12.4M'); '—' when None."""
    if value is None:
        return "—"
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}K"
    return f"{value / 1_000_000:.1f}M"
