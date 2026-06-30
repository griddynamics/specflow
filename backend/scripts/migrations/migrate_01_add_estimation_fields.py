#!/usr/bin/env python3
"""
Migration 01: Add new estimation fields required by the state management refactor.

Safe to run multiple times (idempotent).
Run BEFORE deploying Phase 2.

Usage:
    python migrate_01_add_estimation_fields.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


ESTIMATION_DEFAULTS = {
    "last_activity_at": None,
    "failed_at": None,
    "outputs_archived": False,
    "artifact_path": None,
    "code_archived": False,
    "archive_status": {},
}


async def migrate_estimations(client: firestore.AsyncClient, dry_run: bool = False):
    collection = client.collection("estimations")
    docs = collection.stream()
    updated = 0
    skipped = 0

    async for doc_ref in docs:
        doc = doc_ref.to_dict()
        update = {}

        for field, default in ESTIMATION_DEFAULTS.items():
            if field not in doc:
                update[field] = default

        # Special case: if status=FAILED and failed_at is missing,
        # backfill from status_changed_at
        if (doc.get("status") == "failed"
                and "failed_at" not in doc
                and "status_changed_at" in doc):
            update["failed_at"] = doc["status_changed_at"]
            logger.info(
                "Backfilling failed_at from status_changed_at for %s", doc_ref.id
            )

        if update:
            if dry_run:
                logger.info("[DRY RUN] Would update %s: %s", doc_ref.id, list(update.keys()))
            else:
                await doc_ref.reference.update(update)
                logger.info("Updated %s: added fields %s", doc_ref.id, list(update.keys()))
            updated += 1
        else:
            skipped += 1

    logger.info("Done: %d updated, %d already up to date", updated, skipped)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--project", default=None,
                        help="GCP project ID (default: from environment)")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate_estimations(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
