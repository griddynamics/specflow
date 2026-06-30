#!/usr/bin/env python3
"""
Migration 12: Align generation_sessions documents with the local-analysis workflow.

Renames stored status and checkpoint fields to the values defined in
``generation_workflow_enums`` (e.g. ``files_uploaded``, ``contract_validated``).

Idempotent: only patches documents whose fields differ from the target values.

Usage:
    export GOOGLE_CLOUD_PROJECT=your-project
    python backend/scripts/migrations/migrate_12_generation_session_workflow.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from google.cloud import firestore

from app.state.db_adapter import COL_GENERATION_SESSIONS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Previous persisted values → current ``GenerationCheckpoint`` / ``GenerationStatus`` strings.
CHECKPOINT_RENAMES: dict[str, str] = {
    "uploaded_specs": "files_uploaded",
    "spec_check_done": "contract_validated",
    "planning_done": "contract_validated",
    "plan_synced": "kb_init_done",
    "plan_reparsed": "kb_init_done",
    "synced_specs": "kb_init_done",
    "synced_code": "kb_init_done",
    "baseline_committed": "kb_init_done",
}

STATUS_RENAMES: dict[str, str] = {
    "analysis": "pending",
}


def _migrate_doc(data: dict) -> dict | None:
    """Return fields to update, or None if the document already matches."""
    updates: dict = {}

    raw_status = data.get("status")
    if isinstance(raw_status, str) and raw_status in STATUS_RENAMES:
        updates["status"] = STATUS_RENAMES[raw_status]
        if raw_status == "analysis" and not data.get("error"):
            updates["error"] = (
                "Session reset to pending — spec analysis now runs locally in the IDE."
            )

    raw_cp = data.get("checkpoint")
    if isinstance(raw_cp, str) and raw_cp in CHECKPOINT_RENAMES:
        updates["checkpoint"] = CHECKPOINT_RENAMES[raw_cp]

    return updates or None


async def migrate(client: firestore.AsyncClient, *, dry_run: bool) -> None:
    collection = client.collection(COL_GENERATION_SESSIONS)
    updated = scanned = 0

    async for snap in collection.stream():
        scanned += 1
        data = snap.to_dict() or {}
        patch = _migrate_doc(data)
        if patch is None:
            continue
        if dry_run:
            logger.info("[DRY RUN] would patch %s: %s", snap.id, patch)
        else:
            await snap.reference.update(patch)
            logger.info("Patched %s: %s", snap.id, patch)
        updated += 1

    logger.info(
        "Done: scanned %d, %s %d document(s)",
        scanned,
        "would update" if dry_run else "updated",
        updated,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", default=None, help="GCP project id (default: client default)")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
