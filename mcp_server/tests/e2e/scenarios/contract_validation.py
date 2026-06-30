"""Contract-validation E2E scenario.

Drives `run_generation` against a live stack with a series of deliberately-broken
contracts and asserts each is rejected **before any workspace is allocated** with the
correct structured `code` — the guarantee PR304 ("don't allocate on contract fail")
exists to provide. A final positive control proves the gate lets a valid contract
through (so the suite is not just always-rejecting).

Covers both rejection layers (CLAUDE.md two-layer gate), which must return the same
error shape:
  - MCP-side precheck (no upload): ANALYSIS_MISSING, PLAN_MISSING, E2E_PLAN_MISSING
  - Backend pre-allocation preflight (after upload): PLAN_NO_PHASES, PLAN_UNPARSEABLE

For every rejection it asserts: structured `code`, no `generation_id`, no
`specflow_session.json` written, and the workspace pool's allocated count is unchanged.

Usage (normally via `make contract-validation-e2e-tests`, which runs it in SKIP mode):
    WORKSPACE_COUNT=3 uv run python -m tests.e2e.scenarios.contract_validation
"""

import os
import shutil
from pathlib import Path

import httpx

from tests.e2e.mcp_client import call_tool, mcp_session
from tests.e2e.runner import (
    assert_no_error,
    poll_until,
    resolve_api_credentials,
    run_scenario,
)

EXAMPLE_SPECS_SRC = Path("/tmp/specflow-e2e-specs")

POLL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 5.0

_VALID_ANALYSIS = """\
# Specification Completeness Analysis

## Part F: Integration & Deployment Readiness

**Integration Readiness:** LOCAL_ONLY
"""

_INTEGRATION_READY_ANALYSIS = """\
# Specification Completeness Analysis

## Part F: Integration & Deployment Readiness

**Integration Readiness:** INTEGRATION_TESTS_READY
"""

_VALID_PLAN = """\
# Implementation Plan

## Phase 1: Bootstrap
**Agent MCPs**: none

- Initialise the project skeleton.
- Wire up the no-op entrypoint.
"""

_PLAN_NO_PHASES = "# Implementation Plan\n\nWe will figure out the phases later.\n"
_PLAN_EMPTY_PHASE = "# Implementation Plan\n\n## Phase 1: Bootstrap\n\n## Phase 2: More\n"


def _write_baseline(docs: Path) -> None:
    """(Re)write a valid LOCAL_ONLY contract: analysis + implementation plan."""
    if docs.exists():
        shutil.rmtree(docs)
    (docs / "analysis").mkdir(parents=True)
    (docs / "planning").mkdir(parents=True)
    (docs / "analysis" / "specification_completeness.md").write_text(_VALID_ANALYSIS)
    (docs / "planning" / "IMPLEMENTATION_PLAN.md").write_text(_VALID_PLAN)


# --- mutations: each takes the docs dir and breaks the contract one way ------ #


def _drop_analysis(docs: Path) -> None:
    (docs / "analysis" / "specification_completeness.md").unlink()


def _drop_plan(docs: Path) -> None:
    (docs / "planning" / "IMPLEMENTATION_PLAN.md").unlink()


def _plan_no_phases(docs: Path) -> None:
    (docs / "planning" / "IMPLEMENTATION_PLAN.md").write_text(_PLAN_NO_PHASES)


def _plan_empty_phase(docs: Path) -> None:
    (docs / "planning" / "IMPLEMENTATION_PLAN.md").write_text(_PLAN_EMPTY_PHASE)


def _integration_ready_no_e2e(docs: Path) -> None:
    (docs / "analysis" / "specification_completeness.md").write_text(_INTEGRATION_READY_ANALYSIS)
    # No e2e-test-plan.md written → E2E_PLAN_MISSING.


# (label, expected_code, mutate_fn)
_CASES = [
    ("missing analysis", "ANALYSIS_MISSING", _drop_analysis),
    ("missing plan", "PLAN_MISSING", _drop_plan),
    ("plan with no phases", "PLAN_NO_PHASES", _plan_no_phases),
    ("plan with empty phase", "PLAN_UNPARSEABLE", _plan_empty_phase),
    ("integration-ready without e2e plan", "E2E_PLAN_MISSING", _integration_ready_no_e2e),
]


def _backend_headers(api_key: str, user_email: str) -> dict:
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    if user_email:
        headers["X-User-Email"] = user_email
    return headers


async def _allocated_count(backend_url: str, headers: dict) -> int:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{backend_url}/api/v1/workspace/pool/status", headers=headers)
        resp.raise_for_status()
        return int(resp.json().get("allocated", 0))


async def _run(workspace_count: int, tmp_dir: Path) -> None:
    if not EXAMPLE_SPECS_SRC.exists():
        raise RuntimeError(
            f"Example specs not found at {EXAMPLE_SPECS_SRC}. Run 'make e2e-setup' first."
        )
    shutil.copytree(EXAMPLE_SPECS_SRC, tmp_dir / "specs")
    docs_path = tmp_dir / "docs"
    spec_dir = str(tmp_dir / "specs")
    outputs_dir = str(docs_path)
    session_file = tmp_dir / "specflow_session.json"

    api_key, user_email = resolve_api_credentials()
    backend_url = os.getenv("BACKEND_URL", "http://localhost:8000")
    headers = _backend_headers(api_key, user_email)

    async with mcp_session(
        workspace_count=workspace_count,
        extra_env={"SPECFLOW_API_KEY": api_key, "USER_EMAIL": user_email},
    ) as session:
        for i, (label, expected_code, mutate) in enumerate(_CASES, start=1):
            _write_baseline(docs_path)
            mutate(docs_path)

            allocated_before = await _allocated_count(backend_url, headers)
            result = await call_tool(session, "run_generation", {
                "spec_dir": spec_dir,
                "outputs_dir": outputs_dir,
            })

            assert result.get("code") == expected_code, (
                f"[{label}] expected code={expected_code!r}, got code={result.get('code')!r}. "
                f"Full response: {result}"
            )
            assert not result.get("generation_id"), (
                f"[{label}] a rejection must not return a generation_id: {result}"
            )
            assert not session_file.exists(), (
                f"[{label}] specflow_session.json must NOT be written on rejection"
            )
            allocated_after = await _allocated_count(backend_url, headers)
            assert allocated_after == allocated_before, (
                f"[{label}] rejection allocated workspaces! "
                f"allocated {allocated_before} → {allocated_after}"
            )
            print(f"  {i}/{len(_CASES)} {label}: rejected as {expected_code}, no allocation ✓")

        # Positive control: a valid contract must pass the gate and allocate.
        print("Positive control: valid contract must be accepted")
        _write_baseline(docs_path)
        allocated_before = await _allocated_count(backend_url, headers)
        result = await call_tool(session, "run_generation", {
            "spec_dir": spec_dir,
            "outputs_dir": outputs_dir,
        })
        assert_no_error(result, "run_generation (positive control)")
        generation_id = result.get("generation_id", "")
        assert generation_id, f"valid contract returned no generation_id: {result}"
        allocated_after = await _allocated_count(backend_url, headers)
        assert allocated_after > allocated_before, (
            f"valid contract did not allocate: allocated {allocated_before} → {allocated_after}"
        )
        print(f"  accepted: generation_id={generation_id}, allocated {allocated_before} → {allocated_after} ✓")

        # Drain to completion (SKIP mode finishes fast) so workspaces are released.
        status_data = await poll_until(
            session, generation_id,
            lambda d: d.get("status") in ("completed", "failed"),
            timeout_s=POLL_TIMEOUT_S, interval_s=POLL_INTERVAL_S,
            label="positive-control",
        )
        assert status_data.get("status") == "completed", (
            f"positive control did not complete: {status_data}"
        )
        print("  positive control completed ✓")

    print("\n✅ All contract-validation E2E assertions passed.")


def main() -> None:
    run_scenario("specflow-e2e-contract-", _run)


if __name__ == "__main__":
    main()
