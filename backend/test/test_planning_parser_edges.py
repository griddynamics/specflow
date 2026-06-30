"""Planning parser edge cases: phase_count, empty phases, invalid MCP ids, prose wrapper.

See docs/agents/enabled-mcps.md (test matrix). Planner JSON unification: GH #138.
"""

from __future__ import annotations

import logging

import pytest

from app.services.planning_parser import parse_planning_output


def test_phase_count_mismatch_warns_but_returns_phases(caplog: pytest.LogCaptureFixture) -> None:
    """phase_count may disagree with len(phases); parser warns and still returns all listed phases."""
    text = """
```json
{
  "phase_count": 99,
  "phases": [
    {"number": 1, "name": "Only", "description": "One phase", "estimated_commits": 1}
  ]
}
```
"""
    with caplog.at_level(logging.WARNING, logger="app.services.planning_parser"):
        r = parse_planning_output(text, outputs_dir="./docs", logger=None)
    assert r is not None
    assert r.phase_count == 99
    assert len(r.phases) == 1
    assert r.phases[0].name == "Only"
    assert "Phase count mismatch" in caplog.text


def test_empty_phases_array() -> None:
    text = """
```json
{"phase_count": 0, "phases": []}
```
"""
    r = parse_planning_output(text, outputs_dir="./specflow")
    assert r is not None
    assert r.phase_count == 0
    assert r.phases == []


def test_invalid_agent_mcp_ids_filtered() -> None:
    """Unknown strings dropped; supported ids kept and lowercased."""
    text = """
```json
{
  "phase_count": 1,
  "phases": [{
    "number": 1,
    "name": "X",
    "description": "Y",
    "estimated_commits": 1,
    "applicable_agent_mcps": ["PLAYWRIGHT", "figma", "not_an_mcp", ""]
  }]
}
```
"""
    r = parse_planning_output(text)
    assert r is not None
    assert set(r.phases[0].applicable_agent_mcps or ()) == {"playwright", "figma"}


def test_applicable_agent_mcps_non_list_treated_as_omit() -> None:
    text = """
```json
{
  "phase_count": 1,
  "phases": [{
    "number": 1,
    "name": "X",
    "description": "Y",
    "estimated_commits": 1,
    "applicable_agent_mcps": "playwright"
  }]
}
```
"""
    r = parse_planning_output(text)
    assert r is not None
    assert r.phases[0].applicable_agent_mcps is None


def test_prose_before_and_after_planning_json() -> None:
    r = parse_planning_output(
        "Here is the plan.\n\n"
        '```json\n{"phase_count": 1, "phases": [{"number": 1, "name": "A", '
        '"description": "B", "estimated_commits": 1}]}\n```\n\nDone.',
        outputs_dir="./specflow",
    )
    assert r is not None
    assert r.phase_count == 1
    assert r.phases[0].name == "A"


def test_missing_required_phase_field_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    text = """
```json
{
  "phase_count": 1,
  "phases": [{"number": 1, "name": "X", "estimated_commits": 1}]
}
```
"""
    with caplog.at_level(logging.ERROR, logger="app.services.planning_parser"):
        r = parse_planning_output(text)
    assert r is None
    assert "Missing required field" in caplog.text
