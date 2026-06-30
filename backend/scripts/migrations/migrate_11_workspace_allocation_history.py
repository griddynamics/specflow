#!/usr/bin/env python3
"""
Migration 11: Rename ``estimation_id`` → ``generation_id`` inside
``allocation_history`` entries on workspace documents.

Background: prior to the estimation→generation rename, each entry appended to
``workspaces/{id}.allocation_history`` used the key ``estimation_id``.  The
new code writes ``generation_id``.  This migration back-fills old entries so
the field name is consistent across the array.

Idempotent: entries that already have ``generation_id`` are left untouched.
Documents whose ``allocation_history`` array requires no changes are skipped
(no write issued).

Run AFTER migrate_10 (estimations → generation_sessions).

Usage:
    export GOOGLE_CLOUD_PROJECT=your-project
    python backend/scripts/migrations/migrate_11_workspace_allocation_history.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from google.cloud import firestore

from app.state.db_adapter import COL_WORKSPACES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _migrate_history(history: list) -> tuple[list, int]:
    """Return (updated_history, number_of_entries_changed)."""
    updated = []
    changed = 0
    for entry in history:
        if "estimation_id" in entry:
            entry = dict(entry)
            entry["generation_id"] = entry.pop("estimation_id")
            changed += 1
        updated.append(entry)
    return updated, changed


async def migrate(client: firestore.AsyncClient, *, dry_run: bool) -> None:
    col = client.collection(COL_WORKSPACES)
    migrated_docs = skipped_docs = migrated_entries = 0

    async for snap in col.stream():
        data = snap.to_dict() or {}
        history = data.get("allocation_history")
        if not history:
            skipped_docs += 1
            continue

        new_history, changed = _migrate_history(history)
        if changed == 0:
            skipped_docs += 1
            continue

        if dry_run:
            logger.info(
                "[DRY RUN] workspaces/%s — would update %d allocation_history entr%s",
                snap.id, changed, "y" if changed == 1 else "ies",
            )
        else:
            await snap.reference.update({"allocation_history": new_history})
            logger.info(
                "workspaces/%s — updated %d allocation_history entr%s",
                snap.id, changed, "y" if changed == 1 else "ies",
            )
        migrated_docs += 1
        migrated_entries += changed

    logger.info(
        "Done: %d workspace doc(s) %s (%d entr%s renamed), %d skipped (no old entries)",
        migrated_docs,
        "would be updated (dry-run)" if dry_run else "updated",
        migrated_entries,
        "y" if migrated_entries == 1 else "ies",
        skipped_docs,
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
