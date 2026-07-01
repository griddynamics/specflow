#!/usr/bin/env python3
"""
Fix stuck workspaces using the new state management system.

This script:
1. Finds stuck workspaces (ALLOCATED, STUCK, or AVAILABLE with stale data)
2. Provides two modes:
   - Safe mode (default): Transitions workspaces to CLEANING state, letting background jobs handle cleanup
   - Force mode: Immediately cleans filesystem, Firestore state, and resets git main branch

Usage:
    # Dry run - safe mode (default)
    uv run python backend/scripts/fix_stuck_workspaces.py --dry-run
    
    # Dry run - force mode (immediate cleanup)
    uv run python backend/scripts/fix_stuck_workspaces.py --dry-run --force
    
    # Safe fix (let background jobs clean)
    uv run python backend/scripts/fix_stuck_workspaces.py
    
    # Force fix (immediate cleanup)
    uv run python backend/scripts/fix_stuck_workspaces.py --force
    
    # Fix specific workspaces only
    uv run python backend/scripts/fix_stuck_workspaces.py --workspace-ids ws-01-1 ws-01-2
    
    # Skip specific workspaces
    uv run python backend/scripts/fix_stuck_workspaces.py --skip ws-01-1,ws-01-2
    
    # Force fix with skip
    uv run python backend/scripts/fix_stuck_workspaces.py --force --skip ws-01-1,ws-01-2
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Set

from dotenv import load_dotenv

# Add backend to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# Load .env file explicitly from project root
dotenv_path = PROJECT_ROOT / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

# Set DATABASE_TYPE early: emulator auto-detect takes priority (manually-run emulator),
# otherwise default to sqlite (the local/Docker-dev default) rather than the ephemeral
# in-memory fallback, which would make this script find an always-empty pool.
if os.getenv("FIRESTORE_EMULATOR_HOST") and not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "emulator"
elif not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "sqlite"

from app.database.factory import get_database  # noqa: E402
from app.services.workspace_pool import WorkspacePoolService  # noqa: E402
from app.state import WorkspaceStateMachine  # noqa: E402
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter  # noqa: E402
from app.schemas.generation_workflow_enums import WorkspaceStatus, GenerationStatus  # noqa: E402
from app.jobs.scheduled_wipe import FAILED_RETENTION_DAYS, WIPE_WARNING_HOURS  # noqa: E402


def format_datetime(dt) -> str:
    """Format datetime for display."""
    if dt is None:
        return "N/A"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def normalize_status(status) -> str:
    """Normalize status to enum value string for comparison."""
    if status is None:
        return None
    if isinstance(status, WorkspaceStatus):
        return status.value
    return str(status)


def analyze_workspace(ws_doc: Dict[str, Any], db, workspace_pool: WorkspacePoolService) -> Dict[str, Any]:
    """
    Analyze a workspace to determine if it's stuck and what action to take.
    
    Returns:
        Dictionary with analysis results:
        - is_stuck: bool - Whether workspace is stuck
        - reason: str - Why it's stuck
        - safe_to_fix: bool - Whether it's safe to fix
        - action: str - Action to take ("force_release", "admin_deallocate", "begin_recovery", "admin_release_stuck", "admin_clean_available")
        - generation_status: str - Status of the generation (if any)
        - has_stale_data: bool - Whether workspace has stale data on disk (for AVAILABLE workspaces)
    """
    ws_id = ws_doc["_id"]
    status_str = normalize_status(ws_doc.get("status"))
    locked_by = ws_doc.get("locked_by")
    last_used_by = ws_doc.get("last_used_by")
    
    result = {
        "workspace_id": ws_id,
        "status": status_str,
        "locked_by": locked_by,
        "last_used_by": last_used_by,
        "is_stuck": False,
        "reason": None,
        "safe_to_fix": False,
        "action": None,
        "generation_status": None,
        "has_stale_data": False,
    }
    
    # Handle STUCK workspaces
    if status_str == WorkspaceStatus.STUCK.value:
        result["is_stuck"] = True
        result["safe_to_fix"] = True
        result["action"] = "begin_recovery"  # Will transition STUCK → CLEANING
        result["reason"] = f"Stuck state (error: {ws_doc.get('error', 'unknown')[:100]})"
        return result
    
    # Handle CLEANING workspaces
    if status_str == WorkspaceStatus.CLEANING.value:
        # Already in cleaning - background jobs will handle, but we can still force clean
        result["is_stuck"] = True
        result["safe_to_fix"] = True
        result["action"] = "cleanup"  # Already CLEANING, just need to run cleanup
        result["reason"] = f"Stuck in CLEANING state since {format_datetime(ws_doc.get('cleaning_started_at'))}"
        return result
    
    # Handle ALLOCATED workspaces
    if status_str == WorkspaceStatus.ALLOCATED.value:
        # Check if generation exists and is in terminal state
        if locked_by:
            est_doc = db.get(COL_GENERATION_SESSIONS, locked_by)
            
            if not est_doc:
                result["is_stuck"] = True
                result["reason"] = "Generation not found in database"
                result["safe_to_fix"] = True
                result["action"] = "force_release"  # ALLOCATED → CLEANING
                result["generation_status"] = "NOT_FOUND"
            else:
                est_status = est_doc.get("status")
                result["generation_status"] = est_status
                
                # Safe to release if generation is in terminal state
                if est_status in [GenerationStatus.COMPLETED, GenerationStatus.FAILED]:
                    result["is_stuck"] = True
                    result["reason"] = f"Generation in terminal state: {est_status}"
                    result["safe_to_fix"] = True
                    result["action"] = "force_release"  # ALLOCATED → CLEANING
                    result["failed_at"] = est_doc.get("failed_at")
                elif est_status == GenerationStatus.PENDING:
                    # PENDING but workspace is ALLOCATED - shouldn't happen, but safe to release
                    result["is_stuck"] = True
                    result["reason"] = "Generation is PENDING but workspace is ALLOCATED (orphaned)"
                    result["safe_to_fix"] = True
                    result["action"] = "force_release"  # ALLOCATED → CLEANING
                else:
                    # RUNNING or INITIALIZING - check if truly stuck
                    # For now, we'll be conservative and only fix if explicitly requested
                    # Background jobs handle these cases
                    result["is_stuck"] = False
                    result["reason"] = f"Generation is {est_status} - background jobs will handle if stuck"
        else:
            # No locked_by but status is ALLOCATED - definitely stuck
            result["is_stuck"] = True
            result["reason"] = "Allocated but not locked by any generation (orphaned)"
            result["safe_to_fix"] = True
            result["action"] = "force_release"  # ALLOCATED → CLEANING
    
    # Handle AVAILABLE workspaces - check for stale data
    elif status_str == WorkspaceStatus.AVAILABLE.value:
        # Check disk state for stale data
        # Note: This is async, so we'll check it later in the main function
        result["is_stuck"] = False  # Will be set to True if stale data found
        result["action"] = "admin_clean_available"  # AVAILABLE → CLEANING
        result["reason"] = "Checking for stale data..."
    
    return result


async def check_available_workspace_stale_data(
    workspace_id: str,
    workspace_pool: WorkspacePoolService
) -> Dict[str, Any]:
    """Check if an AVAILABLE workspace has stale data on disk."""
    try:
        disk_state = await workspace_pool.check_workspace_disk_state(workspace_id)
        return {
            "has_stale_data": not disk_state.get("is_clean", False),
            "issues": disk_state.get("issues", []),
            "has_commits_on_main": disk_state.get("has_commits_on_main", False),
            "has_uncommitted_changes": disk_state.get("has_uncommitted_changes", False),
            "has_estimation_artifacts": disk_state.get("has_estimation_artifacts", False),
        }
    except Exception as e:
        return {
            "has_stale_data": True,  # Assume stale if check fails
            "issues": [f"Failed to check disk state: {e}"],
            "has_commits_on_main": False,
            "has_uncommitted_changes": False,
            "has_estimation_artifacts": False,
        }


async def safe_fix_workspace(
    analysis: Dict[str, Any],
    wsm: WorkspaceStateMachine,
    workspace_pool: WorkspacePoolService,
    reason: str,
    confirmed_by: str = "fix_stuck_workspaces_script",
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Safe fix: Transition workspace to CLEANING state, let background jobs handle cleanup.
    
    Returns:
        Dictionary with result: {"success": bool, "message": str, "error": str | None}
    """
    ws_id = analysis["workspace_id"]
    action = analysis["action"]
    
    if dry_run:
        return {
            "success": True,
            "message": f"Would transition {ws_id} to CLEANING via {action}",
            "error": None,
        }
    
    try:
        if action == "force_release":
            # ALLOCATED → CLEANING
            await wsm.force_release(
                workspace_id=ws_id,
                reason=reason,
                confirmed_by=confirmed_by,
            )
            return {
                "success": True,
                "message": f"{ws_id}: Force-released to CLEANING (background jobs will clean)",
                "error": None,
            }
        
        elif action == "begin_recovery":
            # STUCK → CLEANING
            await wsm.begin_recovery(
                workspace_id=ws_id,
                triggered_by="admin:fix_stuck_workspaces_script",
            )
            return {
                "success": True,
                "message": f"{ws_id}: Began recovery STUCK → CLEANING (background jobs will clean)",
                "error": None,
            }
        
        elif action == "admin_clean_available":
            # AVAILABLE → CLEANING
            await wsm.admin_clean_available(
                workspace_id=ws_id,
                reason=reason,
                triggered_by="admin:fix_stuck_workspaces_script",
            )
            return {
                "success": True,
                "message": f"{ws_id}: Transitioned AVAILABLE → CLEANING (background jobs will clean)",
                "error": None,
            }
        
        elif action == "cleanup":
            # Already CLEANING - background jobs will handle
            return {
                "success": True,
                "message": f"{ws_id}: Already CLEANING (background jobs will clean)",
                "error": None,
            }
        
        else:
            return {
                "success": False,
                "message": f"{ws_id}: Unknown action {action}",
                "error": f"Unknown action: {action}",
            }
    
    except Exception as e:
        return {
            "success": False,
            "message": f"{ws_id}: Failed",
            "error": str(e),
        }


async def force_fix_workspace(
    analysis: Dict[str, Any],
    wsm: WorkspaceStateMachine,
    workspace_pool: WorkspacePoolService,
    reason: str,
    confirmed_by: str = "fix_stuck_workspaces_script",
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Force fix: Transition workspace to CLEANING AND immediately clean filesystem/git.
    
    Returns:
        Dictionary with result: {"success": bool, "message": str, "error": str | None}
    """
    ws_id = analysis["workspace_id"]
    action = analysis["action"]
    
    if dry_run:
        return {
            "success": True,
            "message": f"Would force-fix {ws_id}: transition to CLEANING via {action}, then cleanup filesystem/git",
            "error": None,
        }
    
    try:
        # First, transition to CLEANING if not already there
        if action == "force_release":
            # ALLOCATED → CLEANING
            await wsm.force_release(
                workspace_id=ws_id,
                reason=reason,
                confirmed_by=confirmed_by,
            )
        elif action == "begin_recovery":
            # STUCK → CLEANING
            await wsm.begin_recovery(
                workspace_id=ws_id,
                triggered_by="admin:fix_stuck_workspaces_script",
            )
        elif action == "admin_clean_available":
            # AVAILABLE → CLEANING
            await wsm.admin_clean_available(
                workspace_id=ws_id,
                reason=reason,
                triggered_by="admin:fix_stuck_workspaces_script",
            )
        elif action == "cleanup":
            # Already CLEANING, skip transition
            pass
        else:
            return {
                "success": False,
                "message": f"{ws_id}: Unknown action {action}",
                "error": f"Unknown action: {action}",
            }
        
        # Now clean the workspace (filesystem + git reset)
        await workspace_pool.cleanup_workspace(ws_id)
        
        return {
            "success": True,
            "message": f"{ws_id}: Force-fixed (transitioned to CLEANING, cleaned filesystem, reset git main)",
            "error": None,
        }
    
    except Exception as e:
        return {
            "success": False,
            "message": f"{ws_id}: Failed",
            "error": str(e),
        }


async def main():
    """Main script execution."""
    parser = argparse.ArgumentParser(
        description="Fix stuck workspaces using the new state management system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run - safe mode (default)
  %(prog)s --dry-run
  
  # Dry run - force mode
  %(prog)s --dry-run --force
  
  # Safe fix (let background jobs clean)
  %(prog)s
  
  # Force fix (immediate cleanup)
  %(prog)s --force
  
  # Fix specific workspaces
  %(prog)s --workspace-ids ws-01-1 ws-01-2
  
  # Skip specific workspaces
  %(prog)s --skip ws-01-1,ws-01-2
        """
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fixed without actually fixing anything"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force mode: immediately clean filesystem, Firestore state, and reset git main branch. "
             "Default is safe mode: transition to CLEANING and let background jobs handle cleanup."
    )
    parser.add_argument(
        "--workspace-ids",
        nargs="+",
        help="Specific workspace IDs to check/fix (default: check all workspaces)"
    )
    parser.add_argument(
        "--skip",
        type=str,
        help="Comma-separated list of workspace IDs to skip (e.g., ws-01-1,ws-01-2)"
    )
    parser.add_argument(
        "--reason",
        type=str,
        default="fix_stuck_workspaces_script",
        help="Reason to record in audit trail (default: fix_stuck_workspaces_script)"
    )
    parser.add_argument(
        "--confirmed-by",
        type=str,
        default="fix_stuck_workspaces_script",
        help="Name/email of operator authorizing the fix (default: fix_stuck_workspaces_script)"
    )
    parser.add_argument(
        "--check-available",
        action="store_true",
        help="Also check AVAILABLE workspaces for stale data (slower, checks filesystem)"
    )
    
    args = parser.parse_args()
    
    # Parse skip list
    skip_set: Set[str] = set()
    if args.skip:
        skip_set = {ws_id.strip() for ws_id in args.skip.split(",")}
    
    print("=" * 80)
    print("🔧 Stuck Workspace Recovery Tool")
    print("=" * 80)
    print(f"Mode: {'FORCE' if args.force else 'SAFE'} (--force to change)")
    if args.dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
    if skip_set:
        print(f"⏭️  Skipping {len(skip_set)} workspace(s): {', '.join(sorted(skip_set))}")
    print()
    
    try:
        db = get_database()
        raw_db = db
        db_adapter = StateMachineDBAdapter(raw_db)
        workspace_pool = WorkspacePoolService(raw_db)
        wsm = WorkspaceStateMachine(db_adapter)
        
        # Get workspaces to check
        if args.workspace_ids:
            print(f"Checking specific workspaces: {args.workspace_ids}")
            workspaces = []
            for ws_id in args.workspace_ids:
                ws_doc = db.get("workspaces", ws_id)
                if ws_doc:
                    workspaces.append(ws_doc)
                else:
                    print(f"⚠️  Workspace {ws_id} not found in database")
        else:
            print("Checking all workspaces...")
            # Get ALLOCATED, STUCK, and CLEANING workspaces
            allocated_workspaces = db.query(
                collection="workspaces",
                filters=[("status", "==", WorkspaceStatus.ALLOCATED.value)]
            )
            stuck_workspaces = db.query(
                collection="workspaces",
                filters=[("status", "==", WorkspaceStatus.STUCK.value)]
            )
            cleaning_workspaces = db.query(
                collection="workspaces",
                filters=[("status", "==", WorkspaceStatus.CLEANING.value)]
            )
            workspaces = list(allocated_workspaces) + list(stuck_workspaces) + list(cleaning_workspaces)
            
            # Optionally check AVAILABLE workspaces for stale data
            if args.check_available:
                print("  Also checking AVAILABLE workspaces for stale data...")
                available_workspaces = db.query(
                    collection="workspaces",
                    filters=[("status", "==", WorkspaceStatus.AVAILABLE.value)]
                )
                workspaces.extend(list(available_workspaces))
        
        # Filter out skipped workspaces
        workspaces = [ws for ws in workspaces if ws["_id"] not in skip_set]
        
        print(f"Found {len(workspaces)} workspace(s) to analyze")
        print()
        
        # Analyze each workspace
        stuck_workspaces = []
        safe_to_fix = []
        
        for ws in workspaces:
            analysis = analyze_workspace(ws, db, workspace_pool)
            
            # For AVAILABLE workspaces, check for stale data
            if analysis["status"] == WorkspaceStatus.AVAILABLE.value:
                stale_data = await check_available_workspace_stale_data(
                    analysis["workspace_id"],
                    workspace_pool
                )
                if stale_data["has_stale_data"]:
                    analysis["is_stuck"] = True
                    analysis["safe_to_fix"] = True
                    analysis["has_stale_data"] = True
                    analysis["reason"] = f"Has stale data: {', '.join(stale_data['issues'])}"
                    analysis.update(stale_data)
            
            if analysis["is_stuck"]:
                stuck_workspaces.append(analysis)
                if analysis["safe_to_fix"]:
                    safe_to_fix.append(analysis)
        
        # Print results
        if not stuck_workspaces:
            print("✅ No stuck workspaces found!")
            return
        
        print(f"🚨 Found {len(stuck_workspaces)} stuck workspace(s):")
        print()
        
        for analysis in stuck_workspaces:
            ws_id = analysis["workspace_id"]
            reason = analysis["reason"]
            action = analysis.get("action", "unknown")
            safe = "✅ SAFE" if analysis["safe_to_fix"] else "⚠️  NOT SAFE"
            
            print(f"Workspace: {ws_id} - {safe}")
            print(f"  Status: {analysis['status']}")
            print(f"  Action: {action}")
            if analysis.get("generation_status"):
                print(f"  Generation status: {analysis['generation_status']}")
            if analysis.get("locked_by"):
                print(f"  Locked by: {analysis['locked_by']}")
            if analysis.get("has_stale_data"):
                print(f"  Stale data issues: {', '.join(analysis.get('issues', []))}")
            print(f"  Reason: {reason}")
            failed_at = analysis.get("failed_at")
            if isinstance(failed_at, datetime):
                if failed_at.tzinfo is None:
                    failed_at = failed_at.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                days_since = (now - failed_at).total_seconds() / 86400
                seconds_until_eligible = (failed_at + timedelta(days=FAILED_RETENTION_DAYS) - now).total_seconds()
                if seconds_until_eligible > 0:
                    h = seconds_until_eligible / 3600
                    print(f"  Failed: {days_since:.2f} days ago — auto-wipe in ~{h + WIPE_WARNING_HOURS:.1f}h ({h:.1f}h to schedule + {WIPE_WARNING_HOURS}h warning)")
                else:
                    print(f"  Failed: {days_since:.2f} days ago — eligible now (pending next daily job + {WIPE_WARNING_HOURS}h warning)")
            print()
        
        # Fix safe workspaces
        if not safe_to_fix:
            print("⚠️  No workspaces are safe to automatically fix")
            print("   Manual intervention may be required")
            return
        
        print(f"💡 {len(safe_to_fix)} workspace(s) can be safely fixed:")
        print(f"   Mode: {'FORCE (immediate cleanup)' if args.force else 'SAFE (background jobs will clean)'}")
        print()
        
        if args.dry_run:
            print("🔍 DRY RUN - Would fix:")
            for analysis in safe_to_fix:
                mode_desc = "force-fix" if args.force else "safe-fix"
                print(f"  - {analysis['workspace_id']}: {mode_desc} - {analysis['reason']}")
            print()
            print("Run without --dry-run to actually fix these workspaces")
        else:
            print("🔧 Fixing stuck workspaces...")
            print()
            
            success_count = 0
            fail_count = 0
            
            for analysis in safe_to_fix:
                ws_id = analysis["workspace_id"]
                reason = f"{args.reason}: {analysis['reason']}"
                
                if args.force:
                    result = await force_fix_workspace(
                        analysis, wsm, workspace_pool, reason,
                        confirmed_by=args.confirmed_by, dry_run=False
                    )
                else:
                    result = await safe_fix_workspace(
                        analysis, wsm, workspace_pool, reason,
                        confirmed_by=args.confirmed_by, dry_run=False
                    )
                
                if result["success"]:
                    print(f"  ✅ {result['message']}")
                    success_count += 1
                else:
                    print(f"  ❌ {result['message']}: {result['error']}")
                    fail_count += 1
            
            print()
            print("✅ Fix complete!")
            print(f"   Success: {success_count}, Failed: {fail_count}")
            print()
            if not args.force:
                print("Note: Workspaces were transitioned to CLEANING state.")
                print("Background jobs will clean them within ~2.5 hours.")
            else:
                print("Note: Workspaces were force-cleaned (filesystem + git reset).")
            print()
            print("Next steps:")
            print("  1. Check workspace pool status: GET /api/v1/workspace/pool/status")
            print("  2. Verify workspaces are now AVAILABLE")
    
    except Exception as e:
        print(f"\n❌ Script failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
