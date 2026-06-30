#!/usr/bin/env python3
"""
Migration 08: Backfill model_usage nested struct on estimation documents.

Adds a ``model_usage`` dict to every estimation that lacks one, seeding it
from the legacy flat fields ``num_turns`` / ``total_tokens_used``.  The
legacy total_tokens_used (input+output combined, historically) is placed in
``input_tokens``; the other token fields are zeroed since they cannot be
recovered for old runs.

Documents that already have ``model_usage`` are skipped (idempotent).

After this migration all documents carry ``model_usage`` and the application
code no longer needs to handle the old flat-field layout.

Run BEFORE deploying the code that reads/writes only ``model_usage``.

Usage:
    python migrate_08_model_usage.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate_estimations(client: firestore.AsyncClient, dry_run: bool = False) -> None:
    collection = client.collection("estimations")
    updated = 0
    skipped = 0

    async for doc_snap in collection.stream():
        doc = doc_snap.to_dict()

        if doc.get("model_usage"):
            skipped += 1
            continue

        # Build model_usage from legacy flat fields.
        # total_tokens_used was input+output combined; store as input_tokens
        # since we cannot separate them historically.
        model_usage = {
            "num_turns": int(doc.get("num_turns") or 0),
            "input_tokens": int(doc.get("total_tokens_used") or 0),
            "output_tokens": 0,
            "cache_write_tokens": 0,
            "cache_read_tokens": 0,
        }

        if dry_run:
            logger.info(
                "[DRY RUN] Would backfill model_usage on %s: %s",
                doc_snap.id,
                model_usage,
            )
        else:
            await doc_snap.reference.update({"model_usage": model_usage})
            logger.info("Backfilled model_usage on %s", doc_snap.id)

        updated += 1

    logger.info(
        "Done — updated: %d, already had model_usage (skipped): %d",
        updated,
        skipped,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log changes without writing")
    parser.add_argument("--project", default=None, help="GCP project ID")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate_estimations(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
