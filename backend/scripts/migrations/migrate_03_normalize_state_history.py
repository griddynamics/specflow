#!/usr/bin/env python3
"""
Migration 03: Normalize state_history entries in estimation documents.

Adds missing 'triggered_by' and 'metadata' fields to old entries.
Safe to run multiple times.

Usage:
    python migrate_03_normalize_state_history.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate_state_history(client: firestore.AsyncClient, dry_run: bool = False):
    collection = client.collection("estimations")
    updated = 0
    skipped = 0

    async for doc_ref in collection.stream():
        doc = doc_ref.to_dict()
        history = doc.get("state_history", [])
        if not history:
            skipped += 1
            continue

        normalized = []
        changed = False
        for entry in history:
            new_entry = dict(entry)
            if "triggered_by" not in new_entry:
                new_entry["triggered_by"] = "pre_refactor"
                changed = True
            if "metadata" not in new_entry:
                new_entry["metadata"] = {}
                changed = True
            normalized.append(new_entry)

        if changed:
            if dry_run:
                logger.info("[DRY RUN] Would normalize state_history for %s", doc_ref.id)
            else:
                await doc_ref.reference.update({"state_history": normalized})
                logger.info("Normalized state_history for %s", doc_ref.id)
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
    await migrate_state_history(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
