from dataclasses import dataclass
from typing import Optional


@dataclass
class DeployGithubContext:
    """Deploy-phase context injected into the agent prompt.

    Carries the GitHub coordinates needed for gh CLI commands. Built once per
    workspace in _build_deploy_github_context and threaded through
    execute_all_phases → phase_agent_fn → generate_deploy_phase_agent_template.
    """

    github_repo: Optional[str]
    github_ref: str
    deploy_workflow: str
