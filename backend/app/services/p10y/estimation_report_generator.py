"""
Report generation for multi-workspace P10Y estimations.

This module provides functions to format and visualize estimation data
in markdown format for comprehensive reporting.
"""

from typing import Dict, List, Optional

from app.schemas.estimate import (
    ComparativeAnalysis,
    ComponentComparison,
    EstimationSummary,
    SkippedWorkspaceP10Y,
    WorkspaceEstimation,
)


def create_comparison_table(
    workspace_estimations: List[WorkspaceEstimation],
    component_comparison: Dict[str, ComponentComparison],
) -> str:
    """
    Create a markdown table comparing components across workspaces.
    
    Args:
        workspace_estimations: List of workspace estimation results
        component_comparison: Component comparison data
    
    Returns:
        Markdown formatted comparison table
    """
    if not component_comparison:
        return "_No components to compare_\n"
    
    # Get all workspace names for column headers
    workspace_names = [ws.workspace_name for ws in workspace_estimations]
    
    # Build table header
    header = "| Component | " + " | ".join(workspace_names) + " | Average | Std Dev | Variance % |\n"
    separator = "|" + "---|" * (len(workspace_names) + 4) + "\n"
    
    # Build table rows
    rows = []
    for comp_name, comp_comp in sorted(component_comparison.items()):
        row_parts = [f"**{comp_name}**"]
        
        # Add hours for each workspace (or "-" if component not present)
        for ws_name in workspace_names:
            hours = comp_comp.hours_by_workspace.get(ws_name, 0.0)
            if hours > 0:
                row_parts.append(f"{hours:.1f}h")
            else:
                row_parts.append("-")
        
        # Add average, std dev, and variance
        row_parts.append(f"{comp_comp.average:.1f}h")
        row_parts.append(f"{comp_comp.std_deviation:.1f}h")
        row_parts.append(f"{comp_comp.variance_percentage:.1f}%")
        
        rows.append("| " + " | ".join(row_parts) + " |")
    
    return header + separator + "\n".join(rows) + "\n"


def visualize_variance(summary: EstimationSummary) -> str:
    """
    Create ASCII/markdown visualization of variance.
    
    Args:
        summary: Estimation summary with statistics
    
    Returns:
        Markdown formatted variance visualization
    """
    cv_percent = summary.coefficient_of_variation * 100
    
    # Create a simple bar chart representation
    bar_length = 50
    filled_length = min(int((cv_percent / 100) * bar_length), bar_length)
    bar = "█" * filled_length + "░" * (bar_length - filled_length)
    
    # Determine color indicator based on variance level
    if summary.variance_assessment == "low":
        indicator = "🟢"
        message = "Excellent consistency across workspaces"
    elif summary.variance_assessment == "medium":
        indicator = "🟡"
        message = "Acceptable variance, some differences in approach"
    else:  # high
        indicator = "🔴"
        message = "High variance - spec clarity or implementation differences"
    
    visualization = f"""
### Variance Visualization

**Coefficient of Variation: {cv_percent:.1f}%** {indicator}

```
0%                          50%                         100%
|{bar}|
```

**Assessment**: {summary.variance_assessment.upper()} - {message}

- **Average Hours**: {summary.average_hours:.1f}h ± {summary.std_deviation:.1f}h
- **Range**: {summary.min_hours:.1f}h - {summary.max_hours:.1f}h
- **Spread**: {summary.max_hours - summary.min_hours:.1f}h ({_spread_pct_of_average(summary):.1f}% of average)
"""
    
    return visualization


def _spread_pct_of_average(summary: EstimationSummary) -> float:
    if summary.average_hours <= 0:
        return 0.0
    return ((summary.max_hours - summary.min_hours) / summary.average_hours) * 100.0


def _pct_of_total_suffix(value: float, total: float) -> str:
    """Return `  (X.X%)` for value/total, or empty when total is zero (avoid division by zero)."""
    if total <= 0:
        return ""
    return f"  ({(value / total * 100):.1f}%)"


def _p10y_note_for_workspace(ws: WorkspaceEstimation) -> str:
    """One line per workspace when P10Y coverage is partial or missing (100% = omit)."""
    if ws.commits_count <= 0:
        return ""
    if ws.p10y_scored_commits is None:
        return ""
    if ws.p10y_scored_commits >= ws.commits_count:
        return ""
    pct = (ws.p10y_scored_commits / ws.commits_count) * 100.0
    missing_pct = 100.0 - pct
    if ws.p10y_scored_commits == 0:
        return (
            f"\n- **P10Y**: (P10Y missing scores) — none of {ws.commits_count} eligible commits "
            "had P10Y metrics.\n"
        )
    return (
        f"\n- **P10Y**: {ws.p10y_scored_commits} of {ws.commits_count} commits had P10Y scores "
        f"({pct:.1f}%); **{missing_pct:.1f}%** of commits were not processed by P10Y.\n"
    )


def format_skipped_workspaces_section(skipped: List[SkippedWorkspaceP10Y]) -> str:
    if not skipped:
        return ""
    lines = ["## Workspaces Without P10Y Estimates\n"]
    for sw in skipped:
        lines.append(f"- **{sw.workspace_name}**: {sw.reason}\n")
    lines.append("\n")
    return "".join(lines)


def _aggregate_p10y_executive_note(
    workspace_estimations: List[WorkspaceEstimation],
    aggregate_p10y_commit_coverage_pct: Optional[float],
) -> str:
    """
    Executive-summary wording for commit coverage (ops v0.4.0 #4).

    - 100%: say nothing
    - 0%: brief note only (no redundant “0%” spam)
    - partial: total hours + % of commits not processed
    """
    if not workspace_estimations:
        return ""
    cov = aggregate_p10y_commit_coverage_pct
    if cov is None:
        return ""
    if cov >= 99.95:
        return ""
    total_hours = sum(ws.total_hours for ws in workspace_estimations)
    if cov <= 0.05:
        return (
            f"\n**P10Y availability**: No eligible commits received P10Y scores in this run "
            f"(aggregate hour total from workspaces: **{total_hours:.1f}h**).\n\n"
        )
    missing = 100.0 - cov
    return (
        f"\n**P10Y availability**: Total effort across workspaces with estimates is "
        f"**{total_hours:.1f}h**; approximately **{missing:.1f}%** of eligible commits did not "
        f"receive P10Y scores (third-party service).\n\n"
    )


def format_workspace_breakdown(workspace_estimations: List[WorkspaceEstimation]) -> str:
    """
    Format per-workspace breakdown section.
    
    Args:
        workspace_estimations: List of workspace estimation results
    
    Returns:
        Markdown formatted workspace breakdown
    """
    sections = []
    
    for i, ws_est in enumerate(workspace_estimations, 1):
        p10y_line = _p10y_note_for_workspace(ws_est)
        section = f"""
### {i}. {ws_est.workspace_name}

**Total Estimated Hours**: {ws_est.total_hours:.1f}h  
**Total Effective Output Points**: {ws_est.total_effective_output:.1f}  
**Commits Analyzed**: {ws_est.commits_count}
{p10y_line}
#### Work Type Breakdown
- **New Work**: {ws_est.estimation_metrics.new_work:.1f}{_pct_of_total_suffix(ws_est.estimation_metrics.new_work, ws_est.total_effective_output)}
- **Refactor**: {ws_est.estimation_metrics.refactor:.1f}{_pct_of_total_suffix(ws_est.estimation_metrics.refactor, ws_est.total_effective_output)}
- **Rework**: {ws_est.estimation_metrics.rework:.1f}{_pct_of_total_suffix(ws_est.estimation_metrics.rework, ws_est.total_effective_output)}
- **Removed Work**: {ws_est.estimation_metrics.removed_work:.1f} 
- **Quality Score**: {ws_est.estimation_metrics.quality_score:.2f}/1.00

- **Total Output**: {ws_est.estimation_metrics.total_output:.1f} 
#### Component Complexity Metrics Breakdown ({len(ws_est.component_breakdown)} components)
"""
        
        # Sort components by hours (descending)
        sorted_components = sorted(
            ws_est.component_breakdown.items(),
            key=lambda x: x[1].hours,
            reverse=True
        )
        
        if sorted_components:
            for comp_name, comp_est in sorted_components:
                section += f"- **{comp_name}**: {comp_est.hours:.1f}h (Quality: {comp_est.quality_score:.2f})\n"
        else:
            section += "_No component breakdown available_\n"
        
        sections.append(section)
    
    return "\n".join(sections)


def format_high_variance_components(
    comparative_analysis: ComparativeAnalysis,
    component_comparison: Dict[str, ComponentComparison],
) -> str:
    """
    Format high variance components section.
    
    Args:
        comparative_analysis: Comparative analysis data
        component_comparison: Component comparison data
    
    Returns:
        Markdown formatted high variance section
    """
    if not comparative_analysis.high_variance_components:
        return "✅ **No high variance components detected** - All components show consistent estimates across workspaces.\n"
    
    section = f"⚠️ **{len(comparative_analysis.high_variance_components)} High Variance Component(s) Detected**\n\n"
    section += "These components show significant differences across workspaces (CV > 30%):\n\n"
    
    for comp_name in comparative_analysis.high_variance_components:
        comp_comp = component_comparison[comp_name]
        section += f"- **{comp_name}**\n"
        section += f"  - Average: {comp_comp.average:.1f}h ± {comp_comp.std_deviation:.1f}h\n"
        section += f"  - Variance: {comp_comp.variance_percentage:.1f}%\n"
        section += "  - Range: "
        
        hours_values = [h for h in comp_comp.hours_by_workspace.values() if h > 0]
        if hours_values:
            section += f"{min(hours_values):.1f}h - {max(hours_values):.1f}h\n"
        else:
            section += "N/A\n"
        section += "\n"
    
    return section


def format_multi_workspace_report(
    workspace_estimations: List[WorkspaceEstimation],
    summary: EstimationSummary,
    comparative_analysis: ComparativeAnalysis,
    skipped_workspaces: Optional[List[SkippedWorkspaceP10Y]] = None,
    aggregate_p10y_commit_coverage_pct: Optional[float] = None,
) -> str:
    """
    Generate a comprehensive markdown report for multi-workspace estimation.
    
    This is the main function that assembles all report sections into a complete document.
    
    Args:
        workspace_estimations: List of workspace estimation results
        summary: Statistical summary
        comparative_analysis: Comparative analysis
        skipped_workspaces: Workspaces that produced no estimate (best-effort P10Y)
        aggregate_p10y_commit_coverage_pct: Share of commits with P10Y scores (%)
    
    Returns:
        Complete markdown formatted report
    """
    from datetime import datetime, timezone

    skipped_workspaces = skipped_workspaces or []

    report_sections = []
    
    # Header
    report_sections.append(f"""# Multi-Workspace Estimation Report

**Generated**: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}  
**Workspaces Analyzed**: {len(workspace_estimations)}
**Workspaces Skipped (no P10Y estimate)**: {len(skipped_workspaces)}

---
""")
    
    # Executive Summary
    report_sections.append("""## Executive Summary

""")
    
    report_sections.append(
        _aggregate_p10y_executive_note(
            workspace_estimations, aggregate_p10y_commit_coverage_pct
        )
    )

    report_sections.append(f"""
**Average Estimated Hours**: {summary.average_hours:.1f}h ± {summary.std_deviation:.1f}h  
**Range**: {summary.min_hours:.1f}h - {summary.max_hours:.1f}h  
**Coefficient of Variation**: {summary.coefficient_of_variation*100:.1f}%  
**Variance Assessment**: {summary.variance_assessment.upper()}

""")
    
    # Risk Assessment Section
    if summary.risk_assessment:
        risk = summary.risk_assessment
        report_sections.append(f"""
### Risk Assessment

**Final Estimate**: {risk.final_estimate:.1f}h (with {risk.total_buffer_pct*100:.1f}% buffer)

#### Gatekeeping Metrics
- **Instability Ratio**: {risk.instability_ratio*100:.1f}%
- **Rejection Threshold**: {risk.rejection_threshold*100:.1f}%

#### Buffer Breakdown
- **Base Buffer**: {risk.base_component*100:.1f}%
- **Variance Penalty**: {risk.var_component*100:.1f}%
- **Size Penalty**: {risk.size_component*100:.1f}%
- **Total Buffer**: {risk.total_buffer_pct*100:.1f}%

""")
    
    # Variance Visualization
    report_sections.append(visualize_variance(summary))
    
    # Quick Stats Table
    report_sections.append("""
### Quick Statistics

| Metric | Value |
|--------|-------|
""")
    report_sections.append(f"| Average Hours | {summary.average_hours:.1f}h |\n")
    report_sections.append(f"| Minimum Hours | {summary.min_hours:.1f}h |\n")
    report_sections.append(f"| Maximum Hours | {summary.max_hours:.1f}h |\n")
    report_sections.append(f"| Standard Deviation | {summary.std_deviation:.1f}h |\n")
    report_sections.append(f"| Variance Assessment | {summary.variance_assessment.upper()} |\n")
    
    if summary.risk_assessment:
        report_sections.append(f"| Total Buffer | {summary.risk_assessment.total_buffer_pct*100:.1f}% |\n")
        report_sections.append(f"| Final Estimate | {summary.risk_assessment.final_estimate:.1f}h |\n")
    
    total_commits = sum(ws.commits_count for ws in workspace_estimations)
    report_sections.append(f"| Total Commits Analyzed | {total_commits} |\n")

    if workspace_estimations:
        avg_quality = sum(ws.estimation_metrics.quality_score for ws in workspace_estimations) / len(
            workspace_estimations
        )
        report_sections.append(f"| Average Quality Score | {avg_quality:.2f} |\n")
        if aggregate_p10y_commit_coverage_pct is not None:
            report_sections.append(
                f"| P10Y commit coverage (eligible → scored) | "
                f"{aggregate_p10y_commit_coverage_pct:.1f}% |\n"
            )
    
    report_sections.append("\n---\n")

    report_sections.append(format_skipped_workspaces_section(skipped_workspaces))

    # Per-Workspace Breakdown
    report_sections.append("## Per-Workspace Breakdown\n")
    report_sections.append(format_workspace_breakdown(workspace_estimations))
    report_sections.append("\n---\n")
    
    # Component Comparison
    report_sections.append("## Component Comparison\n\n")
    report_sections.append(create_comparison_table(
        workspace_estimations,
        comparative_analysis.component_comparison
    ))
    report_sections.append("\n")
    
    # High Variance Components
    report_sections.append("## High Variance Components\n\n")
    report_sections.append(format_high_variance_components(
        comparative_analysis,
        comparative_analysis.component_comparison
    ))
    report_sections.append("\n---\n")
    
    # Insights & Recommendations
    report_sections.append("## Key Insights\n\n")
    if comparative_analysis.insights:
        for insight in comparative_analysis.insights:
            report_sections.append(f"- {insight}\n")
    else:
        report_sections.append("_No specific insights generated_\n")
    
    report_sections.append("\n---\n")
    
    # Recommendations
    report_sections.append("""## Recommendations

### Budgeting Recommendation
""")
    
    if summary.variance_assessment == "low":
        report_sections.append(f"""
✅ **Use Average Estimate**: {summary.average_hours:.1f}h  
The low variance ({summary.coefficient_of_variation*100:.1f}%) indicates excellent consistency. 
You can confidently use the average estimate with a small contingency buffer (10-15%).
""")
    elif summary.variance_assessment == "medium":
        report_sections.append(f"""
⚠️ **Use Conservative Estimate**: {summary.max_hours:.1f}h  
The medium variance ({summary.coefficient_of_variation*100:.1f}%) suggests some implementation differences.
Budget for the higher estimate to account for potential variations. Consider adding 15-20% contingency.
""")
    else:  # high
        report_sections.append(f"""
🔴 **Review Specifications Before Budgeting**: Range {summary.min_hours:.1f}h - {summary.max_hours:.1f}h  
The high variance ({summary.coefficient_of_variation*100:.1f}%) indicates significant uncertainty.
Recommend spec review and clarification before finalizing budget. Consider budgeting for {summary.max_hours:.1f}h 
with 25-30% contingency, or investigate the root causes of variance first.
""")
    
    report_sections.append("""
### Next Steps

1. Review high-variance components (if any) to understand root causes
2. Consider which workspace's approach best matches your requirements
3. Factor in team experience and tooling when applying these estimates
4. Add appropriate contingency based on variance assessment

---

_This report is generated from actual code commits analyzed by multiple AI agents using P10Y metrics._
""")
    
    return "".join(report_sections)
