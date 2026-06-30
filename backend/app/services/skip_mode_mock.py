"""
Skip Mode Mock Service

Generates realistic mock side effects when SKIP_MODE is enabled, allowing
full end-to-end testing without actual agent execution.

When SKIP_AGENT_EXECUTION is enabled, this service creates:
- Mock planning JSON output
- IMPLEMENTATION_PLAN.md file
- Other expected files (PROGRESS.md, etc.)
- Mock git commits (SKIP_* seed only; P10Y uses git-log metadata, not a JSON sidecar)

Optional env for **Firestore usage + cost** during SKIP_MODE (same fields as real runs):

- ``SKIP_MODE_MOCK_PERSIST_USAGE`` — default ``true``; set ``false`` to skip DB writes (tests).
- ``SKIP_MODE_MOCK_INPUT_TOKENS``, ``SKIP_MODE_MOCK_OUTPUT_TOKENS``,
  ``SKIP_MODE_MOCK_CACHE_WRITE_TOKENS``, ``SKIP_MODE_MOCK_CACHE_READ_TOKENS``,
  ``SKIP_MODE_MOCK_NUM_TURNS`` — integer buckets (sensible defaults).
- ``SKIP_MODE_MOCK_COST_USD`` — float added per skipped logical agent call.
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR
from app.core.telemetry_context import TelemetryContext
from app.prompts.agents_claude_code import E2E_PHASES_FILE, PLANNING_PHASES_FILE
from app.schemas.estimate import ComponentEstimation, EstimationMetrics, WorkspaceEstimation
from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.planning import PhaseInfo, PlanningResult, PlanType
from app.schemas.specification import SpecReadiness, SpecificationCompletenessResult
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.schemas.workspace import WorkspaceSettings


def is_skip_mode_enabled() -> bool:
    """Check if SKIP_MODE is enabled via environment variable."""
    return os.getenv("SKIP_AGENT_EXECUTION", "").lower() in ("true", "1", "yes")


def get_mock_phase_count() -> int:
    """Get configured number of phases for mock planning (default: 6)."""
    try:
        count = int(os.getenv("SKIP_MODE_PHASE_COUNT", "6"))
        return max(1, min(count, 20))  # Clamp between 1 and 20
    except ValueError:
        return 6


def skip_mode_mock_usage_persist_enabled() -> bool:
    """When False, SKIP_MODE does not write synthetic usage to Firestore (unit tests)."""
    return os.getenv("SKIP_MODE_MOCK_PERSIST_USAGE", "true").lower() not in ("false", "0", "no")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def build_skip_mode_mock_usage_delta(model_name: str) -> tuple[ModelTokenUsage, float]:
    """Synthetic token buckets + USD for one skipped ``agent_query`` (notifications / status API)."""
    mu = ModelTokenUsage(
        model_name=model_name or "",
        num_turns=_env_int("SKIP_MODE_MOCK_NUM_TURNS", 1),
        input_tokens=_env_int("SKIP_MODE_MOCK_INPUT_TOKENS", 2_500),
        output_tokens=_env_int("SKIP_MODE_MOCK_OUTPUT_TOKENS", 800),
        cache_write_tokens=_env_int("SKIP_MODE_MOCK_CACHE_WRITE_TOKENS", 0),
        cache_read_tokens=_env_int("SKIP_MODE_MOCK_CACHE_READ_TOKENS", 100),
    )
    cost_usd = _env_float("SKIP_MODE_MOCK_COST_USD", 0.0234)
    return mu, cost_usd


async def persist_skip_mode_mock_agent_query_totals(
    *,
    model: str,
    workspace_path: str,
    logger: logging.Logger,
    workflow: Optional[TelemetryWorkflowLabel] = None,
) -> None:
    """Append SKIP_MODE mock usage to the same Firestore fields as real agent runs.

    Requires ``TelemetryContext.set_agent_query_totals_handler`` and ``generation_id``
    (as in orchestration / spec-analysis). No-op when handler or generation id is missing.
    """
    if not is_skip_mode_enabled() or not skip_mode_mock_usage_persist_enabled():
        return
    persist = TelemetryContext.get_agent_query_totals_handler()
    eid = TelemetryContext.get_generation_id()
    if not persist or not eid:
        return
    wf = workflow or TelemetryContext.get_workflow()
    workflow_key = wf.to_stored_string() if wf else "SKIP_MODE"
    ws_name = TelemetryContext.get_workspace_name()
    if not ws_name:
        ws_name = os.path.basename(str(workspace_path).rstrip(os.sep)) or "_"
    delta, cost_usd = build_skip_mode_mock_usage_delta(model)
    if delta.is_empty() and cost_usd <= 0.0:
        return
    try:
        await persist(eid, workflow_key, ws_name, delta, cost_usd)
        logger.info(
            "[SKIP_MODE] Persisted mock LLM usage for generation=%s workflow=%s workspace=%s",
            eid,
            workflow_key,
            ws_name,
        )
    except Exception as exc:
        logger.warning(
            "[SKIP_MODE] Mock usage persist failed (non-fatal): %s",
            exc,
            exc_info=True,
        )


def generate_mock_planning_json(phase_count: Optional[int] = None) -> str:
    """
    Generate mock planning JSON output that matches the expected format.
    
    Args:
        phase_count: Number of phases to generate (defaults to env var or 6)
        
    Returns:
        JSON string with phase_count and phases array
    """
    if phase_count is None:
        phase_count = get_mock_phase_count()
    
    phases = []
    phase_names = [
        "Project Setup",
        "Core Backend API",
        "User Management Backend",
        "Frontend Foundation",
        "User Interface Implementation",
        "Integration & Testing",
        "Infrastructure & Deployment",
        "Documentation & Polish",
    ]
    
    phase_descriptions = [
        "Initialize project structure, dependencies, and basic configuration",
        "Implement main API endpoints, services, and data models",
        "Implement user authentication, authorization, and user management APIs",
        "Set up frontend framework, routing, and core UI components",
        "Build user-facing pages, forms, and interactive components",
        "Write comprehensive tests, fix integration issues, and validate functionality",
        "Configure Docker, deployment pipelines, and production infrastructure",
        "Complete documentation, code cleanup, and final refinements",
    ]
    
    for i in range(phase_count):
        phase_num = i + 1
        name_idx = min(i, len(phase_names) - 1)
        desc_idx = min(i, len(phase_descriptions) - 1)
        
        # Estimate commits: 2-5 per phase, with setup/deployment having fewer
        if phase_num == 1:
            estimated_commits = 3
        elif phase_num == phase_count:
            estimated_commits = 2
        else:
            estimated_commits = 4
        
        phases.append({
            "number": phase_num,
            "name": phase_names[name_idx] if i < len(phase_names) else f"Phase {phase_num}",
            "description": phase_descriptions[desc_idx] if i < len(phase_descriptions) else f"Complete Phase {phase_num} implementation tasks",
            "estimated_commits": estimated_commits
        })
    
    planning_data = {
        "phase_count": phase_count,
        "phases": phases
    }
    
    return json.dumps(planning_data, indent=2)


def write_mock_planning_phases_json(
    workspace_root: str,
    outputs_dir: str,
    plan_types: list,
    phase_count: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Write mock planning_phases.json and/or e2e_phases.json files for SKIP_MODE.

    After PR #176, the planning agent produces only markdown. The conversion agent
    (REPARSE_PLAN step) creates the JSON files. In SKIP_MODE, we need to create these
    files directly so downstream code doesn't fail when trying to load them.

    Args:
        workspace_root: Root directory of workspace
        outputs_dir: Outputs directory (e.g., "docs")
        plan_types: List of PlanType enums to generate (IMPLEMENTATION and/or E2E)
        phase_count: Number of phases (defaults to env var or 6)
        logger: Optional logger instance
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    if phase_count is None:
        phase_count = get_mock_phase_count()

    planning_dir = Path(workspace_root) / outputs_dir / PLANNING_SUBDIR
    planning_dir.mkdir(parents=True, exist_ok=True)

    json_str = generate_mock_planning_json(phase_count=phase_count)

    for plan_type in plan_types:
        if plan_type == PlanType.IMPLEMENTATION:
            fname = PLANNING_PHASES_FILE
        elif plan_type == PlanType.E2E:
            fname = E2E_PHASES_FILE
        else:
            logger.warning("Unknown plan type: %s, skipping", plan_type)
            continue

        json_path = planning_dir / fname
        json_path.write_text(json_str)
        logger.info("SKIP_MODE: wrote %s", json_path)


def generate_mock_implementation_plan(
    outputs_dir: str,
    phase_count: Optional[int] = None,
    logger: Optional[logging.Logger] = None
) -> str:
    """
    Generate a mock IMPLEMENTATION_PLAN.md file.
    
    Args:
        outputs_dir: Directory where the plan file should be created
        phase_count: Number of phases (defaults to env var or 6)
        logger: Optional logger instance
        
    Returns:
        Path to the created plan file
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    if phase_count is None:
        phase_count = get_mock_phase_count()
    
    plan_path = Path(outputs_dir) / PLANNING_SUBDIR / "IMPLEMENTATION_PLAN.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Generate phase sections
    phase_sections = []
    phase_names = [
        "Project Setup",
        "Core Backend API",
        "User Management Backend",
        "Frontend Foundation",
        "User Interface Implementation",
        "Integration & Testing",
        "Infrastructure & Deployment",
        "Documentation & Polish",
    ]
    
    phase_tasks = [
        ["Initialize project structure", "Set up dependencies", "Configure build tools", "Create basic configuration files"],
        ["Design API structure", "Implement core endpoints", "Create data models", "Add request validation"],
        ["Implement authentication", "Add authorization middleware", "Create user endpoints", "Add user management"],
        ["Set up frontend framework", "Configure routing", "Create base components", "Set up state management"],
        ["Build main pages", "Create forms", "Implement user interactions", "Add error handling"],
        ["Write unit tests", "Add integration tests", "Fix bugs", "Validate functionality"],
        ["Configure Docker", "Set up CI/CD", "Prepare deployment config", "Add monitoring"],
        ["Write documentation", "Code cleanup", "Final testing", "Prepare release"],
    ]
    
    for i in range(phase_count):
        phase_num = i + 1
        name_idx = min(i, len(phase_names) - 1)
        tasks_idx = min(i, len(phase_tasks) - 1)
        
        phase_name = phase_names[name_idx] if i < len(phase_names) else f"Phase {phase_num}"
        tasks = phase_tasks[tasks_idx] if i < len(phase_tasks) else [f"Task 1 for Phase {phase_num}", f"Task 2 for Phase {phase_num}"]
        
        phase_sections.append(f"""## Phase {phase_num}: {phase_name}

### Description
{phase_name} implementation phase.

### Tasks
{chr(10).join(f"- [ ] {task}" for task in tasks)}

### Dependencies
{"None" if phase_num == 1 else f"Depends on Phase {phase_num - 1}"}

### Enforcement Checkpoints
- Follow Part D naming conventions
- Maintain code quality standards
- Write appropriate tests
""")
    
    plan_content = f"""# Implementation Plan

## Architectural Decisions - Locked Values

These values are LOCKED from specification_completeness.md and MUST NOT be changed by any phase agent.
**AGGRESSIVE ENFORCEMENT**: Any deviation from these locks is a CRITICAL error.

### Part A: Universal Dimensions (ALL MANDATORY)

| Dimension | Locked Value | Implementation Approach |
|-----------|--------------|------------------------|
| A1. Data Persistence | External Database | PostgreSQL with connection pooling |
| A2. Infrastructure | Containerized | Docker Compose for local, Kubernetes for production |
| A3. Scale Target | Small Team | <100 concurrent users/requests |
| A4. Technology Stack | Python 3.11, FastAPI | FastAPI framework with Pydantic validation |
| A5. Quality & Testing | Production | 70%+ coverage, integration tests, CI/CD required |
| A6. Scope Boundaries | Defined in specification | See specification for in-scope/out-of-scope features |

### Part B: Technology-Specific Dimensions

#### B2. API/SERVICE PROJECTS
| Dimension | Locked Value |
|-----------|--------------|
| B2.1 API Style | REST |
| B2.2 Framework | FastAPI |
| B2.3 Serialization | JSON |
| B2.4 Versioning Strategy | URL versioning (/api/v1/) |
| B2.5 Documentation | OpenAPI/Swagger |

### Part C: Project-Specific Dimensions

| Dimension | Locked Value |
|-----------|--------------|
| C1. Code Organization | Feature-based structure |
| C2. Error Handling Strategy | Centralized error middleware |
| C3. Logging & Observability | Structured logging with JSON format |
| C4. Configuration Management | Environment variables + config files |

### Part D: Micro-Level Consistency Locks (AGGRESSIVE ENFORCEMENT)

| Convention | Locked Value | Examples |
|------------|--------------|----------|
| D1. File naming | snake_case | user_service.py, api_routes.py |
| D1. Directory naming | plural | components/, services/, utils/ |
| D1. Function naming | snake_case | get_user(), create_order() |
| D1. Variable naming | snake_case | user_name, order_items |
| D2. Async style | async/await | Always use async/await, never .then() |
| D2. Error handling | raise exceptions | raise AppError(), not return None |
| D2. Import style | absolute imports | from app.services.user import UserService |
| D3. Source location | src/ | All source code under src/ |
| D3. Test location | adjacent | user_service.py → user_service_test.py |
| D4. Commit granularity | atomic | Target 30-50 commits total |
| D4. Commit format | conventional | feat(auth): add login endpoint |

### Part E: Feature Completeness Checklist

- [ ] All features from specification implemented
- [ ] All sub-tasks from feature_implementation_standards.md completed
- [ ] Integration tests passing
- [ ] Documentation complete

---

## Implementation Phases

{chr(10).join(phase_sections)}

---

## Notes

This is a mock implementation plan generated by SKIP_MODE for testing purposes.
In normal operation, this file would be created by the planning agent.
"""
    
    plan_path.write_text(plan_content, encoding="utf-8")
    logger.info(f"[SKIP_MODE] Created mock IMPLEMENTATION_PLAN.md at {plan_path}")
    
    return str(plan_path)


def create_mock_planning_result(
    outputs_dir: str,
    workspace_path: str,
    logger: Optional[logging.Logger] = None
) -> PlanningResult:
    """
    Create a complete mock PlanningResult with all side effects.
    
    This function:
    1. Generates mock planning JSON
    2. Creates IMPLEMENTATION_PLAN.md file
    3. Returns a PlanningResult object
    
    Args:
        outputs_dir: Directory where output files should be created
        workspace_path: Workspace root path (for resolving relative paths)
        logger: Optional logger instance
        
    Returns:
        PlanningResult with mock phase data
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    phase_count = get_mock_phase_count()
    
    # Resolve outputs_dir relative to workspace if needed
    if not os.path.isabs(outputs_dir):
        outputs_path = Path(workspace_path) / outputs_dir
    else:
        outputs_path = Path(outputs_dir)
    
    # Generate mock implementation plan file
    plan_file_path = generate_mock_implementation_plan(
        outputs_dir=str(outputs_path),
        phase_count=phase_count,
        logger=logger
    )
    
    # Generate phases
    phases = []
    phase_names = [
        "Project Setup",
        "Core Backend API",
        "User Management Backend",
        "Frontend Foundation",
        "User Interface Implementation",
        "Integration & Testing",
        "Infrastructure & Deployment",
        "Documentation & Polish",
    ]
    
    phase_descriptions = [
        "Initialize project structure, dependencies, and basic configuration",
        "Implement main API endpoints, services, and data models",
        "Implement user authentication, authorization, and user management APIs",
        "Set up frontend framework, routing, and core UI components",
        "Build user-facing pages, forms, and interactive components",
        "Write comprehensive tests, fix integration issues, and validate functionality",
        "Configure Docker, deployment pipelines, and production infrastructure",
        "Complete documentation, code cleanup, and final refinements",
    ]
    
    for i in range(phase_count):
        phase_num = i + 1
        name_idx = min(i, len(phase_names) - 1)
        desc_idx = min(i, len(phase_descriptions) - 1)
        
        if phase_num == 1:
            estimated_commits = 3
        elif phase_num == phase_count:
            estimated_commits = 2
        else:
            estimated_commits = 4
        
        phases.append(PhaseInfo(
            number=phase_num,
            name=phase_names[name_idx] if i < len(phase_names) else f"Phase {phase_num}",
            description=phase_descriptions[desc_idx] if i < len(phase_descriptions) else f"Complete Phase {phase_num} implementation tasks",
            estimated_commits=estimated_commits
        ))
    
    logger.info(
        f"[SKIP_MODE] Generated mock planning result: {phase_count} phases, "
        f"plan file: {plan_file_path}"
    )
    
    return PlanningResult(
        phase_count=phase_count,
        phases=phases,
        plan_file_path=plan_file_path
    )


def generate_mock_agent_result(
    workspace_path: str,
    outputs_dir: str,
    logger: Optional[logging.Logger] = None
) -> str:
    """
    Generate mock agent result JSON that can be parsed by parse_planning_output.
    
    This wraps the JSON in a markdown code block to match real agent output format.
    
    Args:
        workspace_path: Workspace root path
        outputs_dir: Output directory path
        logger: Optional logger instance
        
    Returns:
        String containing mock agent result with JSON in markdown code block
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    phase_count = get_mock_phase_count()
    json_output = generate_mock_planning_json(phase_count=phase_count)
    
    # Format as agent would return it (with markdown code block)
    mock_result = f"""Planning complete. Created implementation plan with {phase_count} phases.

```json
{json_output}
```
"""
    
    logger.debug(f"[SKIP_MODE] Generated mock agent result with {phase_count} phases")
    return mock_result


def create_mock_spec_completeness_file(
    outputs_dir: str,
    logger: Optional[logging.Logger] = None
) -> str:
    """
    Generate a mock specification_completeness.md file for SKIP mode.

    Args:
        outputs_dir: Directory where the completeness file should be created
        logger: Optional logger instance

    Returns:
        Path to the created completeness file
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    completeness_file = Path(outputs_dir) / ANALYSIS_SUBDIR / "specification_completeness.md"
    completeness_file.parent.mkdir(parents=True, exist_ok=True)
    
    content = """# Specification Completeness Analysis

**Status**: READY ✅  
**Analysis Date**: Mock analysis (SKIP_MODE enabled)

## Executive Summary

This is a mock specification completeness analysis generated for testing purposes.
In SKIP_MODE, the agent execution is bypassed and this file is generated automatically.

## Dimension Analysis

### Part A: Universal Dimensions (6/6 locked)
- ✅ A1. Data Persistence: External Database (PostgreSQL)
- ✅ A2. Infrastructure: Containerized (Docker Compose)
- ✅ A3. Scale Target: Small Team (<100 concurrent users)
- ✅ A4. Technology Stack: Python 3.11, FastAPI
- ✅ A5. Quality & Testing: Production (70%+ coverage, CI/CD)
- ✅ A6. Scope Boundaries: Clearly defined

### Part B: Technology-Specific Dimensions (5/5 locked)
- ✅ B2.1 API Style: REST
- ✅ B2.2 Framework: FastAPI
- ✅ B2.3 Serialization: JSON
- ✅ B2.4 Versioning Strategy: URL versioning (/api/v1/)
- ✅ B2.5 Documentation: OpenAPI/Swagger

### Part C: Project-Specific Dimensions (4/4 locked)
- ✅ C1. Code Organization: Feature-based structure
- ✅ C2. Error Handling: Centralized middleware
- ✅ C3. Logging: Structured JSON logging
- ✅ C4. Configuration: Environment variables

### Part D: Micro-Level Conventions (7/7 locked)
- ✅ D1. Naming: snake_case files, functions, variables
- ✅ D2. Async: async/await pattern
- ✅ D3. Source: All code under src/
- ✅ D4. Commits: Atomic, conventional format

### Part E: Feature Completeness
All features from specification are fully specified with acceptance criteria.

## Issues Found

### ✅ No Critical Issues
All required architectural dimensions are locked and well-defined.

### Medium Priority Suggestions
- Consider adding more detailed API endpoint documentation
- Add performance benchmarking targets

## Recommendation

**SPECIFICATION READY** - I have all the information I need.

The specification provides sufficient detail for code generation. All critical architectural
decisions are locked, preventing implementation variance across different teams.

---

## DIMENSION STATUS:
- Part A (Universal): 6/6 locked ✅
- Part B (Tech-Specific): 5/5 applicable, 5 locked ✅
- Part C (Project-Specific): 4 discovered dimensions, 4 locked ✅
- Part D (Micro-Level): 7/7 conventions locked ✅
- Part E (Feature Completeness): All features fully specified ✅

**SPECIFICATION READY - I have all the information I need** ✅

## Part F: Integration & Deployment Readiness

**Integration Readiness:** INTEGRATION_TESTS_READY

**Rationale:** SKIP_MODE exercises the full pipeline including QA loop, deployment
readiness validator, and all integration checkpoints. All external calls are mocked.

**Integration Details — Locked Values:**

| Dimension | Value |
|-----------|-------|
| Deploy method | GitHub Actions: .github/workflows/deploy-dev.yml |
| Target environment | docker-compose (mock) |
| Image registry | localhost:5000/mock-app |
| Base URL | http://localhost:3000 |
| Health check | /api/health |
| E2e framework | Playwright |
| E2e test location | e2e/ |
| Max QA rounds | 1 |

DIMENSION STATUS:
- Part F (Integration Readiness): INTEGRATION_TESTS_READY
"""
    
    completeness_file.write_text(content, encoding="utf-8")
    logger.info(f"[SKIP_MODE] Created mock specification_completeness.md at {completeness_file}")
    
    return str(completeness_file)


def create_mock_spec_completeness_result(
    outputs_dir: str,
    workspace_path: str,
    logger: Optional[logging.Logger] = None
) -> SpecificationCompletenessResult:
    """
    Create a complete mock SpecificationCompletenessResult with side effects.
    
    This function:
    1. Creates specification_completeness.md file
    2. Returns a SpecificationCompletenessResult object
    
    Args:
        outputs_dir: Directory where output files should be created
        workspace_path: Workspace root path (for resolving relative paths)
        logger: Optional logger instance
        
    Returns:
        SpecificationCompletenessResult with mock data
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    # Resolve outputs_dir relative to workspace if needed
    if not os.path.isabs(outputs_dir):
        outputs_path = Path(workspace_path) / outputs_dir
    else:
        outputs_path = Path(outputs_dir)
    
    # Generate mock completeness file
    completeness_file_path = create_mock_spec_completeness_file(
        outputs_dir=str(outputs_path),
        logger=logger
    )
    
    result = SpecificationCompletenessResult(
        readiness=SpecReadiness.INTEGRATION_TESTS_READY,
        summary="SPECIFICATION READY - I have all the information I need. All architectural dimensions are locked.",
        result_file_path=completeness_file_path,
        session_id=None,
    )
    
    logger.info(
        f"[SKIP_MODE] Generated mock spec completeness result: {result.readiness}, "
        f"file: {completeness_file_path}"
    )
    
    return result


def create_mock_spec_index_file(
    outputs_dir: str,
    logger: Optional[logging.Logger] = None
) -> str:
    """
    Generate a mock specification_index.md file for SKIP mode.

    Args:
        outputs_dir: Directory where the index file should be created
        logger: Optional logger instance

    Returns:
        Path to the created index file
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    index_file = Path(outputs_dir) / ANALYSIS_SUBDIR / "specification_index.md"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    
    content = """# Specification Index

**Generated**: Mock index (SKIP_MODE enabled)

## Overview

This is a mock specification index generated for testing purposes.
In SKIP_MODE, the agent execution is bypassed and this file is generated automatically.

## Specification Structure

### 1. Requirements Document
- **Location**: `specifications/requirements.md`
- **Purpose**: Core functional and non-functional requirements
- **Key Sections**: User stories, acceptance criteria, constraints

### 2. API Design
- **Location**: `specifications/api-design.md`
- **Purpose**: API endpoints, request/response formats
- **Key Sections**: REST endpoints, data models, authentication

### 3. Tech Stack
- **Location**: `specifications/tech-stack.md`
- **Purpose**: Technology choices and architecture
- **Key Sections**: Languages, frameworks, infrastructure, databases

### 4. Additional Documentation
- **Location**: `specifications/README.md`
- **Purpose**: Project overview and getting started guide

## Quick Reference

For detailed completeness analysis, see `specification_completeness.md`.

---

*Note: This is a mock index file generated in SKIP_MODE for testing purposes.*
"""
    
    index_file.write_text(content, encoding="utf-8")
    logger.info(f"[SKIP_MODE] Created mock specification_index.md at {index_file}")
    
    return str(index_file)


def create_initial_commit_and_push(
    workspace_path: str,
    logger: Optional[logging.Logger] = None
) -> Optional[str]:
    """
    Create an initial commit in SKIP_MODE.

    This ensures that each workspace has at least one commit with actual files
    (specifications that were copied during workspace preparation).
    No push is needed for SKIP_MODE P10Y bypass (mock generation).

    Args:
        workspace_path: Path to the workspace root
        logger: Optional logger instance

    Returns:
        Commit SHA of the created commit, or None if it fails
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    workspace = Path(workspace_path)
    
    if not workspace.exists():
        logger.error(f"[SKIP_MODE] Workspace does not exist: {workspace}")
        return None
    
    try:
        # Create a simple Python file to ensure work effort is recognized
        sample_python_file = workspace / "sample_code.py"
        python_code = '''class SomeClass:
    def some_function(self):
        import datetime
        x = datetime.datetime.now()
        if x:
            print("Something")
'''
        sample_python_file.write_text(python_code)
        logger.info(f"[SKIP_MODE] Created sample Python file at {sample_python_file}")
        
        # Stage all files (specifications should already be there)
        subprocess.run(
            ["git", "add", "."],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True
        )
        
        # Check if there's anything to commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True
        )
        
        if not status_result.stdout.strip():
            logger.warning(f"[SKIP_MODE] No changes to commit in {workspace}")
            # Try to get the current commit SHA if any
            try:
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    check=True
                )
                return sha_result.stdout.strip()
            except subprocess.CalledProcessError:
                return None
        
        commit_message = "SKIP_mode_seed_workspace"
        
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True
        )
        
        # Get the commit SHA
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True
        )
        commit_sha = sha_result.stdout.strip()
        
        logger.info(f"[SKIP_MODE] Created initial commit: {commit_sha}")
        return commit_sha
        
    except subprocess.CalledProcessError as e:
        logger.error(
            f"[SKIP_MODE] Failed to create commit in {workspace}: {e}\n"
            f"stdout: {e.stdout}\nstderr: {e.stderr}"
        )
        return None


def create_mock_workspace_estimation(
    workspace: "WorkspaceSettings",
    logger: Optional[logging.Logger] = None,
) -> WorkspaceEstimation:
    """
    Create a mock WorkspaceEstimation for SKIP_MODE P10Y bypass.

    Each workspace gets slightly different values so the risk model sees
    realistic variance across the workspace pool.

    Args:
        workspace: WorkspaceSettings for the workspace being estimated
        logger: Optional logger instance

    Returns:
        WorkspaceEstimation with mock data
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Derive a stable per-workspace offset from the last character of the name
    # (e.g. "ws-01-1" → 1, "ws-01-2" → 2, "ws-01-3" → 3)
    ws_name = workspace.name or ""
    try:
        ws_index = int(ws_name[-1])
    except (ValueError, IndexError):
        ws_index = 1

    # Base values that produce realistic-looking multi-workspace variance
    base_hours = 320.0
    variation = (ws_index - 1) * 24.0  # e.g. 0 / 24 / 48 across 3 workspaces

    total_hours = base_hours + variation
    new_work = 180.0 + variation * 0.6
    refactor = 60.0 + variation * 0.2
    rework = 20.0
    removed_work = 5.0
    quality_score = 0.78 + ws_index * 0.01
    effective_output = new_work + refactor
    total_output = new_work + refactor + rework + removed_work

    # Single component that covers the mock code
    component_breakdown: Dict[str, ComponentEstimation] = {
        "specifications": ComponentEstimation(
            component_name="specifications",
            hours=total_hours,
            new_work=new_work,
            refactor=refactor,
            rework=rework,
            quality_score=quality_score,
        )
    }

    estimation_metrics = EstimationMetrics(
        new_work=new_work,
        refactor=refactor,
        rework=rework,
        removed_work=removed_work,
        quality_score=quality_score,
        effective_output=effective_output,
        total_output=total_output,
    )

    logger.warning(
        f"[SKIP_MODE] Mock P10Y estimation for {workspace.name}: "
        f"{total_hours:.1f}h (new_work={new_work:.1f}, refactor={refactor:.1f})"
    )

    return WorkspaceEstimation(
        workspace_name=ws_name or "workspace",
        workspace_path=str(workspace.workspace_path),
        total_hours=total_hours,
        total_effective_output=effective_output,
        component_breakdown=component_breakdown,
        estimation_metrics=estimation_metrics,
        commits_count=1,
        p10y_scored_commits=1,
        model_usage=ModelTokenUsage(model_name=workspace.model or ""),
    )


def setup_skip_mode_workspace_commits(
    workspace_path: str,
    outputs_dir: str,
    logger: Optional[logging.Logger] = None
) -> bool:
    """
    Ensure SKIP_MODE workspace has a local git commit (SKIP_* seed).

    P10Y estimation is mocked in SKIP_MODE; no commit-metadata JSON file is used.

    Args:
        workspace_path: Path to the workspace root
        outputs_dir: Unused; kept for call-site compatibility
        logger: Optional logger instance

    Returns:
        True if successful, False otherwise
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    logger.info(f"[SKIP_MODE] Setting up workspace commits for {workspace_path}")

    commit_sha = create_initial_commit_and_push(workspace_path, logger)

    if not commit_sha:
        logger.error(f"[SKIP_MODE] Failed to create initial commit for {workspace_path}")
        return False

    logger.info(f"[SKIP_MODE] Successfully set up workspace commits for {workspace_path}")
    return True


