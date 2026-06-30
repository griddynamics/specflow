#!/usr/bin/env python3
"""
Migration 05: Remove deprecated fields after Phase 5 deployment.

DANGER: Run this ONLY after Phase 5 has been deployed for at least 1 week
and all deprecated code paths have been confirmed removed.

Fields removed:
  Estimation: lease_expires_at, last_heartbeat, instance_id
  Workspace:  locked_at, lease_expires_at (workspace-level)

Usage:
    python migrate_05_remove_deprecated_fields.py [--dry-run] [--project PROJECT_ID]

REQUIRES explicit --confirm flag (extra safety):
    python migrate_05_remove_deprecated_fields.py --confirm --project my-project
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ESTIMATION_FIELDS_TO_REMOVE = [
    "lease_expires_at",
    "last_heartbeat",
    "instance_id",
]

WORKSPACE_FIELDS_TO_REMOVE = [
    "locked_at",
    "lease_expires_at",
]


async def remove_deprecated_fields(
    client: firestore.AsyncClient, dry_run: bool = False
):
    # Remove from estimations
    est_collection = client.collection("estimations")
    est_updated = 0
    async for doc_ref in est_collection.stream():
        doc = doc_ref.to_dict()
        fields_present = [f for f in ESTIMATION_FIELDS_TO_REMOVE if f in doc]
        if fields_present:
            if dry_run:
                logger.info("[DRY RUN] Would remove %s from estimation %s",
                            fields_present, doc_ref.id)
            else:
                update = {f: firestore.DELETE_FIELD for f in fields_present}
                await doc_ref.reference.update(update)
                logger.info("Removed %s from estimation %s", fields_present, doc_ref.id)
            est_updated += 1

    # Remove from workspaces
    ws_collection = client.collection("workspaces")
    ws_updated = 0
    async for doc_ref in ws_collection.stream():
        doc = doc_ref.to_dict()
        fields_present = [f for f in WORKSPACE_FIELDS_TO_REMOVE if f in doc]
        if fields_present:
            if dry_run:
                logger.info("[DRY RUN] Would remove %s from workspace %s",
                            fields_present, doc_ref.id)
            else:
                update = {f: firestore.DELETE_FIELD for f in fields_present}
                await doc_ref.reference.update(update)
                logger.info("Removed %s from workspace %s", fields_present, doc_ref.id)
            ws_updated += 1

    logger.info(
        "Done: %d estimations updated, %d workspaces updated", est_updated, ws_updated
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", action="store_true",
                        help="Required flag to actually run (safety)")
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        print("ERROR: Pass --confirm to run this migration, or --dry-run to preview.")
        print("This migration removes fields from production data.")
        exit(1)

    client = firestore.AsyncClient(project=args.project)
    await remove_deprecated_fields(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
