#!/usr/bin/env python3
"""
Migration 04: Backfill last_activity_at for RUNNING estimations.

Finds RUNNING estimations without last_activity_at and sets it from the
most recent checkpoint entry in state_history. This prevents Job 1 from
immediately flagging all RUNNING estimations as stuck after deploy.

Run BEFORE deploying Phase 3 (before the stuck_running_detector goes live).

Usage:
    python migrate_04_backfill_last_activity_at.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def backfill_last_activity_at(client: firestore.AsyncClient, dry_run: bool = False):
    collection = client.collection("estimations")
    updated = 0
    skipped = 0

    query = collection.where("status", "==", "running").where(
        "last_activity_at", "==", None
    )

    async for doc_ref in query.stream():
        doc = doc_ref.to_dict()
        history = doc.get("state_history", [])

        # Find most recent entry with a checkpoint
        checkpoint_entries = [
            e for e in history if e.get("checkpoint")
        ]
        if checkpoint_entries:
            latest = max(checkpoint_entries, key=lambda e: e.get("at", 0))
            last_activity = latest.get("at")
        else:
            # Fall back to status_changed_at
            last_activity = doc.get("status_changed_at")

        if last_activity:
            if dry_run:
                logger.info(
                    "[DRY RUN] Would set last_activity_at=%s for %s",
                    last_activity, doc_ref.id
                )
            else:
                await doc_ref.reference.update({"last_activity_at": last_activity})
                logger.info(
                    "Set last_activity_at=%s for %s", last_activity, doc_ref.id
                )
            updated += 1
        else:
            logger.warning(
                "Cannot backfill last_activity_at for %s: no timestamp found", doc_ref.id
            )
            skipped += 1

    logger.info("Done: %d updated, %d could not be backfilled", updated, skipped)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await backfill_last_activity_at(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
