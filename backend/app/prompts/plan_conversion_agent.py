"""
Plan Conversion Agent Prompt

Converts markdown plan files to validated JSON structures.
Used after user hand-edits plans or during initial planning completion.

The agent reads markdown files from workspace paths, extracts structured phase info,
creates separate JSON files for each plan type, and persists them to workspace.
"""

from typing import Dict, List, Tuple

from app.core.artifact_subdirs import PLANNING_SUBDIR
from app.prompts.agents_claude_code import (
    E2E_PHASES_FILE,
    E2E_TEST_PLAN_FILE,
    IMPLEMENTATION_PLAN_FILE,
    PLANNING_PHASES_FILE,
)
from app.schemas.planning import PlanType


def plan_conversion_agent_template(
    workspace_root_dir: str,
    outputs_dir: str,
    plan_types: List[PlanType],
) -> str:
    """
    Generate prompt for converting markdown plans to JSON.

    Args:
        workspace_root_dir: Isolated workspace root (e.g., /workspaces/ws-09-1)
        outputs_dir: The outputs directory relative to workspace root (e.g., specflow)
        plan_types: List of PlanType enums to convert: [PlanType.IMPLEMENTATION], [PlanType.E2E], or both

    Returns:
        Prompt string for the conversion agent
    """
    planning_subdir = f"{outputs_dir}/{PLANNING_SUBDIR}"
    impl_markdown_path = f"{workspace_root_dir}/{planning_subdir}/{IMPLEMENTATION_PLAN_FILE}"
    e2e_markdown_path = f"{workspace_root_dir}/{planning_subdir}/{E2E_TEST_PLAN_FILE}"

    impl_json_path = f"{workspace_root_dir}/{planning_subdir}/{PLANNING_PHASES_FILE}"
    e2e_json_path = f"{workspace_root_dir}/{planning_subdir}/{E2E_PHASES_FILE}"

    # Build task description based on which plans to convert
    conversion_tasks = []
    if PlanType.IMPLEMENTATION in plan_types:
        conversion_tasks.append(
            f"1. **Implementation Plan**: Read {impl_markdown_path}, extract phases, "
            f"write {impl_json_path}"
        )
    if PlanType.E2E in plan_types:
        conversion_tasks.append(
            f"2. **E2E/Deploy Plan**: Read {e2e_markdown_path}, extract phases, "
            f"write {e2e_json_path}"
        )

    conversion_tasks_str = "\n".join(conversion_tasks)

    input_files = _format_plan_file_list(
        plan_types,
        {
            PlanType.IMPLEMENTATION: (impl_markdown_path, "implementation plan markdown"),
            PlanType.E2E: (e2e_markdown_path, "e2e/deploy plan markdown"),
        },
    )
    output_files = _format_plan_file_list(
        plan_types,
        {
            PlanType.IMPLEMENTATION: (impl_json_path, "implementation phases JSON"),
            PlanType.E2E: (e2e_json_path, "e2e/deploy phases JSON"),
        },
    )

    return f"""You are a plan conversion agent. Your task is to extract and validate phase information from markdown files and write JSON structures.

CRITICAL INSTRUCTIONS:
1. Each plan type can be processed independently — you MAY use subagents to parallelize (see below)
2. Read markdown files from the workspace filesystem
3. Extract EXACTLY the phases described in each markdown file
4. Create separate JSON output file for each plan type (not a combined file)
5. Write JSON files to the workspace filesystem at the paths specified below
6. Validate all JSON before writing

CONVERSION TASKS:
{conversion_tasks_str}

WORKSPACE PATHS:
- Workspace root: {workspace_root_dir}
- Planning subdirectory: {planning_subdir}/

INPUT FILES TO READ:
{input_files}

OUTPUT FILES TO WRITE:
{output_files}

JSON STRUCTURE FOR EACH PLAN:
```json
{{
  "phase_count": <number of phases>,
  "phases": [
    {{
      "number": <phase number>,
      "name": "<phase name>",
      "description": "<phase description (1-2 sentences)>",
      "estimated_commits": <estimate (1-10)>,
      "applicable_agent_mcps": []
    }},
    ...
  ]
}}
```

EXTRACTING applicable_agent_mcps FROM MARKDOWN:
Each phase section may contain an `**Agent MCPs**:` annotation line. Extract it as follows:
- Line is absent → omit the field from JSON (harness uses generation-level defaults)
- `**Agent MCPs**: none` → `"applicable_agent_mcps": []`
- `**Agent MCPs**: playwright` → `"applicable_agent_mcps": ["playwright"]`
- `**Agent MCPs**: figma` → `"applicable_agent_mcps": ["figma"]`
- `**Agent MCPs**: figma, playwright` → `"applicable_agent_mcps": ["figma", "playwright"]`

VALIDATION REQUIREMENTS:
- Extract EXACTLY the phases described in the markdown, no more, no less
- Phase numbers should be sequential (1, 2, 3, ...)
- Keep descriptions concise (1-2 sentences max)
- estimated_commits should be a reasonable number (1-10)
- Do not invent phases or modify the user's intent
- Validate each JSON using jq or by parsing before writing

PARALLELIZATION (if multiple plan types):
Use subagents to process plans in parallel:
- Each subagent handles one plan type: read markdown, extract phases, write JSON
- This is much faster than sequential processing
- Each subagent must follow the same JSON structure and validation rules
- Mention in the prompt that they are processing a SPECIFIC plan type and output path

WORKFLOW:
1. For EACH plan type in the conversion tasks:
   a. Read the markdown file from the workspace path
   b. Extract all phases (respect the exact structure in markdown)
   c. Build the JSON object with phase_count and phases array
   d. Validate the JSON is well-formed
   e. Write the JSON file to the specified output path using the Write tool
      (must write to {workspace_root_dir} paths — workspace-local paths only)
2. Return a summary of what was written (all output file paths)

IMPORTANT NOTES:
- Use Read tool to read markdown files from workspace filesystem
- Use Write tool to persist JSON files to workspace filesystem
- Paths must be absolute workspace paths starting with {workspace_root_dir}
- Only process the plan types provided in CONVERSION TASKS above
- Return file paths of all written JSON files in your final response
"""


def _format_plan_file_list(
    plan_types: List[PlanType],
    paths_by_type: Dict[PlanType, Tuple[str, str]],
) -> str:
    """Format a bullet list of `- <path> (<label>)` for each selected plan type."""
    return "\n".join(
        f"- {paths_by_type[pt][0]} ({paths_by_type[pt][1]})"
        for pt in plan_types
        if pt in paths_by_type
    )
