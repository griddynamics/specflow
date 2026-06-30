#!/usr/bin/env python3
"""
Migration 09: Multi-session generation slots on api_keys (single cutover).

1. For each api_keys doc: if a legacy lock was active (in_progress and within TTL),
   seed ``active_generation_sessions`` with one entry using ``current_process`` as
   ``generation_id``. Otherwise seed empty.
2. Set ``max_concurrent_sessions`` to 5 when missing.
3. Delete legacy fields: ``current_process``, ``in_progress``, ``operation_started_at``,
   ``operation_ttl_minutes``.

Run BEFORE deploying code that reads only ``active_generation_sessions``.

Usage:
    python migrate_09_api_key_sessions.py [--dry-run] [--project PROJECT_ID]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        return datetime.fromtimestamp(float(ts()), tz=timezone.utc)
    return None


def _legacy_lock_active(doc: dict, now: datetime) -> bool:
    if not doc.get("in_progress"):
        return False
    started = _parse_dt(doc.get("operation_started_at"))
    if started is None:
        return False
    ttl_min = int(doc.get("operation_ttl_minutes") or 90)
    return now < started + timedelta(minutes=ttl_min)


async def migrate_api_keys(client: firestore.AsyncClient, dry_run: bool = False) -> None:
    collection = client.collection("api_keys")
    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc)

    async for doc_snap in collection.stream():
        doc = doc_snap.to_dict() or {}
        doc_id = doc_snap.id

        legacy_fields = ("current_process", "in_progress", "operation_started_at", "operation_ttl_minutes")
        already_new = doc.get("active_generation_sessions") is not None and not any(
            k in doc for k in legacy_fields
        )
        if already_new:
            skipped += 1
            continue

        max_concurrent = int(doc.get("max_concurrent_sessions") or 5)
        active: list[dict] = []

        if _legacy_lock_active(doc, now):
            gid = doc.get("current_process")
            if isinstance(gid, str) and gid:
                ttl = int(doc.get("operation_ttl_minutes") or 90)
                started = _parse_dt(doc.get("operation_started_at")) or now
                active.append(
                    {
                        "generation_id": gid,
                        "operation": "analysis",
                        "lease_started_at": started,
                        "lease_ttl_minutes": ttl,
                    }
                )

        delete_fields = firestore.DELETE_FIELD
        update_payload = {
            "active_generation_sessions": active,
            "max_concurrent_sessions": max_concurrent,
            "current_process": delete_fields,
            "in_progress": delete_fields,
            "operation_started_at": delete_fields,
            "operation_ttl_minutes": delete_fields,
        }

        if dry_run:
            logger.info("[DRY RUN] Would migrate api_keys/%s active=%s", doc_id, active)
        else:
            await doc_snap.reference.update(update_payload)
            logger.info("Migrated api_keys/%s active=%s", doc_id, active)
        updated += 1

    logger.info("Done — migrated: %d, skipped (already new shape): %d", updated, skipped)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Log changes without writing")
    parser.add_argument("--project", default=None, help="GCP project ID")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    await migrate_api_keys(client, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
