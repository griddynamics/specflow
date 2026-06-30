#!/usr/bin/env python3
"""
One-time Firestore backfill: workspace_pool + key_uid on api_keys/workspaces/generation_sessions.

Run with production credentials after deploying index changes and before relying on
strict key_uid checks. Does not delete documents.

Usage:
  export GCP_PROJECT_ID=...
  export DATABASE_TYPE=firestore
  uv run python scripts/migrate_workspace_pool_key_uid.py [--dry-run]

See docs/backend/workspace-pool-segregation-plan.md (data migration section).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from app.state.db_adapter import COL_GENERATION_SESSIONS

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.factory import get_database
from app.schemas.permissions import Permission


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill workspace_pool and key_uid fields")
    parser.add_argument("--dry-run", action="store_true", help="Print actions only")
    args = parser.parse_args()

    db = get_database()

    def touch_api_keys() -> int:
        n = 0
        for doc in db.query("api_keys", []):
            wid = doc["_id"]
            updates: dict = {}
            if not doc.get("workspace_pool"):
                updates["workspace_pool"] = DEFAULT_WORKSPACE_POOL
            if not doc.get("key_uid"):
                updates["key_uid"] = str(uuid.uuid4())
            # Normalise permissions: replace missing/wildcard with ["user"]
            existing_perms = doc.get("permissions") or []
            if not existing_perms or set(existing_perms) - {Permission.USER.value, Permission.ADMIN.value}:
                updates["permissions"] = [Permission.USER.value]
            if updates:
                n += 1
                if args.dry_run:
                    print(f"api_keys/{wid} <- {updates}")
                else:
                    db.update("api_keys", wid, updates)
        return n

    def touch_workspaces() -> int:
        n = 0
        for doc in db.query("workspaces", []):
            wid = doc["_id"]
            if doc.get("workspace_pool"):
                continue
            n += 1
            if args.dry_run:
                print(f"workspaces/{wid} <- workspace_pool={DEFAULT_WORKSPACE_POOL!r}")
            else:
                db.update("workspaces", wid, {"workspace_pool": DEFAULT_WORKSPACE_POOL})
        return n

    def touch_generation_sessions() -> int:
        n = 0
        for doc in db.query(COL_GENERATION_SESSIONS, []):
            eid = doc["_id"]
            updates: dict = {}
            if not doc.get("workspace_pool"):
                updates["workspace_pool"] = DEFAULT_WORKSPACE_POOL
            if not doc.get("key_uid"):
                updates["key_uid"] = str(uuid.uuid4())
            if updates:
                n += 1
                if args.dry_run:
                    print(f"generation_sessions/{eid} <- {updates}")
                else:
                    db.update(COL_GENERATION_SESSIONS, eid, updates)
        return n

    ak = touch_api_keys()
    ws = touch_workspaces()
    est = touch_generation_sessions()
    print(
        f"Done ({'dry-run' if args.dry_run else 'applied'}): "
        f"api_keys={ak}, workspaces={ws}, generation_sessions={est}"
    )


if __name__ == "__main__":
    main()
