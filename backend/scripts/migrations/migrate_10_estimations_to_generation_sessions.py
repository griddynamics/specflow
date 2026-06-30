#!/usr/bin/env python3
"""
Migration 10: Move Firestore collection ``estimations`` → ``generation_sessions``
and rename document field ``estimation_id`` → ``generation_id``.

Run AFTER creating the Firestore composite indexes for ``generation_sessions``
(``backend/scripts/create_firestore_indexes.py`` is the SSOT — run it with
``--prod`` and wait for indexes to reach READY before switching traffic).

Run this script with the new backend offline or before switching traffic.
Deployed code reads only ``generation_sessions`` (no fallback to ``estimations``).

Idempotent: documents already present in ``generation_sessions`` are skipped.
Source documents in ``estimations`` are deleted only after a successful write.

Usage:
    export GOOGLE_CLOUD_PROJECT=your-project
    python backend/scripts/migrations/migrate_10_estimations_to_generation_sessions.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from google.cloud import firestore

from app.state.db_adapter import COL_GENERATION_SESSIONS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COL_ESTIMATIONS = "estimations"


async def migrate(client: firestore.AsyncClient, *, dry_run: bool) -> None:
    src = client.collection(COL_ESTIMATIONS)
    dst = client.collection(COL_GENERATION_SESSIONS)

    migrated = skipped = 0
    async for snap in src.stream():
        existing = await dst.document(snap.id).get()
        if existing.exists:
            logger.info("Skip %s — already in generation_sessions", snap.id)
            skipped += 1
            continue

        data = dict(snap.to_dict() or {})
        if "estimation_id" in data:
            data["generation_id"] = data.pop("estimation_id")
        elif "generation_id" not in data:
            data["generation_id"] = snap.id

        if dry_run:
            logger.info("[DRY RUN] would migrate estimations/%s → generation_sessions/%s", snap.id, snap.id)
        else:
            await dst.document(snap.id).set(data, merge=False)
            await snap.reference.delete()
            logger.info("Migrated estimations/%s → generation_sessions/%s", snap.id, snap.id)
        migrated += 1

    logger.info(
        "Done: %d document(s) %s, %d skipped (already present)",
        migrated,
        "would be migrated (dry-run)" if dry_run else "migrated",
        skipped,
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
