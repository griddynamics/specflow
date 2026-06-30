#!/usr/bin/env python3
"""
Migration 06: Validate no live estimation has a checkpoint that would become
inconsistent after CHECKPOINT_ORDER reorder (PLANNING_DONE before SYNCED_SPECS).

This is a VALIDATION-ONLY migration. It does NOT modify data.
Run BEFORE deploying the checkpoint reorder. Abort if any RUNNING estimation
has a checkpoint between SYNCED_SPECS and PLANNING_DONE (old order).

The reorder happens in code (estimation_enums.py); this script ensures
no existing document would be left in an invalid state.

Usage:
    python migrate_06_reorder_checkpoint_enum.py [--dry-run] [--project PROJECT_ID]
"""
import asyncio
import argparse
import logging
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Old order had SYNCED_SPECS (index 2) before PLANNING_DONE (index 4)
# New order: PLANNING_DONE before SYNCED_SPECS
# Checkpoints that would be "between" in old flow (problematic if RUNNING):
OLD_MID_CHECKPOINTS = {"synced_specs", "synced_code"}


async def validate_estimations(client: firestore.AsyncClient) -> bool:
    """
    Validate all estimations. Return False if any RUNNING estimation
    has checkpoint in OLD_MID_CHECKPOINTS (would need manual intervention).
    """
    collection = client.collection("estimations")
    problematic = []

    async for doc_ref in collection.stream():
        doc = doc_ref.to_dict()
        status = doc.get("status", "")
        checkpoint = doc.get("checkpoint", "")

        if status == "running" and checkpoint in OLD_MID_CHECKPOINTS:
            problematic.append({
                "id": doc_ref.id,
                "status": status,
                "checkpoint": checkpoint,
            })

    if problematic:
        logger.error(
            "Found %d RUNNING estimation(s) with checkpoint in [synced_specs, synced_code]. "
            "These would be inconsistent after reorder. Resolve before deploy.",
            len(problematic),
        )
        for p in problematic:
            logger.error("  - %s: status=%s, checkpoint=%s", p["id"], p["status"], p["checkpoint"])
        return False

    logger.info("Validation passed: no RUNNING estimations with mid-checkpoint.")
    return True


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Same as normal (validation only)")
    parser.add_argument("--project", default=None, help="GCP project ID")
    args = parser.parse_args()

    client = firestore.AsyncClient(project=args.project)
    ok = await validate_estimations(client)
    exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
