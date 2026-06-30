"""
Real-run E2E scenario.

Runs the new `run_generation` flow with real agent execution (no SKIP_MODE).
Uses a committed fixture spec (tests/e2e/fixtures/simple-task-app/) so the
project is small, deterministic, and generates in ~30 min.

The new flow has no backend analysis/planning agents — users produce
`docs/analysis/specification_completeness.md` and
`docs/planning/IMPLEMENTATION_PLAN.md` locally before calling `run_generation`.
This scenario writes those files itself (a real IDE user would use the
`check_specification_completeness` and `run_planning` tools).

Usage:
    WORKSPACE_COUNT=3 SPECFLOW_API_KEY=... BACKEND_URL=... \\
        uv run python -m tests.e2e.scenarios.real_run
"""

import shutil
from pathlib import Path

from tests.e2e.mcp_client import REPO_ROOT, call_tool, mcp_session
from tests.e2e.runner import (
    PLAN_MARKER,
    assert_no_error,
    poll_until,
    prepend_plan_marker,
    resolve_api_credentials,
    run_scenario,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "e2e" / "fixtures" / "simple-task-app" / "specs"

POLL_TIMEOUT_S = 90 * 60.0
POLL_INTERVAL_S = 15.0


# Real-run uses LOCAL_ONLY so the e2e plan is not required (deploy/E2E is exercised
# separately by integration tests). The plan only needs one valid phase so the
# conversion agent has structure to extract — real codegen agents will write code
# during the generation phase regardless.
_ANALYSIS_CONTENT = """\
# Specification Completeness Analysis

## Critical Issues
None — fixture spec is intentionally minimal.

## Part F: Integration & Deployment Readiness

**Integration Readiness:** LOCAL_ONLY

**Rationale:** Real-run E2E exercises codegen only; deployment is out of scope for this test.
"""

_PLAN_CONTENT = """\
# Implementation Plan

## Architectural Decisions - Locked Values
| Dimension | Locked Value |
|-----------|--------------|
| A1. Data Persistence | In-memory store |
| A4. Technology Stack | Python 3.13 |
| A5. Quality & Testing | MVP — critical path tests only |

## Phase 1: Project skeleton
**Agent MCPs**: none

- Scaffold the project structure following the spec.
- Add a placeholder entrypoint and README.

## Phase 2: Core feature
**Agent MCPs**: none

- Implement the primary user-facing feature described in `specs/`.
- Cover it with at least one unit test.
"""


def _write_local_artifacts(outputs_dir: Path) -> Path:
    analysis_dir = outputs_dir / "analysis"
    planning_dir = outputs_dir / "planning"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)

    (analysis_dir / "specification_completeness.md").write_text(_ANALYSIS_CONTENT, encoding="utf-8")
    plan_path = planning_dir / "IMPLEMENTATION_PLAN.md"
    plan_path.write_text(_PLAN_CONTENT, encoding="utf-8")
    return plan_path


async def _run(workspace_count: int, tmp_dir: Path) -> None:
    docs_path = tmp_dir / "docs"
    spec_dir = str(tmp_dir / "specs")
    outputs_dir = str(docs_path)

    if not FIXTURE_DIR.exists():
        raise RuntimeError(f"Fixture specs not found at {FIXTURE_DIR}")
    shutil.copytree(FIXTURE_DIR, tmp_dir / "specs")

    plan_file = _write_local_artifacts(docs_path)
    prepend_plan_marker(plan_file)
    print(f"Wrote local artifacts under {docs_path} and prepended plan marker")

    api_key, user_email = resolve_api_credentials()

    async with mcp_session(
        workspace_count=workspace_count,
        extra_env={
            "SPECFLOW_API_KEY": api_key,
            "USER_EMAIL": user_email,
        },
    ) as session:
        print("Step 1/3: run_generation (precheck + upload + validate + generate)")
        result = await call_tool(session, "run_generation", {
            "spec_dir": spec_dir,
            "outputs_dir": outputs_dir,
        })
        assert_no_error(result, "run_generation")
        generation_id: str = result.get("generation_id", "")
        assert generation_id, f"No generation_id returned: {result}"
        print(f"  generation_id: {generation_id}")

        session_file = tmp_dir / "specflow_session.json"
        assert session_file.exists(), f"specflow_session.json not created at {session_file}"

        print("Step 2/3: poll until generation completes")
        status_data = await poll_until(
            session, generation_id,
            lambda d: d.get("status") in ("completed", "failed"),
            timeout_s=POLL_TIMEOUT_S, interval_s=POLL_INTERVAL_S,
            label="generation",
        )
        final_status = status_data.get("status")
        assert final_status == "completed", (
            f"Expected status=completed, got {final_status!r}.\n{status_data}"
        )
        print(f"  status: {final_status} ✓")

        print("Step 3/3: download outputs and verify code was produced")
        result = await call_tool(session, "download_outputs", {
            "generation_id": generation_id,
            "outputs_dir": outputs_dir,
        })
        assert_no_error(result, "download_outputs")
        files_extracted = result.get("files_extracted", 0)
        assert files_extracted > 0, f"No files extracted after generation: {result}"

        est_dir = docs_path / generation_id
        assert est_dir.exists(), f"Expected {est_dir} in extracted archive"

        all_dirs = [p for p in est_dir.iterdir() if p.is_dir()]
        workspace_dirs = [p for p in all_dirs if p.name not in ("analysis", "report")]
        assert len(workspace_dirs) == workspace_count, (
            f"Expected {workspace_count} workspace dirs, found {len(workspace_dirs)}: "
            + str([p.name for p in workspace_dirs])
        )
        print(f"  workspace dirs ({workspace_count}): ✓")

        for ws_dir in workspace_dirs:
            src_files = (
                list(ws_dir.rglob("*.ts"))
                + list(ws_dir.rglob("*.tsx"))
                + list(ws_dir.rglob("*.js"))
                + list(ws_dir.rglob("*.py"))
            )
            assert src_files, (
                f"Workspace dir {ws_dir.name} has no source files (.ts/.tsx/.js/.py)"
            )
        print("  workspace source files: ✓")

        post_gen_plans = list(docs_path.glob("**/IMPLEMENTATION_PLAN.md"))
        if post_gen_plans:
            first_line = post_gen_plans[0].read_text(encoding="utf-8").splitlines()[0]
            assert first_line == PLAN_MARKER, (
                f"Plan marker not preserved after generation. First line: {first_line!r}"
            )
            print("  plan marker survived generation ✓")

    print("\n✅ All real-run E2E assertions passed.")
    print(f"   generation_id: {generation_id}")
    print(f"   workspace_count: {workspace_count}")
    print(f"   artifacts: {est_dir}")


def main() -> None:
    run_scenario("specflow-e2e-real-", _run)


if __name__ == "__main__":
    main()
