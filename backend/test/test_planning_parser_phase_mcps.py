"""Planning parser: applicable_agent_mcps on phases."""

from app.services.planning_parser import parse_planning_output


def test_parse_applicable_agent_mcps() -> None:
    text = """
```json
{
  "phase_count": 2,
  "phases": [
    {
      "number": 1,
      "name": "Backend",
      "description": "API",
      "estimated_commits": 2,
      "applicable_agent_mcps": []
    },
    {
      "number": 2,
      "name": "UI",
      "description": "React",
      "estimated_commits": 3,
      "applicable_agent_mcps": ["playwright", "figma", "unknown"]
    }
  ]
}
```
"""
    r = parse_planning_output(text, outputs_dir="./specflow", logger=None)
    assert r is not None
    assert r.phases[0].applicable_agent_mcps == ()
    assert set(r.phases[1].applicable_agent_mcps or ()) == {"playwright", "figma"}


def test_omit_applicable_agent_mcps_is_none() -> None:
    text = """
```json
{"phase_count": 1, "phases": [{"number": 1, "name": "X", "description": "Y", "estimated_commits": 1}]}
```
"""
    r = parse_planning_output(text, outputs_dir="./specflow", logger=None)
    assert r is not None
    assert r.phases[0].applicable_agent_mcps is None
