"""
Unit tests for component breakdown and estimation functions.
"""
from app.services.p10y.p10y_lib import (
    Estimation,
    CodeGenerationMetadata,
    CommitInfo,
    generate_component_breakdown,
    apply_productivity_multiplier,
    format_component_breakdown,
)


class TestComponentBreakdown:
    """Tests for generate_component_breakdown function."""
    
    def test_single_component_per_commit(self):
        """Test breakdown with one component per commit."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="backend: add API", component=["backend"]),
            CommitInfo(sha="commit2", message="frontend: add UI", component=["frontend"]),
            CommitInfo(sha="commit3", message="backend: add tests", component=["backend"]),
        ])
        
        commit_stats = [
            {"sha": "commit1", "fp_delta_total": 10.0, "commit_quality_score": 0.8, 
             "churn_rate": 0.1, "refactor": 0, "rework": 0, "new_work": 10.0,
             "removed_work": 0, "quality_score": 0.8, "effective_output": 9.0, "total_output": 10.0},
            {"sha": "commit2", "fp_delta_total": 15.0, "commit_quality_score": 0.9,
             "churn_rate": 0.05, "refactor": 0, "rework": 0, "new_work": 15.0,
             "removed_work": 0, "quality_score": 0.9, "effective_output": 14.0, "total_output": 15.0},
            {"sha": "commit3", "fp_delta_total": 5.0, "commit_quality_score": 0.85,
             "churn_rate": 0.08, "refactor": 0, "rework": 0, "new_work": 5.0,
             "removed_work": 0, "quality_score": 0.85, "effective_output": 4.5, "total_output": 5.0},
        ]
        
        breakdown = generate_component_breakdown(metadata, commit_stats)
        
        assert "backend" in breakdown
        assert "frontend" in breakdown
        assert breakdown["backend"].function_points == 15.0  # commit1 + commit3
        assert breakdown["frontend"].function_points == 15.0  # commit2
        assert breakdown["backend"].new_work == 15.0
        assert breakdown["frontend"].new_work == 15.0
    
    def test_multiple_components_per_commit(self):
        """Test breakdown when commits have multiple components."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="backend/api: add endpoint", component=["backend", "api"]),
            CommitInfo(sha="commit2", message="frontend/api: integrate", component=["frontend", "api"]),
        ])
        
        commit_stats = [
            {"sha": "commit1", "fp_delta_total": 20.0, "commit_quality_score": 0.8,
             "churn_rate": 0.1, "refactor": 2.0, "rework": 1.0, "new_work": 17.0,
             "removed_work": 0, "quality_score": 0.8, "effective_output": 18.0, "total_output": 20.0},
            {"sha": "commit2", "fp_delta_total": 25.0, "commit_quality_score": 0.85,
             "churn_rate": 0.12, "refactor": 3.0, "rework": 2.0, "new_work": 20.0,
             "removed_work": 0, "quality_score": 0.85, "effective_output": 22.0, "total_output": 25.0},
        ]
        
        breakdown = generate_component_breakdown(metadata, commit_stats)
        
        # Both commits should be counted for "api" component
        assert "api" in breakdown
        assert breakdown["api"].function_points == 45.0  # commit1 + commit2
        
        # Backend only gets commit1
        assert "backend" in breakdown
        assert breakdown["backend"].function_points == 20.0
        
        # Frontend only gets commit2
        assert "frontend" in breakdown
        assert breakdown["frontend"].function_points == 25.0
    
    def test_empty_commits(self):
        """Test breakdown with no commits."""
        metadata = CodeGenerationMetadata(commits=[])
        commit_stats = []
        
        breakdown = generate_component_breakdown(metadata, commit_stats)
        
        assert len(breakdown) == 0
    
    def test_commit_not_in_stats(self):
        """Test that commits without stats are skipped."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="backend: add API", component=["backend"]),
            CommitInfo(sha="commit2", message="frontend: add UI", component=["frontend"]),
        ])
        
        # Only provide stats for commit1
        commit_stats = [
            {"sha": "commit1", "fp_delta_total": 10.0, "commit_quality_score": 0.8,
             "churn_rate": 0.1, "refactor": 0, "rework": 0, "new_work": 10.0,
             "removed_work": 0, "quality_score": 0.8, "effective_output": 9.0, "total_output": 10.0},
        ]
        
        breakdown = generate_component_breakdown(metadata, commit_stats)
        
        assert "backend" in breakdown
        assert "frontend" not in breakdown
        assert breakdown["backend"].function_points == 10.0


class TestProductivityMultiplier:
    """Tests for apply_productivity_multiplier function."""
    
    def test_default_multiplier(self):
        """Test with default 2.0x multiplier."""
        estimation = Estimation(
            function_points=100.0,
            commit_quality_score=0.8,
            churn_rate=0.1,
            technologies=[],
            id_contributor=1,
            refactor=10.0,
            rework=5.0,
            new_work=85.0,
            removed_work=0,
            quality_score=0.8,
            effective_output=95.0,
            total_output=100.0,
        )
        
        hours = apply_productivity_multiplier(estimation)
        
        assert hours == 200.0  # 100 FP * 2.0
    
    def test_custom_multiplier(self):
        """Test with custom multiplier."""
        estimation = Estimation(
            function_points=50.0,
            commit_quality_score=0.9,
            churn_rate=0.05,
            technologies=[],
            id_contributor=1,
            refactor=5.0,
            rework=2.0,
            new_work=43.0,
            removed_work=0,
            quality_score=0.9,
            effective_output=48.0,
            total_output=50.0,
        )
        
        hours = apply_productivity_multiplier(estimation, multiplier=1.5)
        
        assert hours == 75.0  # 50 FP * 1.5
    
    def test_zero_function_points(self):
        """Test with zero function points."""
        estimation = Estimation(
            function_points=0.0,
            commit_quality_score=0,
            churn_rate=0,
            technologies=[],
            id_contributor=0,
            refactor=0,
            rework=0,
            new_work=0,
            removed_work=0,
            quality_score=0,
            effective_output=0,
            total_output=0,
        )
        
        hours = apply_productivity_multiplier(estimation)
        
        assert hours == 0.0


class TestFormatComponentBreakdown:
    """Tests for format_component_breakdown function."""
    
    def test_format_single_component(self):
        """Test formatting with single component."""
        component_breakdown = {
            "backend": Estimation(
                function_points=100.0,
                commit_quality_score=0.8,
                churn_rate=0.1,
                technologies=[],
                id_contributor=1,
                refactor=10.0,
                rework=5.0,
                new_work=85.0,
                removed_work=0,
                quality_score=0.85,
                effective_output=95.0,
                total_output=100.0,
            )
        }
        
        formatted = format_component_breakdown(component_breakdown)
        
        assert "## Component Breakdown" in formatted
        assert "### Backend" in formatted
        assert "Estimated Hours: 200.0" in formatted  # 100 * 2.0
        assert "Quality Score: 0.85" in formatted
    
    def test_format_multiple_components(self):
        """Test formatting with multiple components."""
        component_breakdown = {
            "backend": Estimation(
                function_points=50.0,
                commit_quality_score=0.8,
                churn_rate=0.1,
                technologies=[],
                id_contributor=1,
                refactor=5.0,
                rework=2.0,
                new_work=43.0,
                removed_work=0,
                quality_score=0.8,
                effective_output=48.0,
                total_output=50.0,
            ),
            "frontend": Estimation(
                function_points=60.0,
                commit_quality_score=0.9,
                churn_rate=0.05,
                technologies=[],
                id_contributor=1,
                refactor=3.0,
                rework=1.0,
                new_work=56.0,
                removed_work=0,
                quality_score=0.9,
                effective_output=59.0,
                total_output=60.0,
            ),
        }
        
        formatted = format_component_breakdown(component_breakdown)
        
        assert "### Backend" in formatted
        assert "### Frontend" in formatted
        assert "Estimated Hours: 100.0" in formatted  # 50 * 2.0
        assert "Estimated Hours: 120.0" in formatted  # 60 * 2.0
    
    def test_format_empty_breakdown(self):
        """Test formatting with no components."""
        component_breakdown = {}
        
        formatted = format_component_breakdown(component_breakdown)
        
        assert "## Component Breakdown" in formatted
        # Should only have the header, no component sections
        assert "###" not in formatted
    
    def test_format_custom_multiplier(self):
        """Test formatting with custom multiplier."""
        component_breakdown = {
            "backend": Estimation(
                function_points=100.0,
                commit_quality_score=0.8,
                churn_rate=0.1,
                technologies=[],
                id_contributor=1,
                refactor=10.0,
                rework=5.0,
                new_work=85.0,
                removed_work=0,
                quality_score=0.8,
                effective_output=95.0,
                total_output=100.0,
            )
        }
        
        formatted = format_component_breakdown(component_breakdown, multiplier=1.5)
        
        assert "Estimated Hours: 150.0" in formatted  # 100 * 1.5

