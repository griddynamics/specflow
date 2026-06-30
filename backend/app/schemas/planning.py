from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


class PlanType(str, Enum):
    """Plan type enumeration for type-safe plan processing.

    Allowed plan types that can be converted from markdown to JSON:
    - IMPLEMENTATION: Main implementation plan (IMPLEMENTATION_PLAN.md)
    - E2E: E2E/deploy test plan (e2e-test-plan.md)
    """
    IMPLEMENTATION = "implementation"
    E2E = "e2e"


@dataclass
class PhaseInfo:
    """Information about a single implementation phase."""
    number: int
    name: str
    description: str
    estimated_commits: int
    # Subset of SUPPORTED_MCPS to attach for this phase only; None = use generation's full enabled set.
    applicable_agent_mcps: Optional[Tuple[str, ...]] = None


@dataclass
class PlanningResult:
    """Result from planning agent with structured output."""
    phase_count: int
    phases: List[PhaseInfo]
    plan_file_path: str  # Path to IMPLEMENTATION_PLAN.md
    plan_markdown_checksum: Optional[str] = None  # SHA256 of markdown file for change detection
