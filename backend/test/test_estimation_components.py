"""
Unit tests for per-phase breakdown and estimation functions.
"""
from app.services.p10y.p10y_lib import (
    Estimation,
    CodeGenerationMetadata,
    CommitInfo,
    generate_phase_breakdown,
    apply_productivity_multiplier,
)


class TestPhaseBreakdown:
    """Tests for generate_phase_breakdown function."""

    def test_commits_grouped_by_phase(self):
        """Commits are aggregated by their phase number."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="p06_add API", phase=6),
            CommitInfo(sha="commit2", message="p13_add UI", phase=13),
            CommitInfo(sha="commit3", message="p06_add tests", phase=6),
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

        breakdown = generate_phase_breakdown(metadata, commit_stats)

        assert 6 in breakdown
        assert 13 in breakdown
        assert breakdown[6].function_points == 15.0  # commit1 + commit3
        assert breakdown[13].function_points == 15.0  # commit2
        assert breakdown[6].new_work == 15.0
        assert breakdown[13].new_work == 15.0

    def test_unphased_commits_bucketed_under_none(self):
        """Commits with no phase prefix aggregate under the None (unphased) key."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="p03_add endpoint", phase=3),
            CommitInfo(sha="commit2", message="chore misc", phase=None),
        ])

        commit_stats = [
            {"sha": "commit1", "fp_delta_total": 20.0, "commit_quality_score": 0.8,
             "churn_rate": 0.1, "refactor": 2.0, "rework": 1.0, "new_work": 17.0,
             "removed_work": 0, "quality_score": 0.8, "effective_output": 18.0, "total_output": 20.0},
            {"sha": "commit2", "fp_delta_total": 25.0, "commit_quality_score": 0.85,
             "churn_rate": 0.12, "refactor": 3.0, "rework": 2.0, "new_work": 20.0,
             "removed_work": 0, "quality_score": 0.85, "effective_output": 22.0, "total_output": 25.0},
        ]

        breakdown = generate_phase_breakdown(metadata, commit_stats)

        assert 3 in breakdown
        assert None in breakdown
        assert breakdown[3].function_points == 20.0
        assert breakdown[None].function_points == 25.0

    def test_empty_commits(self):
        """Test breakdown with no commits."""
        metadata = CodeGenerationMetadata(commits=[])
        commit_stats = []

        breakdown = generate_phase_breakdown(metadata, commit_stats)

        assert len(breakdown) == 0

    def test_commit_not_in_stats(self):
        """Test that commits without stats are skipped."""
        metadata = CodeGenerationMetadata(commits=[
            CommitInfo(sha="commit1", message="p06_add API", phase=6),
            CommitInfo(sha="commit2", message="p13_add UI", phase=13),
        ])

        # Only provide stats for commit1
        commit_stats = [
            {"sha": "commit1", "fp_delta_total": 10.0, "commit_quality_score": 0.8,
             "churn_rate": 0.1, "refactor": 0, "rework": 0, "new_work": 10.0,
             "removed_work": 0, "quality_score": 0.8, "effective_output": 9.0, "total_output": 10.0},
        ]

        breakdown = generate_phase_breakdown(metadata, commit_stats)

        assert 6 in breakdown
        assert 13 not in breakdown
        assert breakdown[6].function_points == 10.0


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
