#!/usr/bin/env python3
"""
Migration 07: Add API key lock fields for per-key operation locking.

Fields added to all api_keys documents:
  - current_process: None (str | None — estimation_id of active operation)
  - in_progress: False (bool)
  - operation_started_at: None (datetime | None)
  - operation_ttl_minutes: None (int | None)

Safe to run multiple times (idempotent). Additive only.
Run BEFORE deploying the planning tool / generation endpoints.

Usage:
    python migrate_07_add_api_key_lock_fields.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY_FIELDS_TO_ADD = {
    "current_process": None,
    "in_progress": False,
    "operation_started_at": None,
    "operation_ttl_minutes": None,
}


async def migrate_api_keys(client: firestore.AsyncClient, dry_run: bool = False):
    collection = client.collection("api_keys")
    updated = 0
    skipped = 0

    async for doc_ref in collection.stream():
        doc = doc_ref.to_dict()
        update = {k: v for k, v in API_KEY_FIELDS_TO_ADD.items() if k not in doc}

        if update:
            if dry_run:
                logger.info("[DRY RUN] Would add %s to api_key %s", list(update.keys()), doc_ref.id)
            else:
                await doc_ref.reference.update(update)
                logger.info("Added %s to api_key %s", list(update.keys()), doc_ref.id)
            updated += 1
        else:
            skipped += 1

    logger.info("Done: %d updated, %d already have fields", updated, skipped)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--project", default=None, help="GCP project ID")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate_api_keys(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
