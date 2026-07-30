"""
Multi-workspace estimation logic for comparing P10Y results across workspaces.
"""

import logging
import statistics
from typing import Dict, List, Optional

from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.estimate import (
    PhaseComparison,
    PhaseEstimation,
    EstimationMetrics,
    EstimationSummary,
    WorkspaceEstimation,
)
from app.schemas.workspace import WorkspaceSettings
from app.services.p10y.p10y_lib import (
    CodeGenerationMetadata,
    Estimation,
    apply_productivity_multiplier,
    calculate_estimation,
    generate_phase_breakdown,
)

# Variance thresholds for coefficient of variation (CV)
CV_LOW = 0.15  # < 15% variance - consistent
CV_MEDIUM = 0.30  # 15-30% variance - moderate
# > 30% variance - high inconsistency

# Sentinel bucket for commits with no implementation-plan phase prefix.
UNPHASED_KEY = "unphased"


def _phase_key(phase_number: Optional[int]) -> str:
    """Stable, sort-friendly dict key for a phase (zero-padded number, or the unphased sentinel)."""
    return UNPHASED_KEY if phase_number is None else f"{phase_number:02d}"


def estimation_to_metrics(estimation: Estimation) -> EstimationMetrics:
    """Convert P10Y Estimation dataclass to EstimationMetrics Pydantic model."""
    return EstimationMetrics(
        new_work=estimation.new_work,
        refactor=estimation.refactor,
        rework=estimation.rework,
        removed_work=estimation.removed_work,
        quality_score=estimation.quality_score,
        effective_output=estimation.effective_output,
        total_output=estimation.total_output,
    )


def estimation_to_phase_estimation(
    phase_number: Optional[int],
    phase_name: str,
    estimation: Estimation,
    multiplier: float = 2.0,
) -> PhaseEstimation:
    """Convert a P10Y Estimation dataclass to a PhaseEstimation Pydantic model."""
    hours = apply_productivity_multiplier(estimation, multiplier)
    return PhaseEstimation(
        phase_number=phase_number,
        phase_name=phase_name,
        hours=hours,
        new_work=estimation.new_work,
        refactor=estimation.refactor,
        rework=estimation.rework,
        quality_score=estimation.quality_score,
    )


async def estimate_single_workspace(
    workspace: WorkspaceSettings,
    filtered_commit_stats_data: List[Dict],
    code_generation_metadata: CodeGenerationMetadata,
    logger: logging.Logger,
    multiplier: float = 2.0,
    phase_names: Optional[Dict[int, str]] = None,
    p10y_returned_commits: Optional[int] = None,
) -> Optional[WorkspaceEstimation]:
    """
    Run estimation for a single workspace.

    Args:
        workspace: Workspace configuration
        filtered_commit_stats_data: P10Y commit stats already filtered for this workspace
        code_generation_metadata: Commit metadata already loaded by the caller (same snapshot
            used to derive the P10Y allowlist — avoids a second git-log subprocess and ensures
            the commits used for the phase breakdown are identical to those used for filtering)
        logger: Logger instance
        multiplier: Productivity multiplier (default 2.0)
        phase_names: Map of phase number -> name from the shared implementation plan. Used to
            label the phase breakdown; the number is the join key across variants.
        p10y_returned_commits: Count of eligible commits P10Y returned a row for (before the
            valid-technology filter), used downstream to split coverage into service-gap vs
            non-code causes.

    Returns:
        WorkspaceEstimation or None if estimation fails
    """
    phase_names = phase_names or {}
    try:
        if not code_generation_metadata or not code_generation_metadata.commits:
            logger.warning(
                f"No commits found in git history for workspace {workspace.name} "
                f"(repo {workspace.full_workspace_path})"
            )
            return None

        # Calculate overall estimation
        estimation: Estimation = await calculate_estimation(filtered_commit_stats_data)

        # Calculate per-phase breakdown (keyed by phase number; None -> unphased)
        phase_breakdown_raw = generate_phase_breakdown(
            code_generation_metadata, filtered_commit_stats_data
        )

        # Convert to Pydantic models, resolving the human name from the shared plan.
        phase_breakdown: Dict[str, PhaseEstimation] = {}
        for phase_number, phase_est in phase_breakdown_raw.items():
            if phase_number is None:
                name = UNPHASED_KEY
            else:
                name = phase_names.get(phase_number, f"Phase {phase_number}")
            phase_breakdown[_phase_key(phase_number)] = estimation_to_phase_estimation(
                phase_number, name, phase_est, multiplier
            )

        # Calculate total hours
        total_hours = apply_productivity_multiplier(estimation, multiplier)

        return WorkspaceEstimation(
            workspace_name=workspace.name,
            workspace_path=str(workspace.workspace_path),
            total_hours=total_hours,
            total_effective_output=estimation.effective_output,
            phase_breakdown=phase_breakdown,
            estimation_metrics=estimation_to_metrics(estimation),
            commits_count=len(code_generation_metadata.commits),
            p10y_scored_commits=len(filtered_commit_stats_data),
            p10y_returned_commits=p10y_returned_commits,
            model_usage=ModelTokenUsage(model_name=workspace.model or ""),
        )

    except Exception as e:
        logger.error(
            f"Failed to estimate workspace {workspace.name}: {e}", exc_info=True
        )
        return None


def calculate_estimation_statistics(
    workspace_estimations: List[WorkspaceEstimation],
) -> EstimationSummary:
    """
    Calculate statistical summary across all workspace estimations.

    Args:
        workspace_estimations: List of workspace estimation results

    Returns:
        EstimationSummary with statistics
    """
    if not workspace_estimations:
        raise ValueError("No workspace estimations provided")

    hours_list = [ws.total_hours for ws in workspace_estimations]

    average_hours = statistics.mean(hours_list)

    if len(hours_list) > 1:
        std_deviation = statistics.stdev(hours_list)
        coefficient_of_variation = std_deviation / average_hours if average_hours > 0 else 0
    else:
        std_deviation = 0.0
        coefficient_of_variation = 0.0

    min_hours = min(hours_list)
    max_hours = max(hours_list)

    variance_assessment = assess_variance(coefficient_of_variation)

    return EstimationSummary(
        average_hours=average_hours,
        std_deviation=std_deviation,
        min_hours=min_hours,
        max_hours=max_hours,
        coefficient_of_variation=coefficient_of_variation,
        variance_assessment=variance_assessment,
    )


def assess_variance(coefficient_of_variation: float) -> str:
    """
    Classify variance level based on coefficient of variation.

    Args:
        coefficient_of_variation: CV value (std_dev / mean)

    Returns:
        "low", "medium", or "high"
    """
    if coefficient_of_variation < CV_LOW:
        return "low"
    elif coefficient_of_variation < CV_MEDIUM:
        return "medium"
    else:
        return "high"


def generate_phase_comparison(
    workspace_estimations: List[WorkspaceEstimation],
) -> Dict[str, PhaseComparison]:
    """
    Generate per-phase comparison across workspaces.

    The phase key (zero-padded number, or the unphased sentinel) is derived deterministically
    from the shared implementation plan, so the same phase joins exactly across variants — no
    fuzzy name matching needed.

    Args:
        workspace_estimations: List of workspace estimation results

    Returns:
        Dictionary mapping phase key to PhaseComparison objects
    """
    all_keys: set[str] = set()
    for ws_est in workspace_estimations:
        all_keys.update(ws_est.phase_breakdown.keys())

    phase_comparisons: Dict[str, PhaseComparison] = {}

    for key in all_keys:
        hours_by_workspace: Dict[str, float] = {}
        phase_number: Optional[int] = None
        phase_name: str = key
        for ws_est in workspace_estimations:
            phase_est = ws_est.phase_breakdown.get(key)
            if phase_est is None:
                continue
            hours_by_workspace[ws_est.workspace_name] = phase_est.hours
            phase_number = phase_est.phase_number
            phase_name = phase_est.phase_name

        if not hours_by_workspace:
            continue

        hours_values = list(hours_by_workspace.values())
        average = statistics.mean(hours_values)
        if len(hours_values) > 1:
            std_deviation = statistics.stdev(hours_values)
            variance_percentage = (std_deviation / average * 100) if average > 0 else 0
        else:
            std_deviation = 0.0
            variance_percentage = 0.0

        phase_comparisons[key] = PhaseComparison(
            phase_number=phase_number,
            phase_name=phase_name,
            hours_by_workspace=hours_by_workspace,
            average=average,
            std_deviation=std_deviation,
            variance_percentage=variance_percentage,
        )

    return phase_comparisons


def identify_high_variance_phases(
    phase_comparison: Dict[str, PhaseComparison],
    threshold: float = CV_MEDIUM,
) -> List[str]:
    """
    Identify phases with high variance across workspaces.

    Args:
        phase_comparison: Dictionary of phase comparisons
        threshold: CV threshold for high variance (default 0.30 = 30%)

    Returns:
        Sorted list of phase keys with high variance
    """
    high_variance = []

    for key, comp in phase_comparison.items():
        cv = comp.std_deviation / comp.average if comp.average > 0 else 0
        if cv > threshold:
            high_variance.append(key)

    return sorted(high_variance)


def generate_insights(
    workspace_estimations: List[WorkspaceEstimation],
    summary: EstimationSummary,
    high_variance_phases: List[str],
) -> List[str]:
    """
    Generate insights about variance and potential root causes.

    Args:
        workspace_estimations: List of workspace estimation results
        summary: Statistical summary
        high_variance_phases: List of phase keys with high variance

    Returns:
        List of insight strings
    """
    insights = []

    # Human-friendly phase labels for messages (name resolved from the shared plan).
    phase_label = {
        key: pe.phase_name
        for ws in workspace_estimations
        for key, pe in ws.phase_breakdown.items()
    }

    # Overall variance assessment
    if summary.variance_assessment == "low":
        insights.append(
            "Overall variance is low (CV < 15%), indicating consistent implementation across workspaces."
        )
    elif summary.variance_assessment == "medium":
        insights.append(
            "Overall variance is moderate (CV 15-30%), suggesting some differences in approach but within acceptable range."
        )
    else:
        insights.append(
            "Overall variance is high (CV > 30%), indicating significant inconsistencies that warrant investigation."
        )

    # Commit count variance
    commit_counts = [ws.commits_count for ws in workspace_estimations]
    if len(commit_counts) > 1:
        commit_cv = (
            statistics.stdev(commit_counts) / statistics.mean(commit_counts)
            if statistics.mean(commit_counts) > 0
            else 0
        )
        if commit_cv > 0.3:
            insights.append(
                f"Large variance in commit counts ({min(commit_counts)}-{max(commit_counts)}) suggests different interpretation of specifications or commit strategies."
            )

    # Phase coverage differences (with a shared plan this should trend high)
    phase_sets = [set(ws.phase_breakdown.keys()) for ws in workspace_estimations]
    if len(phase_sets) > 1:
        common_phases = set.intersection(*phase_sets)
        all_phases = set.union(*phase_sets)
        coverage = len(common_phases) / len(all_phases) if all_phases else 1.0

        if coverage < 0.7:
            insights.append(
                f"Only {coverage*100:.0f}% of plan phases produced measurable work across all workspaces, indicating some phases diverged in scope or commit hygiene."
            )

    # High variance phases
    if high_variance_phases:
        labels = [phase_label.get(k, k) for k in high_variance_phases[:3]]
        insights.append(
            f"Phases with high variance ({', '.join(labels)}) may require specification clarification or a standardized implementation approach."
        )

    # Quality score variance
    quality_scores = [ws.estimation_metrics.quality_score for ws in workspace_estimations]
    if len(quality_scores) > 1:
        quality_cv = (
            statistics.stdev(quality_scores) / statistics.mean(quality_scores)
            if statistics.mean(quality_scores) > 0
            else 0
        )
        if quality_cv > 0.2:
            insights.append(
                f"Quality scores vary significantly (CV {quality_cv*100:.1f}%), suggesting different code quality standards or testing approaches."
            )

    # Work type ratio analysis
    for ws in workspace_estimations:
        metrics = ws.estimation_metrics
        total_work = metrics.new_work + metrics.refactor + metrics.rework
        if total_work > 0:
            refactor_ratio = metrics.refactor / total_work
            if refactor_ratio > 0.4:
                insights.append(
                    f"Workspace '{ws.workspace_name}' has high refactor ratio ({refactor_ratio*100:.0f}%), indicating significant code restructuring."
                )

    return insights
