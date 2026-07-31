"""
Tests for p10y_lib per-phase breakdown, productivity multiplier, and technology filtering.
"""
import logging
from app.services.p10y.p10y_lib import (
    CommitInfo,
    CodeGenerationMetadata,
    Estimation,
    VALID_TECHNOLOGIES,
    apply_productivity_multiplier,
    generate_phase_breakdown,
    has_valid_technology,
)


def test_generate_phase_breakdown():
    """Test per-phase breakdown generation."""
    metadata = CodeGenerationMetadata(commits=[
        CommitInfo(sha="abc123", message="p06_add service", phase=6),
        CommitInfo(sha="def456", message="p13_add UI", phase=13),
        CommitInfo(sha="ghi789", message="p06_add endpoint", phase=6),
    ])

    commit_stats_data = [
        {
            "sha": "abc123",
            "fp_delta_total": 10.0,
            "commit_quality_score": 0.8,
            "churn_rate": 0.1,
            "refactor": 2.0,
            "rework": 1.0,
            "new_work": 7.0,
            "removed_work": 0.5,
            "quality_score": 0.85,
            "effective_output": 9.0,
            "total_output": 10.0,
        },
        {
            "sha": "def456",
            "fp_delta_total": 8.0,
            "commit_quality_score": 0.75,
            "churn_rate": 0.15,
            "refactor": 1.5,
            "rework": 1.5,
            "new_work": 5.0,
            "removed_work": 0.3,
            "quality_score": 0.80,
            "effective_output": 7.0,
            "total_output": 8.0,
        },
        {
            "sha": "ghi789",
            "fp_delta_total": 12.0,
            "commit_quality_score": 0.9,
            "churn_rate": 0.05,
            "refactor": 3.0,
            "rework": 2.0,
            "new_work": 7.0,
            "removed_work": 0.2,
            "quality_score": 0.90,
            "effective_output": 11.0,
            "total_output": 12.0,
        },
    ]

    breakdown = generate_phase_breakdown(metadata, commit_stats_data)

    assert 6 in breakdown
    assert 13 in breakdown

    # Phase 6 should include commits abc123 and ghi789
    assert breakdown[6].function_points == 22.0  # 10 + 12
    assert breakdown[6].new_work == 14.0  # 7 + 7

    # Phase 13 should include commit def456
    assert breakdown[13].function_points == 8.0
    assert breakdown[13].new_work == 5.0


def test_apply_productivity_multiplier():
    """Test productivity multiplier application."""
    generation = Estimation(
        function_points=50.0,
        commit_quality_score=0.8,
        churn_rate=0.1,
        technologies=[],
        id_contributor=1,
        refactor=10.0,
        rework=5.0,
        new_work=35.0,
        removed_work=2.0,
        quality_score=0.85,
        effective_output=45.0,
        total_output=50.0,
    )

    # Test with default multiplier (2.0)
    hours = apply_productivity_multiplier(generation)
    assert hours == 100.0

    # Test with custom multiplier
    hours = apply_productivity_multiplier(generation, multiplier=3.0)
    assert hours == 150.0


def test_generate_phase_breakdown_with_missing_commits():
    """Test per-phase breakdown when some commits are not in stats."""
    metadata = CodeGenerationMetadata(commits=[
        CommitInfo(sha="abc123", message="p06_add service", phase=6),
        CommitInfo(sha="missing", message="p13_add UI", phase=13),
    ])

    commit_stats_data = [
        {
            "sha": "abc123",
            "fp_delta_total": 10.0,
            "commit_quality_score": 0.8,
            "churn_rate": 0.1,
            "refactor": 2.0,
            "rework": 1.0,
            "new_work": 7.0,
            "removed_work": 0.5,
            "quality_score": 0.85,
            "effective_output": 9.0,
            "total_output": 10.0,
        },
    ]

    breakdown = generate_phase_breakdown(metadata, commit_stats_data)

    # Only phase 6 should be in breakdown
    assert 6 in breakdown
    assert 13 not in breakdown
    assert breakdown[6].effective_output == 9.0


def test_generate_phase_breakdown_empty():
    """Test per-phase breakdown with no commits."""
    metadata = CodeGenerationMetadata(commits=[])
    commit_stats_data = []

    breakdown = generate_phase_breakdown(metadata, commit_stats_data)

    assert breakdown == {}


def test_valid_technologies_predefined_list():
    """Test that VALID_TECHNOLOGIES contains expected languages."""
    expected_technologies = {
        "abap", "c", "c#", "c++", "css", "go", "html", "java", "js", "kotlin",
        "less", "lua", "objective-c", "php", "python", "ruby", "rust", "sass",
        "scala", "shell", "solidity", "sql", "swift", "typescript", "vue", "hcl"
    }
    assert VALID_TECHNOLOGIES == expected_technologies


def test_has_valid_technology_with_valid_tech(caplog):
    """Test has_valid_technology returns True for commits with valid technologies."""
    logger = logging.getLogger("test")

    commit_stats = {
        "sha": "abc123",
        "technologies": {"supported": ["Python", "JavaScript", "CSS"]}
    }

    assert has_valid_technology(commit_stats, logger) is True
    # Should not log warning
    assert "Skipping commit" not in caplog.text


def test_has_valid_technology_case_insensitive(caplog):
    """Test has_valid_technology handles case-insensitive comparison."""
    logger = logging.getLogger("test")

    # Mix of uppercase and lowercase
    commit_stats = {
        "sha": "abc123",
        "technologies": {"supported": ["PYTHON", "javascript", "TypeScript"]}
    }

    assert has_valid_technology(commit_stats, logger) is True


def test_has_valid_technology_with_invalid_tech(caplog):
    """Test has_valid_technology returns False for commits with no valid technologies."""
    logger = logging.getLogger("test")

    commit_stats = {
        "sha": "abc123def456",
        "technologies": {"supported": ["Markdown", "JSON", "YAML"]}
    }

    with caplog.at_level(logging.WARNING):
        assert has_valid_technology(commit_stats, logger) is False
        # Should log warning
        assert "Skipping commit abc123def456 from final calculation" in caplog.text
        assert "no valid technologies found" in caplog.text
        assert "['Markdown', 'JSON', 'YAML']" in caplog.text


def test_has_valid_technology_with_empty_list(caplog):
    """Test has_valid_technology returns False for commits with empty technologies list."""
    logger = logging.getLogger("test")

    commit_stats = {
        "sha": "abc123",
        "technologies": {}
    }

    assert has_valid_technology(commit_stats, logger) is False


def test_has_valid_technology_with_none(caplog):
    """Test has_valid_technology returns False for commits with None technologies."""
    logger = logging.getLogger("test")

    commit_stats = {
        "sha": "abc123",
        "technologies": None
    }

    assert has_valid_technology(commit_stats, logger) is False


def test_has_valid_technology_with_mixed_valid_invalid(caplog):
    """Test has_valid_technology returns True if at least one tech is valid."""
    logger = logging.getLogger("test")

    commit_stats = {
        "sha": "abc123",
        "technologies": {"supported": ["Markdown", "Python", "JSON"]}
    }

    assert has_valid_technology(commit_stats, logger) is True
