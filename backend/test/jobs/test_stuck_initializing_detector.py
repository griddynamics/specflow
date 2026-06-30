"""
Tests for stuck_initializing_detector.py (Job 2).
"""
import pytest
from datetime import datetime, timedelta, timezone

from app.jobs.stuck_initializing_detector import detect_stuck_initializing
from app.schemas.generation_workflow_enums import GenerationStatus, GenerationCheckpoint, WorkspaceStatus


@pytest.mark.asyncio
async def test_stale_initializing_rolled_back_to_pending(db):
    """Stale INITIALIZING generation → allocation_failed → PENDING."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    db.seed_generation_session("est-init", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-init")
    assert est["status"] == GenerationStatus.PENDING


@pytest.mark.asyncio
async def test_fresh_initializing_not_touched(db):
    """Fresh INITIALIZING generation → not touched."""
    now = datetime.now(timezone.utc)
    fresh_time = now - timedelta(minutes=5)

    db.seed_generation_session("est-fresh", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": fresh_time,
        "state_history": [],
    })

    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-fresh")
    assert est["status"] == GenerationStatus.INITIALIZING


@pytest.mark.asyncio
async def test_running_generation_not_touched(db):
    """RUNNING generation (wrong status) → not touched."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    db.seed_generation_session("est-running", {
        "status": GenerationStatus.RUNNING,
        "status_changed_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-running")
    assert est["status"] == GenerationStatus.RUNNING


@pytest.mark.asyncio
async def test_state_history_entry_added(db):
    """allocation_failed appends a state_history entry."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    db.seed_generation_session("est-hist", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-hist")
    history = est.get("state_history", [])
    assert len(history) >= 1
    last = history[-1]
    assert last["triggered_by"] == "job:stuck_initializing_detector"


@pytest.mark.asyncio
async def test_multiple_stale_initializing_all_rolled_back(db):
    """Multiple stale INITIALIZING generations → all rolled back to PENDING."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    for i in range(3):
        db.seed_generation_session(f"est-{i}", {
            "status": GenerationStatus.INITIALIZING,
            "status_changed_at": stale_time,
            "state_history": [],
        })

    await detect_stuck_initializing(db, threshold_minutes=15)

    for i in range(3):
        est = db.get_generation_session_data(f"est-{i}")
        assert est["status"] == GenerationStatus.PENDING


@pytest.mark.asyncio
async def test_already_pending_after_concurrent_recovery(db):
    """If another process already rolled back → state machine rejects, job skips gracefully."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    # Seed as PENDING (already recovered by another process)
    db.seed_generation_session("est-concurrent", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": stale_time,
        "state_history": [],
    })

    # Should not raise — state machine rejects the transition, job skips
    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-concurrent")
    assert est["status"] == GenerationStatus.PENDING  # Unchanged


# ================================================================== #
# Issue 4 fix — workspace rollback after stuck INITIALIZING           #
# ================================================================== #

@pytest.mark.asyncio
async def test_allocated_workspaces_rolled_back_to_cleaning(db):
    """
    Issue 4 fix: ALLOCATED workspaces are rolled back to CLEANING after
    detect_stuck_initializing() fires on an generation with workspace_ids.
    Without this fix, they would stay ALLOCATED forever (pool drain).
    """
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)
    workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

    db.seed_generation_session("est-init-ws", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": stale_time,
        "workspace_ids": workspace_ids,
        "state_history": [],
    })
    for ws_id in workspace_ids:
        db.seed_workspace(ws_id, {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-init-ws",
        })

    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-init-ws")
    assert est["status"] == GenerationStatus.PENDING

    for ws_id in workspace_ids:
        ws = db.get_workspace_data(ws_id)
        assert ws["status"] == WorkspaceStatus.CLEANING, (
            f"Workspace {ws_id} should be CLEANING but is {ws['status']}"
        )
        assert ws["locked_by"] is None


@pytest.mark.asyncio
async def test_no_workspace_rollback_when_no_workspace_ids(db):
    """Generation with empty workspace_ids → generation rolls back, no workspace calls."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)

    db.seed_generation_session("est-no-ws", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": stale_time,
        "workspace_ids": [],
        "state_history": [],
    })

    # No workspaces seeded — should not raise
    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-no-ws")
    assert est["status"] == GenerationStatus.PENDING


@pytest.mark.asyncio
async def test_workspace_rollback_continues_after_individual_failure(db):
    """
    If one workspace rollback fails (e.g. wrong locked_by), the loop
    continues and rolls back the other workspaces.
    """
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=30)
    workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

    db.seed_generation_session("est-partial", {
        "status": GenerationStatus.INITIALIZING,
        "status_changed_at": stale_time,
        "workspace_ids": workspace_ids,
        "state_history": [],
    })

    # ws-01-1 has wrong locked_by → allocation_rollback will raise WorkspaceOwnershipError
    db.seed_workspace("ws-01-1", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "different-generation",  # Wrong owner → rollback fails
    })
    db.seed_workspace("ws-01-2", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-partial",
    })
    db.seed_workspace("ws-01-3", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-partial",
    })

    # Should not raise — individual failure is logged and skipped
    await detect_stuck_initializing(db, threshold_minutes=15)

    est = db.get_generation_session_data("est-partial")
    assert est["status"] == GenerationStatus.PENDING

    # ws-01-1 failed rollback → stays ALLOCATED (wrong owner)
    ws1 = db.get_workspace_data("ws-01-1")
    assert ws1["status"] == WorkspaceStatus.ALLOCATED

    # ws-01-2 and ws-01-3 succeeded → CLEANING
    ws2 = db.get_workspace_data("ws-01-2")
    assert ws2["status"] == WorkspaceStatus.CLEANING

    ws3 = db.get_workspace_data("ws-01-3")
    assert ws3["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_workspace_rollback_skipped_when_generation_already_recovered(db):
    """
    If the generation was already in PENDING (concurrent recovery), Part A
    skips via `continue` so its workspace rollback does NOT run. However,
    Part B detects these as orphaned and rolls them back.
    """
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-skip-ws", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": stale_time,
        "workspace_ids": ["ws-01-1"],
        "state_history": [],
    })
    db.seed_workspace("ws-01-1", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-skip-ws",
        "allocated_at": stale_time,
    })

    await detect_stuck_initializing(db, threshold_minutes=15)

    # Part B recovers orphaned PENDING+ALLOCATED workspaces
    ws = db.get_workspace_data("ws-01-1")
    assert ws["status"] == WorkspaceStatus.CLEANING


# ================================================================== #
# Part B — Orphaned PENDING workspaces with ALLOCATED workspaces      #
# ================================================================== #

@pytest.mark.asyncio
async def test_orphaned_pending_allocated_workspace_cleaned(db):
    """
    Scenario: Generation is PENDING but workspace is still ALLOCATED (orphaned).
    This happens when allocation_failed succeeded but allocation_rollback failed.
    Part B detects and rolls back the workspace.
    
    STEEL COMMANDMENT: Safe because PENDING = no code generation ever ran.
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=2)

    db.seed_generation_session("est-orphan", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": old_time,
        "state_history": [],
    })
    db.seed_workspace("ws-orphan-1", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-orphan",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-orphan-1")
    assert ws["status"] == WorkspaceStatus.CLEANING
    assert ws["locked_by"] is None


@pytest.mark.asyncio
async def test_orphaned_pending_recent_not_touched(db):
    """
    Scenario: Workspace just allocated, generation still PENDING.
    Grace period protects against racing with normal allocation flow.
    """
    now = datetime.now(timezone.utc)
    recent_time = now - timedelta(minutes=5)

    db.seed_generation_session("est-recent", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": recent_time,
        "state_history": [],
    })
    db.seed_workspace("ws-recent", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-recent",
        "allocated_at": recent_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-recent")
    assert ws["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_orphaned_failed_generation_not_touched(db):
    """
    Scenario: Generation is FAILED with ALLOCATED workspace.
    Part B only handles PENDING, NOT FAILED. FAILED workspaces are preserved
    per STEEL COMMANDMENT II and handled by scheduled_wipe (7-day retention).
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=2)

    db.seed_generation_session("est-failed", {
        "status": GenerationStatus.FAILED,
        "status_changed_at": old_time,
        "failed_at": old_time,
        "state_history": [],
    })
    db.seed_workspace("ws-failed", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-failed",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-failed")
    assert ws["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_orphaned_running_generation_not_touched(db):
    """
    Scenario: Generation is RUNNING with ALLOCATED workspace.
    This is normal operation — Part B must NOT touch these.
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=2)

    db.seed_generation_session("est-running", {
        "status": GenerationStatus.RUNNING,
        "status_changed_at": old_time,
        "state_history": [],
    })
    db.seed_workspace("ws-running", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-running",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-running")
    assert ws["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_pending_with_kb_init_checkpoint_not_touched(db):
    """
    Scenario: PENDING generation has progressed to KB_INIT_DONE checkpoint.
    Workspaces are in use — should NOT be cleaned.
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=3)

    db.seed_generation_session("est-kb-init", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": old_time,
        "checkpoint": GenerationCheckpoint.KB_INIT_DONE,
        "state_history": [],
    })
    db.seed_workspace("ws-kb-init", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-kb-init",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-kb-init")
    assert ws["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_pending_with_planning_checkpoint_not_touched(db):
    """
    Scenario: PENDING generation has progressed to PLANNING_DONE checkpoint.
    Workspaces are in use — should NOT be cleaned.
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=4)

    db.seed_generation_session("est-planning", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": old_time,
        "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
        "state_history": [],
    })
    db.seed_workspace("ws-planning", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-planning",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    ws = db.get_workspace_data("ws-planning")
    assert ws["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_pending_pre_spec_check_cleaned_up(db):
    """
    Scenario: PENDING generation with NO checkpoint (or only UPLOADED_SPECS).
    This is truly orphaned (never progressed past initial upload).
    Workspace should be cleaned up after threshold.
    """
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=2)

    db.seed_generation_session("est-pre-spec", {
        "status": GenerationStatus.PENDING,
        "status_changed_at": old_time,
        # No checkpoint or only UPLOADED_SPECS
        "checkpoint": GenerationCheckpoint.FILES_UPLOADED,
        "state_history": [],
    })
    db.seed_workspace("ws-pre-spec", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-pre-spec",
        "allocated_at": old_time,
    })

    await detect_stuck_initializing(db)

    # Should be cleaned up — no progress past initial upload
    ws = db.get_workspace_data("ws-pre-spec")
    assert ws["status"] == WorkspaceStatus.CLEANING
