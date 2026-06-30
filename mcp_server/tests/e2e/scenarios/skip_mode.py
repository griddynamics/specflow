"""
Skip-mode E2E scenario.

Runs the new `run_generation` flow against a locally running stack with
SKIP_AGENT_EXECUTION=true.  Agents return immediately — the test validates
tool wiring, contract validation, session-file behaviour, status progression,
and artifact layout.

The new flow:
  1. The user produces `docs/analysis/specification_completeness.md` and
     `docs/planning/IMPLEMENTATION_PLAN.md` locally (normally via the
     `check_specification_completeness` and `run_planning` MCP tools in an IDE).
     This scenario writes those files directly to mimic that work.
  2. `run_generation` uploads everything, the backend contract validator
     normalizes filenames + converts the markdown plans to JSON, then the
     generation workflow runs (no-op under SKIP_AGENT_EXECUTION).
  3. The user downloads the archived outputs.

Usage:
    WORKSPACE_COUNT=3 uv run python -m tests.e2e.scenarios.skip_mode
"""

import json
import shutil
from pathlib import Path

from tests.e2e.mcp_client import call_tool, mcp_session
from schemas.gain_json import SpecFlow_JSON_FILENAME
from tests.e2e.runner import (
    PLAN_MARKER,
    assert_no_error,
    poll_until,
    prepend_plan_marker,
    resolve_api_credentials,
    run_scenario,
)

EXAMPLE_SPECS_SRC = Path("/tmp/specflow-e2e-specs")

POLL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 5.0


# Minimal valid contract files. Part F = LOCAL_ONLY so the e2e plan is not required.
# IMPLEMENTATION_PLAN.md must have at least one phase heading so the conversion agent
# (in SKIP_MODE, which writes a mock JSON with one phase) produces a non-empty plan.
_ANALYSIS_CONTENT = """\
# Specification Completeness Analysis

## Critical Issues
None.

## Part F: Integration & Deployment Readiness

**Integration Readiness:** LOCAL_ONLY

**Rationale:** No deployment automation in scope for this E2E run.
"""

_PLAN_CONTENT = """\
# Implementation Plan

## Architectural Decisions - Locked Values
| Dimension | Value |
|-----------|-------|
| A1. Data Persistence | None (in-memory) |

## Phase 1: Bootstrap
**Agent MCPs**: none

- Initialise project skeleton.
- Wire up the no-op entrypoint.
"""


def _write_local_artifacts(outputs_dir: Path) -> Path:
    """Write the analysis + implementation plan a user would have produced locally."""
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

    if not EXAMPLE_SPECS_SRC.exists():
        raise RuntimeError(
            f"Example specs not found at {EXAMPLE_SPECS_SRC}. "
            "Run 'make e2e-setup' first."
        )
    shutil.copytree(EXAMPLE_SPECS_SRC, tmp_dir / "specs")

    plan_file = _write_local_artifacts(docs_path)
    print(f"Wrote local artifacts under {docs_path}")

    api_key, user_email = resolve_api_credentials()

    async with mcp_session(
        workspace_count=workspace_count,
        extra_env={
            "SPECFLOW_API_KEY": api_key,
            "USER_EMAIL": user_email,
        },
    ) as session:
        print("Step 1/5: prepend marker to local plan (will round-trip through generation)")
        prepend_plan_marker(plan_file)
        print("  marker written ✓")

        print("Step 2/5: run_generation (precheck + upload + validate + generate)")
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
        print("  specflow_session.json: ✓")

        specflow_json_path = tmp_dir / SpecFlow_JSON_FILENAME
        assert specflow_json_path.exists(), f"gain.json not created at {specflow_json_path}"
        specflow_data = json.loads(specflow_json_path.read_text())
        for required in ("description", "servicesDescription", "codingAgents"):
            assert specflow_data.get(required) is not None, (
                f"gain.json missing {required!r}: {specflow_data}"
            )
        versions = specflow_data.get("versions") or {}
        assert versions.get("specflow"), f"gain.json versions.specflow is empty: {specflow_data}"
        print("  gain.json: ✓")

        print("Step 3/5: poll until generation completes")
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

        print("Step 4/5: download outputs (post-generation)")
        result = await call_tool(session, "download_outputs", {
            "generation_id": generation_id,
            "outputs_dir": outputs_dir,
        })
        assert_no_error(result, "download_outputs")
        files_extracted = result.get("files_extracted", 0)
        assert files_extracted > 0, f"No files extracted after generation: {result}"
        print(f"  files extracted: {files_extracted}")

        # Archive extracts flat: analysis/, report/, ws-*/ all directly under docs_path
        top_dirs = [p for p in docs_path.iterdir() if p.is_dir()]
        dir_names = [p.name for p in top_dirs]
        print(f"  archive dirs: {dir_names}")
        workspace_dirs = [p for p in top_dirs if p.name not in ("analysis", "report", "planning")]
        assert len(workspace_dirs) == workspace_count, (
            f"Expected {workspace_count} workspace dirs, found {len(workspace_dirs)}: {dir_names}"
        )
        print(f"  workspace dirs ({workspace_count}): ✓")

        print("Step 5/5: verify user-edited plan survived round-trip")
        post_gen_plans = list(docs_path.glob("**/IMPLEMENTATION_PLAN.md"))
        if post_gen_plans:
            first_line = post_gen_plans[0].read_text(encoding="utf-8").splitlines()[0]
            assert first_line == PLAN_MARKER, (
                f"Plan marker not preserved after generation. First line: {first_line!r}"
            )
            print("  plan marker survived generation ✓")

    print("\n✅ All skip-mode E2E assertions passed.")
    print(f"   generation_id: {generation_id}")
    print(f"   workspace_count: {workspace_count}")
    print(f"   artifacts: {docs_path}")


def main() -> None:
    run_scenario("specflow-e2e-skip-", _run)


if __name__ == "__main__":
    main()
