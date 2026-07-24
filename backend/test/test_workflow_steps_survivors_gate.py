"""Minimum-survivors gate (un-drops what PR #47 deferred, strengthened).

Codegen: fail when fewer than min(2, total) workspaces survived — variance
scores need >= 2 samples; single-workspace runs still pass with their 1.
Deploy: min 1 survivor (all-failed rule). Partial failure above the bar
continues with survivors.
"""

from unittest.mock import Mock

import pytest

from app.schemas.workspace import WorkspaceSettings
from app.services.parallel_executor import ParallelAgentResult
from app.services.workflow_steps import (
    MIN_WORKSPACES_FOR_VARIANCE,
    InsufficientWorkspacesError,
    _raise_if_insufficient_workspaces,
)


def _ws(name: str) -> WorkspaceSettings:
    return WorkspaceSettings(name=name, workspace_path=f"/agent/{name}", provider="anthropic", model="m")


def _result(name: str, success: bool, error: str | None = None) -> ParallelAgentResult:
    return ParallelAgentResult(
        workspace_name=name,
        workspace_settings=_ws(name),
        result=None,
        success=success,
        error=error,
    )


def _gate(results, min_survivors=MIN_WORKSPACES_FOR_VARIANCE):
    _raise_if_insufficient_workspaces(
        results,
        step_label="code generation",
        logger=Mock(),
        min_survivors=min_survivors,
        requirement_reason="to compute variance-reduced estimates",
    )


class TestCodegenGate:
    def test_one_of_three_survivors_raises_with_variance_message(self):
        results = [
            _result("ws-1", False, "API Error: The socket connection was closed unexpectedly"),
            _result("ws-2", False, "API Error: Unable to connect to API (ConnectionRefused)"),
            _result("ws-3", True),
        ]
        with pytest.raises(InsufficientWorkspacesError) as exc:
            _gate(results)
        msg = str(exc.value)
        assert "Only 1 of 3 workspaces completed code generation" in msg
        assert "at least 2 are required to compute variance-reduced estimates" in msg
        assert "retry_generation" in msg
        # All-connection failures get the friendly network hint.
        assert "network interruption" in msg
        # No internal jargon in the user-facing message.
        assert "checkpoint" not in msg.lower()
        assert "firestore" not in msg.lower()

    def test_two_of_three_survivors_continues_with_warning(self):
        logger = Mock()
        results = [
            _result("ws-1", False, "boom"),
            _result("ws-2", True),
            _result("ws-3", True),
        ]
        _raise_if_insufficient_workspaces(
            results,
            step_label="code generation",
            logger=logger,
            min_survivors=MIN_WORKSPACES_FOR_VARIANCE,
            requirement_reason="to compute variance-reduced estimates",
        )
        logger.warning.assert_called_once()

    def test_zero_of_three_raises(self):
        results = [_result(f"ws-{i}", False, "err") for i in range(3)]
        with pytest.raises(InsufficientWorkspacesError):
            _gate(results)

    def test_single_workspace_run_success_passes(self):
        _gate([_result("ws-1", True)])  # min(2, 1) = 1 -> satisfied

    def test_single_workspace_run_failure_raises(self):
        with pytest.raises(InsufficientWorkspacesError) as exc:
            _gate([_result("ws-1", False, "err")])
        assert "at least 1 is required" in str(exc.value)

    def test_all_model_routing_failures_recommend_model_change(self):
        results = [
            _result("ws-1", False, "API returned an empty response"),
            _result("ws-2", False, "malformed response from model"),
            _result("ws-3", False, "empty response"),
        ]
        with pytest.raises(InsufficientWorkspacesError) as exc:
            _gate(results)
        assert "change the model in Settings" in str(exc.value)

    def test_connection_type_failure_does_not_recommend_model_change(self):
        results = [
            _result("ws-1", False, "connection refused"),
            _result("ws-2", False, "connection refused"),
            _result("ws-3", False, "connection refused"),
        ]
        with pytest.raises(InsufficientWorkspacesError) as exc:
            _gate(results)
        assert "change the model" not in str(exc.value)

    def test_all_success_and_non_parallel_items_are_noops(self):
        _gate([_result("ws-1", True), _result("ws-2", True)])
        _gate(["not-a-result", None])
        _gate([])
        _gate(None)


class TestDeployGate:
    def test_one_survivor_of_three_continues(self):
        results = [
            _result("ws-1", False, "deploy failed"),
            _result("ws-2", False, "deploy failed"),
            _result("ws-3", True),
        ]
        _gate(results, min_survivors=1)

    def test_zero_survivors_raises(self):
        results = [_result(f"ws-{i}", False, "deploy failed") for i in range(3)]
        with pytest.raises(InsufficientWorkspacesError):
            _gate(results, min_survivors=1)
