"""
Phase labels in the P10Y breakdown come from the shared implementation plan.

The plan is persisted per workspace under ``workspace_phases[ws_id]["planning_data"]`` —
reading a top-level ``planning_data`` finds nothing and silently degrades every row to a
label that only repeats the phase number.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from app.schemas.workspace import WorkspaceSettings
from app.services.p10y.multi_workspace_estimation import (
    UNNAMED_PHASE,
    estimate_single_workspace,
)
from app.services.p10y.p10y_lib import CodeGenerationMetadata, CommitInfo
from app.workflows.multi_workspace_estimation_p10y import _load_phase_names

LOGGER = logging.getLogger("test_phase_names")


def _adapter(doc: dict | None) -> Mock:
    adapter = Mock()
    adapter.get_generation_session = AsyncMock(return_value=doc)
    return adapter


def _plan(*phases: tuple[int, str]) -> dict:
    return {"phases": [{"number": n, "name": name} for n, name in phases]}


@pytest.mark.asyncio
async def test_reads_names_from_workspace_phases() -> None:
    """The plan lives under workspace_phases, not at the document root."""
    doc = {
        "workspace_phases": {
            "ws-05-1": {
                "last_completed_phase": 2,
                "total_phases": 2,
                "planning_data": _plan((1, "Repo Scaffold & Tooling"), (2, "Hold lifecycle")),
            }
        }
    }
    assert await _load_phase_names("gen-1", _adapter(doc), LOGGER) == {
        1: "Repo Scaffold & Tooling",
        2: "Hold lifecycle",
    }


@pytest.mark.asyncio
async def test_top_level_planning_data_is_not_where_the_plan_lives() -> None:
    """Guards the regression: a root-level planning_data must not be the only place we look."""
    doc = {"planning_data": _plan((1, "Root Level"))}
    assert await _load_phase_names("gen-1", _adapter(doc), LOGGER) == {}


@pytest.mark.asyncio
async def test_first_workspace_entry_with_phases_wins() -> None:
    """Every workspace carries the same plan; entries without one are skipped."""
    doc = {
        "workspace_phases": {
            "ws-05-1": {"last_completed_phase": 0},
            "ws-05-2": {"planning_data": _plan((3, "Backend Domain"))},
        }
    }
    assert await _load_phase_names("gen-1", _adapter(doc), LOGGER) == {3: "Backend Domain"}


@pytest.mark.asyncio
async def test_unusable_phase_entries_are_ignored() -> None:
    doc = {
        "workspace_phases": {
            "ws-1": {
                "planning_data": {
                    "phases": [
                        {"number": 1, "name": "Kept"},
                        {"number": "2", "name": "Non-int number"},
                        {"number": 3, "name": ""},
                        {"name": "No number"},
                    ]
                }
            }
        }
    }
    assert await _load_phase_names("gen-1", _adapter(doc), LOGGER) == {1: "Kept"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc",
    [None, {}, {"workspace_phases": {}}, {"workspace_phases": {"ws-1": {"planning_data": {}}}}],
)
async def test_missing_plan_yields_no_names(doc) -> None:
    assert await _load_phase_names("gen-1", _adapter(doc), LOGGER) == {}


@pytest.mark.asyncio
async def test_no_generation_id_or_adapter_skips_the_read() -> None:
    adapter = _adapter({"workspace_phases": {}})
    assert await _load_phase_names(None, adapter, LOGGER) == {}
    assert await _load_phase_names("gen-1", None, LOGGER) == {}
    adapter.get_generation_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_failure_is_non_fatal() -> None:
    adapter = Mock()
    adapter.get_generation_session = AsyncMock(side_effect=RuntimeError("firestore down"))
    assert await _load_phase_names("gen-1", adapter, LOGGER) == {}


# ---------------------------------------------------------------------------
# How the resolved names reach the breakdown labels
# ---------------------------------------------------------------------------

def _commit_stats(sha: str, fp: float) -> dict:
    return {
        "sha": sha,
        "fp_delta_total": fp,
        "commit_quality_score": 0.8,
        "churn_rate": 0.1,
        "refactor": 0.0,
        "rework": 0.0,
        "new_work": fp,
        "removed_work": 0.0,
        "quality_score": 0.8,
        "effective_output": fp,
        "total_output": fp,
    }


async def _estimate_with(phase_names: dict[int, str] | None):
    workspace = WorkspaceSettings(workspace_path="/tmp/ws-1", provider="openrouter", model="m", name="ws-1")
    metadata = CodeGenerationMetadata(
        commits=[CommitInfo(sha="abc123", message="p03_add endpoint", phase=3)]
    )
    return await estimate_single_workspace(
        workspace=workspace,
        filtered_commit_stats_data=[_commit_stats("abc123", 5.0)],
        code_generation_metadata=metadata,
        logger=LOGGER,
        phase_names=phase_names,
    )


@pytest.mark.asyncio
async def test_plan_name_becomes_the_phase_label() -> None:
    est = await _estimate_with({3: "Backend — Projection & Modes"})
    assert est is not None
    assert est.phase_breakdown["03"].phase_name == "Backend — Projection & Modes"
    assert est.phase_breakdown["03"].phase_number == 3


@pytest.mark.asyncio
async def test_unnamed_phase_does_not_repeat_the_number() -> None:
    """Without a name the label must not become "Phase 3" — the number has its own column."""
    est = await _estimate_with(None)
    assert est is not None
    label = est.phase_breakdown["03"]
    assert label.phase_number == 3
    assert label.phase_name == UNNAMED_PHASE
    assert "Phase 3" not in label.phase_name
