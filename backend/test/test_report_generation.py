"""
Tests for generation report generation.
"""

import pytest

from app.schemas.estimate import (
    ComparativeAnalysis,
    ComponentComparison,
    ComponentEstimation,
    EstimationMetrics,
    EstimationSummary,
    SkippedWorkspaceP10Y,
    WorkspaceEstimation,
)
from app.services.p10y.estimation_report_generator import (
    create_comparison_table,
    format_high_variance_components,
    format_multi_workspace_report,
    format_workspace_breakdown,
    visualize_variance,
)


@pytest.fixture
def sample_workspace_estimations():
    """Create sample workspace estimations for testing."""
    return [
        WorkspaceEstimation(
            workspace_name="workspace-1",
            workspace_path="/workspace1",
            total_hours=150.0,
            total_effective_output=75.0,
            commits_count=15,
            component_breakdown={
                "backend": ComponentEstimation(
                    component_name="backend",
                    hours=80.0,
                    new_work=60.0,
                    refactor=15.0,
                    rework=5.0,
                    quality_score=0.85
                ),
                "frontend": ComponentEstimation(
                    component_name="frontend",
                    hours=70.0,
                    new_work=55.0,
                    refactor=12.0,
                    rework=3.0,
                    quality_score=0.90
                ),
            },
            estimation_metrics=EstimationMetrics(
                new_work=60.0,
                refactor=12.0,
                rework=3.0,
                removed_work=0.0,
                quality_score=0.87,
                effective_output=60.0,
                total_output=75.0
            )
        ),
        WorkspaceEstimation(
            workspace_name="workspace-2",
            workspace_path="/workspace2",
            total_hours=180.0,
            total_effective_output=90.0,
            commits_count=18,
            component_breakdown={
                "backend": ComponentEstimation(
                    component_name="backend",
                    hours=100.0,
                    new_work=75.0,
                    refactor=20.0,
                    rework=5.0,
                    quality_score=0.80
                ),
                "frontend": ComponentEstimation(
                    component_name="frontend",
                    hours=80.0,
                    new_work=60.0,
                    refactor=15.0,
                    rework=5.0,
                    quality_score=0.85
                ),
            },
            estimation_metrics=EstimationMetrics(
                new_work=72.0,
                refactor=15.0,
                rework=3.0,
                removed_work=0.0,
                quality_score=0.82,
                effective_output=72.0,
                total_output=90.0
            )
        ),
        WorkspaceEstimation(
            workspace_name="workspace-3",
            workspace_path="/workspace3",
            total_hours=165.0,
            total_effective_output=82.5,
            commits_count=16,
            component_breakdown={
                "backend": ComponentEstimation(
                    component_name="backend",
                    hours=90.0,
                    new_work=68.0,
                    refactor=18.0,
                    rework=4.0,
                    quality_score=0.83
                ),
                "frontend": ComponentEstimation(
                    component_name="frontend",
                    hours=75.0,
                    new_work=58.0,
                    refactor=14.0,
                    rework=3.0,
                    quality_score=0.88
                ),
            },
            estimation_metrics=EstimationMetrics(
                new_work=66.0,
                refactor=13.5,
                rework=3.0,
                removed_work=0.0,
                quality_score=0.85,
                effective_output=66.0,
                total_output=82.5
            )
        ),
    ]


@pytest.fixture
def sample_summary():
    """Create sample estimation summary."""
    return EstimationSummary(
        average_hours=165.0,
        std_deviation=15.0,
        min_hours=150.0,
        max_hours=180.0,
        coefficient_of_variation=0.091,  # ~9.1%
        variance_assessment="low"
    )


@pytest.fixture
def sample_component_comparison():
    """Create sample component comparison."""
    return {
        "backend": ComponentComparison(
            component_name="backend",
            hours_by_workspace={
                "workspace-1": 80.0,
                "workspace-2": 100.0,
                "workspace-3": 90.0,
            },
            average=90.0,
            std_deviation=10.0,
            variance_percentage=11.1
        ),
        "frontend": ComponentComparison(
            component_name="frontend",
            hours_by_workspace={
                "workspace-1": 70.0,
                "workspace-2": 80.0,
                "workspace-3": 75.0,
            },
            average=75.0,
            std_deviation=5.0,
            variance_percentage=6.7
        ),
    }


@pytest.fixture
def sample_comparative_analysis(sample_component_comparison):
    """Create sample comparative analysis."""
    return ComparativeAnalysis(
        component_comparison=sample_component_comparison,
        high_variance_components=[],
        insights=[
            "All workspaces show consistent estimates (CV < 15%)",
            "Quality scores are uniformly high across workspaces (0.82-0.87)",
            "Backend component accounts for ~54% of total effort",
        ]
    )


class TestCreateComparisonTable:
    """Tests for create_comparison_table function."""
    
    def test_creates_valid_markdown_table(
        self, sample_workspace_estimations, sample_component_comparison
    ):
        """Test that a valid markdown table is created."""
        table = create_comparison_table(
            sample_workspace_estimations,
            sample_component_comparison
        )
        
        # Check table structure
        assert "| Component |" in table
        assert "workspace-1" in table
        assert "workspace-2" in table
        assert "workspace-3" in table
        assert "Average" in table
        assert "Std Dev" in table
        assert "Variance %" in table
        
        # Check component data
        assert "backend" in table
        assert "frontend" in table
        assert "80.0h" in table  # workspace-1 backend
        assert "100.0h" in table  # workspace-2 backend
    
    def test_handles_empty_comparison(self, sample_workspace_estimations):
        """Test handling of empty component comparison."""
        table = create_comparison_table(sample_workspace_estimations, {})
        assert "No components to compare" in table
    
    def test_handles_missing_components(self, sample_workspace_estimations):
        """Test handling when some workspaces don't have a component."""
        component_comparison = {
            "backend": ComponentComparison(
                component_name="backend",
                hours_by_workspace={
                    "workspace-1": 80.0,
                    # workspace-2 missing backend
                    "workspace-3": 90.0,
                },
                average=85.0,
                std_deviation=7.1,
                variance_percentage=8.3
            ),
        }
        
        table = create_comparison_table(
            sample_workspace_estimations,
            component_comparison
        )
        
        # Should show "-" for missing component
        assert "-" in table


class TestVisualizeVariance:
    """Tests for visualize_variance function."""
    
    def test_low_variance_visualization(self):
        """Test visualization for low variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=10.0,
            min_hours=155.0,
            max_hours=175.0,
            coefficient_of_variation=0.06,  # 6%
            variance_assessment="low"
        )
        
        viz = visualize_variance(summary)
        
        assert "Coefficient of Variation: 6.0%" in viz
        assert "🟢" in viz
        assert "low" in viz.lower()
        assert "Excellent consistency" in viz
        assert "█" in viz  # Bar chart
        assert "░" in viz  # Bar chart
    
    def test_medium_variance_visualization(self):
        """Test visualization for medium variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=35.0,
            min_hours=130.0,
            max_hours=200.0,
            coefficient_of_variation=0.21,  # 21%
            variance_assessment="medium"
        )
        
        viz = visualize_variance(summary)
        
        assert "21.0%" in viz
        assert "🟡" in viz
        assert "medium" in viz.lower()
        assert "Acceptable variance" in viz
    
    def test_high_variance_visualization(self):
        """Test visualization for high variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=60.0,
            min_hours=100.0,
            max_hours=230.0,
            coefficient_of_variation=0.36,  # 36%
            variance_assessment="high"
        )
        
        viz = visualize_variance(summary)
        
        assert "36.0%" in viz
        assert "🔴" in viz
        assert "high" in viz.lower()
        assert "High variance" in viz


class TestFormatWorkspaceBreakdown:
    """Tests for format_workspace_breakdown function."""
    
    def test_formats_all_workspaces(self, sample_workspace_estimations):
        """Test that all workspaces are included in breakdown."""
        breakdown = format_workspace_breakdown(sample_workspace_estimations)
        
        assert "workspace-1" in breakdown
        assert "workspace-2" in breakdown
        assert "workspace-3" in breakdown
    
    def test_includes_key_metrics(self, sample_workspace_estimations):
        """Test that key metrics are included."""
        breakdown = format_workspace_breakdown(sample_workspace_estimations)
        
        assert "Total Estimated Hours" in breakdown
        assert "Total Effective Output Points" in breakdown
        assert "Commits Analyzed" in breakdown
        assert "Work Type Breakdown" in breakdown
        assert "Component Complexity Metrics Breakdown" in breakdown
        assert "Quality Score" in breakdown
    
    def test_component_sorting(self, sample_workspace_estimations):
        """Test that components are sorted by hours (descending)."""
        breakdown = format_workspace_breakdown(sample_workspace_estimations)
        
        # For workspace-1, backend (80h) should appear before frontend (70h)
        backend_pos = breakdown.find("**backend**")
        frontend_pos = breakdown.find("**frontend**")
        assert backend_pos < frontend_pos

    def test_zero_total_effective_output_omits_share_percentages(self):
        """Work type % of total is omitted when total_effective_output is zero (no division by zero)."""
        ws = WorkspaceEstimation(
            workspace_name="edge-zero-output",
            workspace_path="/x",
            total_hours=0.0,
            total_effective_output=0.0,
            commits_count=0,
            component_breakdown={},
            estimation_metrics=EstimationMetrics(
                new_work=0.0,
                refactor=0.0,
                rework=0.0,
                removed_work=0.0,
                quality_score=0.0,
                effective_output=0.0,
                total_output=0.0,
            ),
        )
        breakdown = format_workspace_breakdown([ws])
        assert "**Total Effective Output Points**: 0.0" in breakdown
        assert "- **New Work**: 0.0\n" in breakdown
        assert "- **Refactor**: 0.0\n" in breakdown
        assert "- **Rework**: 0.0\n" in breakdown


class TestFormatHighVarianceComponents:
    """Tests for format_high_variance_components function."""
    
    def test_no_high_variance_components(self, sample_comparative_analysis, sample_component_comparison):
        """Test when there are no high variance components."""
        result = format_high_variance_components(
            sample_comparative_analysis,
            sample_component_comparison
        )
        
        assert "No high variance components detected" in result
        assert "✅" in result
    
    def test_with_high_variance_components(self, sample_component_comparison):
        """Test when there are high variance components."""
        analysis = ComparativeAnalysis(
            component_comparison=sample_component_comparison,
            high_variance_components=["backend"],
            insights=[]
        )
        
        result = format_high_variance_components(
            analysis,
            sample_component_comparison
        )
        
        assert "High Variance Component(s) Detected" in result
        assert "⚠️" in result
        assert "backend" in result
        assert "Average:" in result
        assert "Variance:" in result


class TestFormatMultiWorkspaceReport:
    """Tests for format_multi_workspace_report function."""
    
    def test_generates_complete_report(
        self,
        sample_workspace_estimations,
        sample_summary,
        sample_comparative_analysis
    ):
        """Test that a complete report is generated."""
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            sample_summary,
            sample_comparative_analysis
        )
        
        # Check main sections are present
        assert "# Multi-Workspace Estimation Report" in report
        assert "## Executive Summary" in report
        assert "## Per-Workspace Breakdown" in report
        assert "## Component Comparison" in report
        assert "## High Variance Components" in report
        assert "## Key Insights" in report
        assert "## Recommendations" in report

    def test_skipped_workspaces_and_p10y_coverage_sections(
        self,
        sample_workspace_estimations,
        sample_summary,
        sample_comparative_analysis,
    ):
        """Verify skipped workspaces and aggregate P10Y coverage metrics appear in report sections."""
        skipped = [
            SkippedWorkspaceP10Y(workspace_name="ws-miss", reason="P10Y repository not configured"),
        ]
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            sample_summary,
            sample_comparative_analysis,
            skipped_workspaces=skipped,
            aggregate_p10y_commit_coverage_pct=82.5,
        )
        assert "Workspaces Skipped (no P10Y estimate)" in report
        assert "## Workspaces Without P10Y Estimates" in report
        assert "ws-miss" in report
        assert "P10Y availability" in report
        assert "82.5" in report
    
    def test_includes_all_workspaces(
        self,
        sample_workspace_estimations,
        sample_summary,
        sample_comparative_analysis
    ):
        """Test that all workspaces are included."""
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            sample_summary,
            sample_comparative_analysis
        )
        
        assert "workspace-1" in report
        assert "workspace-2" in report
        assert "workspace-3" in report
    
    def test_includes_variance_visualization(
        self,
        sample_workspace_estimations,
        sample_summary,
        sample_comparative_analysis
    ):
        """Test that variance visualization is included."""
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            sample_summary,
            sample_comparative_analysis
        )
        
        assert "Variance Visualization" in report
        assert "█" in report  # Bar chart
        assert "🟢" in report  # Low variance indicator
    
    def test_includes_insights(
        self,
        sample_workspace_estimations,
        sample_summary,
        sample_comparative_analysis
    ):
        """Test that insights are included."""
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            sample_summary,
            sample_comparative_analysis
        )
        
        for insight in sample_comparative_analysis.insights:
            assert insight in report
    
    def test_low_variance_recommendation(
        self,
        sample_workspace_estimations,
        sample_comparative_analysis
    ):
        """Test recommendation for low variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=10.0,
            min_hours=155.0,
            max_hours=175.0,
            coefficient_of_variation=0.06,
            variance_assessment="low"
        )
        
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            summary,
            sample_comparative_analysis
        )
        
        assert "Use Average Estimate" in report
        assert "10-15%" in report  # Contingency buffer
    
    def test_medium_variance_recommendation(
        self,
        sample_workspace_estimations,
        sample_comparative_analysis
    ):
        """Test recommendation for medium variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=35.0,
            min_hours=130.0,
            max_hours=200.0,
            coefficient_of_variation=0.21,
            variance_assessment="medium"
        )
        
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            summary,
            sample_comparative_analysis
        )
        
        assert "Use Conservative Estimate" in report
        assert "15-20%" in report  # Contingency buffer
    
    def test_high_variance_recommendation(
        self,
        sample_workspace_estimations,
        sample_comparative_analysis
    ):
        """Test recommendation for high variance."""
        summary = EstimationSummary(
            average_hours=165.0,
            std_deviation=60.0,
            min_hours=100.0,
            max_hours=230.0,
            coefficient_of_variation=0.36,
            variance_assessment="high"
        )
        
        report = format_multi_workspace_report(
            sample_workspace_estimations,
            summary,
            sample_comparative_analysis
        )
        
        assert "Review Specifications Before Budgeting" in report
        assert "25-30%" in report  # Contingency buffer


# ---------------------------------------------------------------------------
# Phase 4: _write_structured_report — report write failure must not raise
# ---------------------------------------------------------------------------

class TestWriteStructuredReport:
    """
    Phase 4: The structured report is observability output, not a safety invariant.
    A filesystem error (ENOSPC, permissions, etc.) must log a warning and continue
    rather than propagating and killing an generation after all work is done.
    """

    def test_write_success(self, tmp_path):
        """Happy path: file is written when directory exists."""
        import logging
        from app.workflows.multi_workspace_estimation_p10y import _write_structured_report

        report_path = str(tmp_path / "outputs" / "report.md")
        _write_structured_report("# Report\ncontent", report_path, logging.getLogger("test"))

        assert (tmp_path / "outputs" / "report.md").read_text() == "# Report\ncontent"

    def test_makedirs_failure_does_not_raise(self, caplog):
        """os.makedirs raising OSError must be caught; function must not propagate."""
        import logging
        from unittest.mock import patch
        from app.workflows.multi_workspace_estimation_p10y import _write_structured_report

        with caplog.at_level(logging.WARNING):
            with patch("os.makedirs", side_effect=OSError("ENOSPC: no space left")):
                _write_structured_report(
                    "# Report", "/nonexistent/path/report.md", logging.getLogger("test")
                )

        assert any(
            r.levelno >= logging.WARNING for r in caplog.records
        ), "Expected WARNING log on makedirs failure"

    def test_open_failure_does_not_raise(self, tmp_path, caplog):
        """open() raising OSError must be caught; function must not propagate."""
        import logging
        from unittest.mock import patch
        from app.workflows.multi_workspace_estimation_p10y import _write_structured_report

        report_path = str(tmp_path / "report.md")
        with caplog.at_level(logging.WARNING):
            with patch("builtins.open", side_effect=OSError("permission denied")):
                _write_structured_report("# Report", report_path, logging.getLogger("test"))

        assert any(
            r.levelno >= logging.WARNING for r in caplog.records
        ), "Expected WARNING log on open() failure"


# ---------------------------------------------------------------------------
# Phase 5: the workflow saves the HTML report next to the markdown report,
# regardless of whether any email/Slack notifier is configured (local
# quickstart users have neither).
# ---------------------------------------------------------------------------


class TestWorkflowSavesHtmlReportRegardlessOfNotifierConfig:
    @pytest.mark.asyncio
    async def test_html_report_written_next_to_markdown_report(self, tmp_path):
        import logging
        from unittest.mock import AsyncMock, patch

        from app.core.config import settings as global_settings
        from app.schemas.model_token_usage import ModelTokenUsage
        from app.schemas.specification import GenerationWorkflowRequest
        from app.services.parallel_executor import ParallelGenerationResult
        from app.schemas.workspace import WorkspaceSettings
        from app.workflows.multi_workspace_estimation_p10y import (
            multi_workspace_estimation_p10y_workflow,
        )

        workspace_estimation = WorkspaceEstimation(
            workspace_name="ws-01-1",
            workspace_path=str(tmp_path / "ws-01-1"),
            total_hours=100.0,
            total_effective_output=90.0,
            component_breakdown={
                "auth": ComponentEstimation(
                    component_name="auth",
                    hours=40.0,
                    new_work=30.0,
                    refactor=8.0,
                    rework=2.0,
                    quality_score=0.9,
                ),
            },
            estimation_metrics=EstimationMetrics(
                new_work=30.0,
                refactor=8.0,
                rework=2.0,
                removed_work=0.0,
                quality_score=0.9,
                effective_output=90.0,
                total_output=100.0,
            ),
            commits_count=5,
            model_usage=ModelTokenUsage(model_name="anthropic/claude-sonnet-4.5", num_turns=3),
        )
        parallel_result = ParallelGenerationResult(
            workspace_name="ws-01-1",
            workspace_settings=WorkspaceSettings(
                workspace_path=str(tmp_path / "ws-01-1"),
                provider="anthropic",
                model="claude-sonnet-4.5",
            ),
            estimation=workspace_estimation,
            success=True,
        )

        request = GenerationWorkflowRequest(
            spec_path="specs/test.md",
            outputs_dir="outputs",
            generation_id=None,  # skips Firestore-backed usage/archive branches
        )
        test_settings = global_settings.model_copy(
            update={"WORKSPACE_BASE_PATH": str(tmp_path)}
        )

        with (
            patch(
                "app.workflows.multi_workspace_estimation_p10y.execute_generation_parallel",
                new=AsyncMock(return_value=[parallel_result]),
            ),
            patch(
                "app.workflows.multi_workspace_estimation_p10y._generate_ai_report",
                new=AsyncMock(return_value="skipped in test"),
            ),
        ):
            response = await multi_workspace_estimation_p10y_workflow(
                request=request,
                settings=test_settings,
                logger=logging.getLogger("test"),
                workspace_ids=["ws-01-1"],
                db_adapter=None,
            )

        outputs_dir = tmp_path / "ws-01-1" / "outputs"
        md_report = outputs_dir / "multi-workspace-estimation-report.md"
        html_report = outputs_dir / "multi-workspace-estimation-report.html"

        assert md_report.exists()
        assert html_report.exists()
        html_content = html_report.read_text()
        assert "<html" in html_content.lower()
        assert "auth" in html_content
        assert response.workspace_estimations == [workspace_estimation]

    @pytest.mark.asyncio
    async def test_html_report_includes_variants_with_real_db_adapter(self, tmp_path):
        """Regression: the workflow must pass the *sync* IDatabase to the renderer.

        The renderer resolves repo/branch links via a synchronous
        ``db.get("workspaces", id)``. If the async ``StateMachineDBAdapter`` is
        passed instead of its underlying sync db, every lookup raises
        ``AttributeError`` (swallowed by the renderer's per-workspace guard) and
        the whole Variants section silently vanishes from the HTML report.
        """
        import logging
        from unittest.mock import AsyncMock, patch

        from app.core.config import settings as global_settings
        from app.database.memory import InMemoryDatabase
        from app.schemas.model_token_usage import ModelTokenUsage
        from app.schemas.specification import GenerationWorkflowRequest
        from app.services.parallel_executor import ParallelGenerationResult
        from app.schemas.workspace import WorkspaceSettings
        from app.state.db_adapter import COL_WORKSPACES, StateMachineDBAdapter
        from app.workflows.multi_workspace_estimation_p10y import (
            multi_workspace_estimation_p10y_workflow,
        )

        workspace_estimation = WorkspaceEstimation(
            workspace_name="ws-01-1",
            workspace_path=str(tmp_path / "ws-01-1"),
            total_hours=100.0,
            total_effective_output=90.0,
            component_breakdown={
                "auth": ComponentEstimation(
                    component_name="auth",
                    hours=40.0,
                    new_work=30.0,
                    refactor=8.0,
                    rework=2.0,
                    quality_score=0.9,
                ),
            },
            estimation_metrics=EstimationMetrics(
                new_work=30.0,
                refactor=8.0,
                rework=2.0,
                removed_work=0.0,
                quality_score=0.9,
                effective_output=90.0,
                total_output=100.0,
            ),
            commits_count=5,
            model_usage=ModelTokenUsage(model_name="anthropic/claude-sonnet-4.5", num_turns=3),
        )
        parallel_result = ParallelGenerationResult(
            workspace_name="ws-01-1",
            workspace_settings=WorkspaceSettings(
                workspace_path=str(tmp_path / "ws-01-1"),
                provider="anthropic",
                model="claude-sonnet-4.5",
            ),
            estimation=workspace_estimation,
            success=True,
        )

        db = InMemoryDatabase()
        db.set(COL_WORKSPACES, "ws-01-1", {"repo_url": "https://github.com/acme/repo.git"})
        db_adapter = StateMachineDBAdapter(db)

        request = GenerationWorkflowRequest(
            spec_path="specs/test.md",
            outputs_dir="outputs",
            generation_id=None,  # skips Firestore-backed usage/archive branches
        )
        test_settings = global_settings.model_copy(
            update={"WORKSPACE_BASE_PATH": str(tmp_path)}
        )

        with (
            patch(
                "app.workflows.multi_workspace_estimation_p10y.execute_generation_parallel",
                new=AsyncMock(return_value=[parallel_result]),
            ),
            patch(
                "app.workflows.multi_workspace_estimation_p10y._generate_ai_report",
                new=AsyncMock(return_value="skipped in test"),
            ),
        ):
            await multi_workspace_estimation_p10y_workflow(
                request=request,
                settings=test_settings,
                logger=logging.getLogger("test"),
                workspace_ids=["ws-01-1"],
                db_adapter=db_adapter,
            )

        html_content = (
            tmp_path / "ws-01-1" / "outputs" / "multi-workspace-estimation-report.html"
        ).read_text()
        # The Variants section only renders when the repo lookup succeeds — i.e.
        # when the sync db was passed through correctly.
        assert "Variants" in html_content
        assert "acme/repo" in html_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
