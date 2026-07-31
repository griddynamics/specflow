"""
Tests for cross-workspace per-phase comparison and high-variance detection.
"""
from app.schemas.estimate import EstimationMetrics, PhaseEstimation, WorkspaceEstimation
from app.services.p10y.multi_workspace_estimation import (
    generate_phase_comparison,
    identify_high_variance_phases,
)


def _phase(number: int, name: str, hours: float) -> PhaseEstimation:
    return PhaseEstimation(
        phase_number=number,
        phase_name=name,
        hours=hours,
        new_work=hours,
        refactor=0.0,
        rework=0.0,
        quality_score=0.8,
    )


def _ws(name: str, phases: dict) -> WorkspaceEstimation:
    return WorkspaceEstimation(
        workspace_name=name,
        workspace_path=f"/tmp/{name}",
        total_hours=sum(p.hours for p in phases.values()),
        total_effective_output=0.0,
        phase_breakdown=phases,
        estimation_metrics=EstimationMetrics(
            new_work=0.0, refactor=0.0, rework=0.0, removed_work=0.0,
            quality_score=0.8, effective_output=0.0, total_output=0.0,
        ),
        commits_count=1,
    )


def _sample_workspaces():
    return [
        _ws("ws-1", {"06": _phase(6, "Backend", 40.0), "13": _phase(13, "Frontend", 70.0)}),
        _ws("ws-2", {"06": _phase(6, "Backend", 35.0), "13": _phase(13, "Frontend", 28.0)}),
        _ws("ws-3", {"06": _phase(6, "Backend", 30.0), "13": _phase(13, "Frontend", 33.0)}),
    ]


def test_phase_comparison_joins_on_phase_key_with_names():
    comparison = generate_phase_comparison(_sample_workspaces())

    assert set(comparison.keys()) == {"06", "13"}
    backend = comparison["06"]
    assert backend.phase_number == 6
    assert backend.phase_name == "Backend"
    assert backend.hours_by_workspace == {"ws-1": 40.0, "ws-2": 35.0, "ws-3": 30.0}
    assert backend.average == 35.0  # mean(40,35,30)
    # sample stdev of (40,35,30) is 5.0 -> 14.3%
    assert round(backend.variance_percentage, 1) == 14.3


def test_high_variance_phase_detected():
    comparison = generate_phase_comparison(_sample_workspaces())
    # Frontend hours (70,28,33) => CV > 30%; Backend (40,35,30) => < 30%.
    high = identify_high_variance_phases(comparison)
    assert high == ["13"]


def test_phase_present_in_one_workspace_has_zero_variance():
    workspaces = [
        _ws("ws-1", {"06": _phase(6, "Backend", 40.0)}),
        _ws("ws-2", {"07": _phase(7, "Auth", 10.0)}),
    ]
    comparison = generate_phase_comparison(workspaces)
    assert comparison["06"].variance_percentage == 0.0
    assert comparison["07"].variance_percentage == 0.0
    assert identify_high_variance_phases(comparison) == []


def test_unphased_bucket_compared_like_any_phase():
    workspaces = [
        _ws("ws-1", {"unphased": _phase_unphased(5.0)}),
        _ws("ws-2", {"unphased": _phase_unphased(15.0)}),
    ]
    comparison = generate_phase_comparison(workspaces)
    assert "unphased" in comparison
    assert comparison["unphased"].phase_number is None
    assert comparison["unphased"].average == 10.0


def _phase_unphased(hours: float) -> PhaseEstimation:
    return PhaseEstimation(
        phase_number=None,
        phase_name="unphased",
        hours=hours,
        new_work=hours,
        refactor=0.0,
        rework=0.0,
        quality_score=0.8,
    )
