"""
Unit tests for SKIP_MODE mock side effects functionality.

Tests that mock planning results and files are generated correctly when SKIP_MODE is enabled.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import logging

import pytest

from app.core.telemetry_context import TelemetryContext
from app.schemas.llm_tier import WorkflowName
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.services.skip_mode_mock import (
    build_skip_mode_mock_usage_delta,
    create_mock_planning_result,
    generate_mock_agent_result,
    generate_mock_implementation_plan,
    generate_mock_planning_json,
    get_mock_phase_count,
    is_skip_mode_enabled,
    persist_skip_mode_mock_agent_query_totals,
    setup_skip_mode_workspace_commits,
    skip_mode_mock_usage_persist_enabled,
)


def test_is_skip_mode_enabled():
    """Test skip mode detection."""
    original_value = os.environ.get("SKIP_AGENT_EXECUTION")
    
    try:
        # Test enabled values
        for value in ["true", "TRUE", "True", "1", "yes", "YES", "Yes"]:
            os.environ["SKIP_AGENT_EXECUTION"] = value
            assert is_skip_mode_enabled() is True
        
        # Test disabled values
        for value in ["false", "FALSE", "0", "", "no", "NO"]:
            os.environ["SKIP_AGENT_EXECUTION"] = value
            assert is_skip_mode_enabled() is False
        
        # Test unset
        os.environ.pop("SKIP_AGENT_EXECUTION", None)
        assert is_skip_mode_enabled() is False
    finally:
        if original_value is None:
            os.environ.pop("SKIP_AGENT_EXECUTION", None)
        else:
            os.environ["SKIP_AGENT_EXECUTION"] = original_value


def test_get_mock_phase_count():
    """Test phase count configuration."""
    original_value = os.environ.get("SKIP_MODE_PHASE_COUNT")
    
    try:
        # Test default
        os.environ.pop("SKIP_MODE_PHASE_COUNT", None)
        assert get_mock_phase_count() == 6
        
        # Test custom value
        os.environ["SKIP_MODE_PHASE_COUNT"] = "8"
        assert get_mock_phase_count() == 8
        
        # Test clamping (min)
        os.environ["SKIP_MODE_PHASE_COUNT"] = "0"
        assert get_mock_phase_count() == 1
        
        # Test clamping (max)
        os.environ["SKIP_MODE_PHASE_COUNT"] = "25"
        assert get_mock_phase_count() == 20
        
        # Test invalid value (should default)
        os.environ["SKIP_MODE_PHASE_COUNT"] = "invalid"
        assert get_mock_phase_count() == 6
    finally:
        if original_value is None:
            os.environ.pop("SKIP_MODE_PHASE_COUNT", None)
        else:
            os.environ["SKIP_MODE_PHASE_COUNT"] = original_value


def test_generate_mock_planning_json():
    """Test mock planning JSON generation."""
    json_str = generate_mock_planning_json(phase_count=3)
    data = json.loads(json_str)
    
    assert "phase_count" in data
    assert "phases" in data
    assert data["phase_count"] == 3
    assert len(data["phases"]) == 3
    
    # Check phase structure
    for i, phase in enumerate(data["phases"]):
        assert phase["number"] == i + 1
        assert "name" in phase
        assert "description" in phase
        assert "estimated_commits" in phase
        assert isinstance(phase["estimated_commits"], int)


def test_generate_mock_implementation_plan():
    """Test mock IMPLEMENTATION_PLAN.md file generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        plan_path = generate_mock_implementation_plan(
            outputs_dir=tmpdir,
            phase_count=4
        )
        
        assert Path(plan_path).exists()
        assert Path(plan_path).name == "IMPLEMENTATION_PLAN.md"
        
        content = Path(plan_path).read_text()
        
        # Check for required sections
        assert "Architectural Decisions - Locked Values" in content
        assert "Part A: Universal Dimensions" in content
        assert "Part B: Technology-Specific Dimensions" in content
        assert "Part D: Micro-Level Consistency Locks" in content
        
        # Check for phases
        assert "Phase 1:" in content
        assert "Phase 4:" in content
        assert "Phase 5:" not in content  # Should not have more than 4


def test_generate_mock_agent_result():
    """Test mock agent result generation."""
    result = generate_mock_agent_result(
        workspace_path="/tmp/test",
        outputs_dir="specflow"
    )
    
    # Should contain JSON in markdown code block
    assert "```json" in result or "```" in result
    assert "phase_count" in result
    
    # Should be parseable
    json_match = None
    if "```json" in result:
        import re
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', result, re.DOTALL)
    elif "```" in result:
        import re
        json_match = re.search(r'```\s*(\{.*?\})\s*```', result, re.DOTALL)
    
    if json_match:
        data = json.loads(json_match.group(1))
        assert "phase_count" in data
        assert "phases" in data


def test_create_mock_planning_result():
    """Test complete mock planning result creation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = tmpdir
        outputs_dir = "specflow"
        
        result = create_mock_planning_result(
            outputs_dir=outputs_dir,
            workspace_path=workspace_path
        )
        
        # Check PlanningResult structure
        assert result.phase_count > 0
        assert len(result.phases) == result.phase_count
        assert result.plan_file_path is not None
        
        # Check that plan file exists
        plan_path = Path(result.plan_file_path)
        assert plan_path.exists()
        
        # Check phases
        for i, phase in enumerate(result.phases):
            assert phase.number == i + 1
            assert phase.name is not None
            assert phase.description is not None
            assert phase.estimated_commits > 0


def test_create_mock_planning_result_custom_phase_count():
    """Test mock planning result with custom phase count."""
    original_value = os.environ.get("SKIP_MODE_PHASE_COUNT")
    
    try:
        os.environ["SKIP_MODE_PHASE_COUNT"] = "10"
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_mock_planning_result(
                outputs_dir="specflow",
                workspace_path=tmpdir
            )
            
            assert result.phase_count == 10
            assert len(result.phases) == 10
    finally:
        if original_value is None:
            os.environ.pop("SKIP_MODE_PHASE_COUNT", None)
        else:
            os.environ["SKIP_MODE_PHASE_COUNT"] = original_value


def test_setup_skip_mode_workspace_commits_creates_skip_seed_commit():
    """SKIP_MODE workspace setup creates a git commit with SKIP_ subject (no JSON sidecar)."""
    log = logging.getLogger("test_skip_mode")
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@test"],
            cwd=ws,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=ws,
            check=True,
            capture_output=True,
        )
        assert setup_skip_mode_workspace_commits(
            workspace_path=str(ws),
            outputs_dir="specflow",
            logger=log,
        )
        r = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=ws,
            check=True,
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == "SKIP_mode_seed_workspace"
        assert not (ws / "specflow" / "code-generation-commits.json").exists()


def test_skip_mode_mock_usage_persist_enabled_env():
    orig = os.environ.get("SKIP_MODE_MOCK_PERSIST_USAGE")
    try:
        os.environ["SKIP_MODE_MOCK_PERSIST_USAGE"] = "false"
        assert skip_mode_mock_usage_persist_enabled() is False
        os.environ["SKIP_MODE_MOCK_PERSIST_USAGE"] = "1"
        assert skip_mode_mock_usage_persist_enabled() is True
    finally:
        if orig is None:
            os.environ.pop("SKIP_MODE_MOCK_PERSIST_USAGE", None)
        else:
            os.environ["SKIP_MODE_MOCK_PERSIST_USAGE"] = orig


def test_build_skip_mode_mock_usage_delta_non_empty():
    mu, cost = build_skip_mode_mock_usage_delta("anthropic/claude-3")
    assert not mu.is_empty()
    assert mu.model_name == "anthropic/claude-3"
    assert cost > 0.0


@pytest.mark.asyncio
async def test_persist_skip_mode_mock_calls_registered_handler(monkeypatch):
    monkeypatch.setenv("SKIP_AGENT_EXECUTION", "true")
    calls: list[tuple] = []

    async def handler(gid: str, wf: str, ws: str, delta, cost: float) -> None:
        calls.append((gid, wf, ws, delta, cost))

    TelemetryContext.merge_generation_id("gen-skip-1")
    TelemetryContext.set_workspace_name("pool-ws-1")
    TelemetryContext.set_agent_query_totals_handler(handler)
    try:
        log = logging.getLogger("test_persist_skip")
        await persist_skip_mode_mock_agent_query_totals(
            model="anthropic/claude-sonnet-4",
            workspace_path="/tmp/pool-ws-1",
            logger=log,
            workflow=TelemetryWorkflowLabel.plain(WorkflowName.PLANNING),
        )
        assert len(calls) == 1
        gid, wf, ws, delta, cost = calls[0]
        assert gid == "gen-skip-1"
        assert wf == WorkflowName.PLANNING.value
        assert ws == "pool-ws-1"
        assert not delta.is_empty()
        assert cost > 0.0
    finally:
        TelemetryContext.clear_context()


@pytest.mark.asyncio
async def test_persist_skip_mode_mock_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("SKIP_AGENT_EXECUTION", "true")
    monkeypatch.setenv("SKIP_MODE_MOCK_PERSIST_USAGE", "false")
    called: list = []

    async def handler(*args):
        called.append(args)

    TelemetryContext.merge_generation_id("gen-x")
    TelemetryContext.set_agent_query_totals_handler(handler)
    try:
        await persist_skip_mode_mock_agent_query_totals(
            model="m",
            workspace_path="/tmp/x",
            logger=logging.getLogger("t"),
        )
        assert called == []
    finally:
        TelemetryContext.clear_context()

