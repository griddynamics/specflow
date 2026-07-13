"""
_raise_if_all_workspaces_failed: turns a swallowed all-workspace failure into a real
exception so the orchestrator marks the run FAILED (instead of advancing the checkpoint
and emailing "complete"). Partial success survives (D2); a total connection loss gets a
friendly, actionable message.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from app.services.parallel_executor import ParallelAgentResult
from app.services.workflow_steps import _raise_if_all_workspaces_failed

_LOG = logging.getLogger("test")


def _result(name: str, *, success: bool, error: str | None = None) -> ParallelAgentResult:
    return ParallelAgentResult(
        workspace_name=name,
        workspace_settings=Mock(),
        result=None,
        success=success,
        error=error,
    )


def test_all_failed_connection_raises_friendly() -> None:
    results = [
        _result("ws-1", success=False,
                error="[ws-1] Phase 2 lost connection to the API. Error: API Error: Unable to connect to API (ConnectionRefused)"),
        _result("ws-2", success=False,
                error="[ws-2] Phase 1 lost connection to the API. Error: The socket connection was closed unexpectedly."),
    ]
    with pytest.raises(RuntimeError) as exc:
        _raise_if_all_workspaces_failed(results, phase_label="code generation", logger=_LOG)
    msg = str(exc.value).lower()
    assert "lost connection" in msg
    assert "retry_generation" in msg


def test_all_failed_generic_raises_with_details() -> None:
    results = [
        _result("ws-1", success=False, error="Aborting after 2 consecutive phase errors"),
        _result("ws-2", success=False, error="tool-call incompatibility"),
    ]
    with pytest.raises(RuntimeError) as exc:
        _raise_if_all_workspaces_failed(results, phase_label="code generation", logger=_LOG)
    msg = str(exc.value)
    assert "All workspaces failed during code generation" in msg
    assert "ws-1" in msg and "ws-2" in msg
    # Not a connection failure → must NOT show the connection guidance.
    assert "retry_generation" not in msg


def test_partial_success_does_not_raise() -> None:
    results = [
        _result("ws-1", success=True),
        _result("ws-2", success=False, error="Unable to connect to API (ConnectionRefused)"),
    ]
    # One workspace succeeded → survivors continue (D2), no raise.
    _raise_if_all_workspaces_failed(results, phase_label="code generation", logger=_LOG)


def test_all_success_does_not_raise() -> None:
    results = [_result("ws-1", success=True), _result("ws-2", success=True)]
    _raise_if_all_workspaces_failed(results, phase_label="code generation", logger=_LOG)


def test_empty_results_does_not_raise() -> None:
    _raise_if_all_workspaces_failed([], phase_label="code generation", logger=_LOG)
    _raise_if_all_workspaces_failed(None, phase_label="code generation", logger=_LOG)
