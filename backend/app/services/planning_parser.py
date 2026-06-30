"""
Parser for structured output from planning agent.

Extracts phase count and phase information from planning agent's JSON output.
"""

import json
import logging
import re
from typing import Optional, Tuple

from app.core.config import SUPPORTED_MCPS
from app.schemas.planning import PhaseInfo, PlanningResult
from app.prompts.agents_claude_code import IMPLEMENTATION_PLAN_FILE
from app.core.artifact_subdirs import PLANNING_SUBDIR


def parse_applicable_agent_mcps(phase_data: dict) -> Optional[Tuple[str, ...]]:
    """Extract and validate applicable MCPs from phase data.

    Shared between agent-JSON parsing (`construct_planning_result`) and Firestore-dict
    reconstruction (`_planning_result_from_dict`) so the normalization stays in one place.
    """
    raw = phase_data.get("applicable_agent_mcps", None)
    if not isinstance(raw, list):
        return None
    valid = {
        x.strip().lower()
        for x in raw
        if isinstance(x, str) and x.strip() and x.strip().lower() in SUPPORTED_MCPS
    }
    return tuple(sorted(valid))


def construct_planning_result(
    data: dict,
    outputs_dir: str,
    plan_filename: str,
    logger: logging.Logger,
    markdown_checksum: Optional[str] = None,
) -> Optional[PlanningResult]:
    """Construct PlanningResult from parsed JSON data.

    Reusable helper for parsing JSON (from agent response or files).

    Args:
        data: Parsed JSON dict with phase_count and phases
        outputs_dir: Directory where plan file is located
        plan_filename: Name of the markdown plan file
        logger: Logger instance
        markdown_checksum: Optional checksum of the markdown file

    Returns:
        PlanningResult if parsing succeeds, None otherwise
    """
    # Validate required fields
    if "phase_count" not in data:
        logger.error("Missing 'phase_count' field in planning output")
        return None

    if "phases" not in data:
        logger.error("Missing 'phases' field in planning output")
        return None

    phase_count = data["phase_count"]
    phases_data = data["phases"]

    if not isinstance(phases_data, list):
        logger.error("'phases' must be a list")
        return None

    if len(phases_data) != phase_count:
        logger.warning(
            f"Phase count mismatch: phase_count={phase_count}, "
            f"but phases list has {len(phases_data)} items"
        )

    # Parse phases
    phases = []
    for phase_data in phases_data:
        try:
            phase = PhaseInfo(
                number=phase_data["number"],
                name=phase_data["name"],
                description=phase_data["description"],
                estimated_commits=phase_data.get("estimated_commits", 0),
                applicable_agent_mcps=parse_applicable_agent_mcps(phase_data),
            )
            phases.append(phase)
        except KeyError as e:
            logger.error(f"Missing required field in phase data: {e}")
            logger.debug(f"Phase data: {phase_data}")
            return None

    plan_file_path = f"{outputs_dir}/{PLANNING_SUBDIR}/{plan_filename}"

    logger.info(
        f"Parsed planning output: {phase_count} phases, "
        f"plan file: {plan_file_path}"
    )

    return PlanningResult(
        phase_count=phase_count,
        phases=phases,
        plan_file_path=plan_file_path,
        plan_markdown_checksum=markdown_checksum,
    )


def parse_planning_output(
    agent_result: str,
    outputs_dir: str = "./specflow",
    logger: Optional[logging.Logger] = None,
    plan_filename: str = IMPLEMENTATION_PLAN_FILE,
) -> Optional[PlanningResult]:
    """
    Parse structured output from planning agent to extract phase information.

    The planning agent should return JSON in this format:
    {
        "phase_count": 5,
        "phases": [
            {
                "number": 1,
                "name": "Project Setup",
                "description": "...",
                "estimated_commits": 3
            },
            ...
        ]
    }

    Args:
        agent_result: The text result from the planning agent
        outputs_dir: Directory where IMPLEMENTATION_PLAN.md should be located
        logger: Optional logger instance
        plan_filename: Name of the markdown plan file

    Returns:
        PlanningResult if parsing succeeds, None otherwise
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    if not agent_result:
        logger.error("Agent result is empty")
        return None

    # Try to extract JSON from the agent result
    # The JSON might be embedded in markdown code blocks or plain text
    json_match = re.search(
        r'```(?:json)?\s*(\{.*?\})\s*```',
        agent_result,
        re.DOTALL
    )

    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find JSON object directly
        json_match = re.search(r'\{.*"phase_count".*\}', agent_result, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            logger.error("Could not find JSON in agent result")
            return None

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        logger.debug(f"JSON string: {json_str[:500]}")
        return None

    return construct_planning_result(data, outputs_dir, plan_filename, logger)
