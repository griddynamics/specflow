#!/usr/bin/env python3
"""
Migration 02: Add new workspace fields required by the state management refactor.

Safe to run multiple times (idempotent).
Run BEFORE deploying Phase 2 (before WorkspaceStateMachine is wired in).

Usage:
    python migrate_02_add_workspace_fields.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


WORKSPACE_DEFAULTS = {
    "cleaning_started_at": None,
    "scheduled_for_wipe": False,
    "scheduled_for_wipe_at": None,
}


async def migrate_workspaces(client: firestore.AsyncClient, dry_run: bool = False):
    collection = client.collection("workspaces")
    docs = collection.stream()
    updated = 0
    skipped = 0

    async for doc_ref in docs:
        doc = doc_ref.to_dict()
        update = {}

        for field, default in WORKSPACE_DEFAULTS.items():
            if field not in doc:
                update[field] = default

        # Special case: if status=CLEANING and cleaning_started_at is missing,
        # backfill from locked_at or current timestamp as sentinel
        if (doc.get("status") == "cleaning"
                and "cleaning_started_at" not in doc
                and doc.get("locked_at")):
            # Use locked_at as a conservative estimate
            update["cleaning_started_at"] = doc["locked_at"]
            logger.info(
                "Backfilling cleaning_started_at from locked_at for %s", doc_ref.id
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate_workspaces(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
