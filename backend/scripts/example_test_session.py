#!/usr/bin/env python3
"""
Seed one fake, already-COMPLETED generation session for local TUI testing.

Writes a generation_sessions document (with a full P10Y result — summary,
per-workspace breakdown, component comparison) plus the markdown/HTML reports
on disk, so `specflow tui` can render a finished run without waiting for a
real generation.

Requires the local sentinel identity to already exist (api_keys/local) —
run `specflow init` (or `python scripts/init_firestore.py`) at least once
before this script.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Set DATABASE_TYPE early if FIRESTORE_EMULATOR_HOST is set (before importing settings)
if os.getenv("FIRESTORE_EMULATOR_HOST") and not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "emulator"

from app.core.artifact_files import MULTI_WORKSPACE_REPORT_HTML_FILE, MULTI_WORKSPACE_REPORT_MD_FILE
from app.core.artifact_subdirs import REPORT_SUBDIR
from app.core.local_identity import LOCAL_API_KEY_DOC_ID
from app.core.notifications import render_generation_session_report_html
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.factory import get_database
from app.schemas.estimate import (
    ComparativeAnalysis,
    ComponentComparison,
    ComponentEstimation,
    EstimationMetrics,
    EstimationSummary,
    MultiWorkspaceEstimationResponse,
    RiskAssessment,
    WorkspaceEstimation,
)
from app.schemas.generation_workflow_enums import GenerationCheckpoint, GenerationStatus
from app.services.artifact_store import ARTIFACTS_BASE
from app.services.p10y.estimation_report_generator import format_multi_workspace_report
from app.state.db_adapter import COL_GENERATION_SESSIONS

# Matches tui.constants.LOCAL_ONLY_READINESS — kept as a literal since the TUI
# package isn't importable from the backend.
LOCAL_ONLY_READINESS = "LOCAL_ONLY"

COMPONENTS = [
    ("auth", 0.40, 6.0),
    ("billing", 0.35, 9.5),
    ("api", 0.25, 3.2),
]


def _workspace_estimation(workspace_name: str, total_hours: float) -> WorkspaceEstimation:
    component_breakdown = {
        name: ComponentEstimation(
            component_name=name,
            hours=round(total_hours * share, 1),
            new_work=round(total_hours * share * 0.7, 1),
            refactor=round(total_hours * share * 0.25, 1),
            rework=round(total_hours * share * 0.05, 1),
            quality_score=0.9,
        )
        for name, share, _variance in COMPONENTS
    }
    return WorkspaceEstimation(
        workspace_name=workspace_name,
        workspace_path=f"/workspaces/{workspace_name}",
        total_hours=total_hours,
        total_effective_output=round(total_hours * 0.9, 1),
        component_breakdown=component_breakdown,
        estimation_metrics=EstimationMetrics(
            new_work=round(total_hours * 0.7, 1),
            refactor=round(total_hours * 0.25, 1),
            rework=round(total_hours * 0.05, 1),
            removed_work=0.0,
            quality_score=0.9,
            effective_output=round(total_hours * 0.9, 1),
            total_output=total_hours,
        ),
        commits_count=12,
        total_usd_cost=4.25,
    )


def build_estimation_result(workspace_ids: list[str]) -> MultiWorkspaceEstimationResponse:
    """Fake P10Y result with a real component breakdown, built from the actual schemas."""
    hours_by_ws = [110.0 + 5.0 * i for i in range(len(workspace_ids))]
    workspace_estimations = [
        _workspace_estimation(ws_id, hours) for ws_id, hours in zip(workspace_ids, hours_by_ws)
    ]

    average_hours = sum(hours_by_ws) / len(hours_by_ws)
    summary = EstimationSummary(
        average_hours=average_hours,
        std_deviation=3.3,
        min_hours=min(hours_by_ws),
        max_hours=max(hours_by_ws),
        coefficient_of_variation=0.03,
        variance_assessment="low",
        risk_assessment=RiskAssessment(
            status="Approved",
            instability_ratio=0.03,
            rejection_threshold=0.15,
            base_component=0.10,
            var_component=0.02,
            size_component=0.01,
            total_buffer_pct=0.13,
            final_estimate=average_hours * 1.13,
        ),
    )

    component_comparison = {
        name: ComponentComparison(
            component_name=name,
            hours_by_workspace={
                ws.workspace_name: ws.component_breakdown[name].hours for ws in workspace_estimations
            },
            average=sum(ws.component_breakdown[name].hours for ws in workspace_estimations)
            / len(workspace_estimations),
            std_deviation=2.1,
            variance_percentage=variance,
        )
        for name, _share, variance in COMPONENTS
    }
    comparative_analysis = ComparativeAnalysis(
        component_comparison=component_comparison,
        high_variance_components=["billing"],
        insights=[
            "Low variance across workspaces",
            "Billing has the highest variance — consider a closer look",
        ],
    )

    return MultiWorkspaceEstimationResponse(
        summary=summary,
        workspace_estimations=workspace_estimations,
        comparative_analysis=comparative_analysis,
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_usd_cost=sum(ws.total_usd_cost for ws in workspace_estimations),
    )


def write_reports(generation_id: str, result: MultiWorkspaceEstimationResponse, spec_path: str) -> Path:
    """Write the markdown + HTML reports where the report.html endpoint expects to find them."""
    report_dir = ARTIFACTS_BASE / generation_id / REPORT_SUBDIR
    report_dir.mkdir(parents=True, exist_ok=True)

    markdown = format_multi_workspace_report(
        workspace_estimations=result.workspace_estimations,
        summary=result.summary,
        comparative_analysis=result.comparative_analysis,
        skipped_workspaces=result.skipped_workspaces,
        aggregate_p10y_commit_coverage_pct=result.aggregate_p10y_commit_coverage_pct,
    )
    (report_dir / MULTI_WORKSPACE_REPORT_MD_FILE).write_text(markdown)

    html_content, _plain = render_generation_session_report_html(
        generation_id=generation_id,
        workspace_ids=[ws.workspace_name for ws in result.workspace_estimations],
        result=result,
        spec_path=spec_path,
        db=None,
    )
    (report_dir / MULTI_WORKSPACE_REPORT_HTML_FILE).write_text(html_content)

    return report_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generation-id",
        default="est-example0001",
        help="Generation ID to seed (default: est-example0001). Overwrites if it already exists.",
    )
    parser.add_argument(
        "--spec-path",
        default="specs/example.md",
        help="Fake spec path stored on the session (cosmetic only).",
    )
    args = parser.parse_args()

    db = get_database()

    sentinel = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
    if not sentinel:
        print(
            f"ERROR: local sentinel identity (api_keys/{LOCAL_API_KEY_DOC_ID}) not found.\n"
            "Run `specflow init` (or `python scripts/init_firestore.py`) at least once first."
        )
        return 1
    user_email = (sentinel.get("user_id") or "").lower()
    key_uid = sentinel.get("key_uid")
    workspace_pool = sentinel.get("workspace_pool") or DEFAULT_WORKSPACE_POOL

    workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
    result = build_estimation_result(workspace_ids)
    report_dir = write_reports(args.generation_id, result, args.spec_path)

    now = datetime.now(timezone.utc)
    workspace_phases = {
        ws_id: {"last_completed_phase": 9, "total_phases": 9, "phase_name": "Done"}
        for ws_id in workspace_ids
    }

    db.set(
        COL_GENERATION_SESSIONS,
        args.generation_id,
        {
            "generation_id": args.generation_id,
            "user_email": user_email,
            "key_uid": key_uid,
            "workspace_pool": workspace_pool,
            "status": GenerationStatus.COMPLETED.value,
            "checkpoint": GenerationCheckpoint.ESTIMATION_DONE.value,
            "created_at": now,
            "started_at": now,
            "completed_at": now,
            "workspace_ids": workspace_ids,
            "parameters": {
                "workspace_count": len(workspace_ids),
                "spec_path": args.spec_path,
                "outputs_dir": "docs",
            },
            "last_spec_readiness": LOCAL_ONLY_READINESS,
            "workspace_phases": workspace_phases,
            "progress": {},
            "result": result.model_dump(),
            "artifact_path": str(ARTIFACTS_BASE / args.generation_id),
            "code_archived": True,
            "retry_count": 0,
            "max_retries": 3,
            "error": None,
        },
    )

    print(f"Seeded completed generation session: {args.generation_id}")
    print(f"Reports written to: {report_dir}")
    print(f"\nOn your host machine, run:\n\n  specflow tui --generation-id {args.generation_id}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
