#!/usr/bin/env python3
"""
Create Firestore Composite Indexes

Single source of truth for all composite indexes required by the application.
Run this script (idempotent) whenever indexes change — it skips indexes that
already exist. ``firestore.indexes.json`` has been removed; this script replaces it.

Required indexes:
1.  workspaces:           status (ASC) + lease_expires_at (ASC)
2.  workspaces:           set_number (ASC) + status (ASC) + clean_verified (ASC)
2b. workspaces:           workspace_pool (ASC) + set_number (ASC) + status (ASC) + clean_verified (ASC)
3.  workspaces:           status (ASC) + cleaning_started_at (ASC)
4.  workspaces:           scheduled_for_wipe (ASC) + scheduled_for_wipe_at (ASC)
5.  generation_sessions:  status (ASC) + status_changed_at (ASC)
6.  generation_sessions:  status (ASC) + last_activity_at (ASC)
7.  generation_sessions:  key_uid (ASC) + created_at (DESC)   [local quickstart uses emulator — index only needed for prod]

Usage:
    # With Firestore Emulator (indexes not needed, but script will skip gracefully)
    export FIRESTORE_EMULATOR_HOST=localhost:8080
    python scripts/create_firestore_indexes.py

    # Production (requires proper GCP authentication)
    export GCP_PROJECT_ID=your-project-id
    python scripts/create_firestore_indexes.py --prod

    # Named database
    export GCP_PROJECT_ID=your-project-id
    python scripts/create_firestore_indexes.py --prod --database my-database
"""

import sys
import os
import argparse
from pathlib import Path
from typing import List, Dict

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from google.cloud import firestore_admin_v1
    from google.cloud.firestore_admin_v1.types import Index
except ImportError as e:
    print(e)
    print("ERROR: google-cloud-firestore-admin not installed")
    print("Install with: uv add google-cloud-firestore-admin")
    print("Or: pip install google-cloud-firestore-admin")
    sys.exit(1)


# Index definitions
INDEXES = [
    {
        "collection": "workspaces",
        "fields": [
            {"field": "status", "order": "ASCENDING"},
            {"field": "lease_expires_at", "order": "ASCENDING"},
        ],
        "description": "For recover_expired_leases query: status == 'allocated' AND lease_expires_at < now",
    },
    {
        "collection": "workspaces",
        "fields": [
            {"field": "set_number", "order": "ASCENDING"},
            {"field": "status", "order": "ASCENDING"},
            {"field": "clean_verified", "order": "ASCENDING"},
        ],
        "description": "Legacy allocate query without workspace_pool (remove after all workspaces backfilled)",
    },
    {
        "collection": "workspaces",
        "fields": [
            {"field": "workspace_pool", "order": "ASCENDING"},
            {"field": "set_number", "order": "ASCENDING"},
            {"field": "status", "order": "ASCENDING"},
            {"field": "clean_verified", "order": "ASCENDING"},
        ],
        "description": "allocate_workspace_set with workspace_pool filter",
    },
    {
        "collection": "generation_sessions",
        "fields": [
            {"field": "status", "order": "ASCENDING"},
            {"field": "status_changed_at", "order": "ASCENDING"},
        ],
        "description": "For recover_stuck_initializing query: status == 'initializing' AND status_changed_at < threshold",
    },
    {
        "collection": "generation_sessions",
        "fields": [
            {"field": "status", "order": "ASCENDING"},
            {"field": "last_activity_at", "order": "ASCENDING"},
        ],
        "description": "For stuck_running_detector query: status == 'running' AND last_activity_at < threshold",
    },
    {
        "collection": "generation_sessions",
        "fields": [
            {"field": "key_uid", "order": "ASCENDING"},
            {"field": "created_at", "order": "DESCENDING"},
        ],
        # NOTE: local quickstart uses the Firestore emulator which does not require
        # composite indexes — this index is only needed for prod Firestore deployments.
        "description": "For GET /generation-sessions/ list: key_uid == x ORDER BY created_at DESC",
    },
    {
        "collection": "workspaces",
        "fields": [
            {"field": "status", "order": "ASCENDING"},
            {"field": "cleaning_started_at", "order": "ASCENDING"},
        ],
        "description": "For stuck_cleaning_recovery query: status == 'cleaning' AND cleaning_started_at < threshold",
    },
    {
        "collection": "workspaces",
        "fields": [
            {"field": "scheduled_for_wipe", "order": "ASCENDING"},
            {"field": "scheduled_for_wipe_at", "order": "ASCENDING"},
        ],
        "description": "For scheduled_wipe query: scheduled_for_wipe == True AND scheduled_for_wipe_at < now",
    },
]


def create_index(
    client: firestore_admin_v1.FirestoreAdminClient,
    project_id: str,
    database_id: str,
    collection: str,
    fields: List[Dict[str, str]],
    dry_run: bool = False,
) -> bool:
    """
    Create a composite index in Firestore.
    
    Args:
        client: Firestore Admin client
        project_id: GCP project ID
        database_id: Firestore database ID (usually "(default)")
        collection: Collection name
        fields: List of field definitions with "field" and "order" keys
        dry_run: If True, only print what would be created
        
    Returns:
        True if index was created or already exists, False on error
    """
    parent = f"projects/{project_id}/databases/{database_id}/collectionGroups/{collection}"
    
    # Build index fields
    index_fields = []
    for field_def in fields:
        order = (
            Index.IndexField.Order.ASCENDING
            if field_def["order"] == "ASCENDING"
            else Index.IndexField.Order.DESCENDING
        )
        index_fields.append(
            Index.IndexField(
                field_path=field_def["field"],
                order=order
            )
        )
    
    # Create index definition
    index = Index(
        query_scope=Index.QueryScope.COLLECTION,
        fields=index_fields,
    )
    
    if dry_run:
        field_str = ", ".join([f"{f['field']} ({f['order']})" for f in fields])
        print(f"[DRY RUN] Would create index: {collection} on [{field_str}]")
        return True
    
    try:
        # Check if index already exists
        existing_indexes = client.list_indexes(parent=parent)
        for existing in existing_indexes:
            if len(existing.fields) == len(index_fields):
                # Check if fields match
                match = all(
                    ef.field_path == nf.field_path and ef.order == nf.order
                    for ef, nf in zip(existing.fields, index_fields)
                )
                if match:
                    print(f"✓ Index already exists: {collection} on [{', '.join([f['field'] for f in fields])}]")
                    return True
        
        # Create the index
        print(f"Creating index: {collection} on [{', '.join([f['field'] for f in fields])}]...")
        operation_result = client.create_index(parent=parent, index=index)
        
        # Wait for operation to complete
        if hasattr(operation_result, 'result'):
            # Operation is synchronous
            result = operation_result.result()
            print(f"✓ Index created successfully: {collection}, result: {result}")
            return True
        else:
            # Operation is asynchronous, wait for it
            print(f"  Index creation started (operation: {operation_result.name})")
            print("  Note: Index creation may take a few minutes. Check status in Firebase Console.")
            return True
            
    except Exception as e:
        error_str = str(e)
        if "already exists" in error_str.lower() or "ALREADY_EXISTS" in error_str:
            print(f"✓ Index already exists: {collection} on [{', '.join([f['field'] for f in fields])}]")
            return True
        else:
            print(f"✗ Error creating index for {collection}: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Create Firestore composite indexes"
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Run against production Firestore (requires GCP_PROJECT_ID)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without creating indexes"
    )
    parser.add_argument(
        "--database",
        default="(default)",
        help="Firestore database name (default: '(default)'). Use for named databases."
    )
    args = parser.parse_args()
    
    # Check if running against emulator
    if os.getenv("FIRESTORE_EMULATOR_HOST"):
        print("⚠️  Firestore Emulator detected. Indexes are not needed for the emulator.")
        print("   The emulator automatically creates indexes as needed.")
        print("   Skipping index creation.")
        return
    
    # Get project ID
    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        print("ERROR: GCP_PROJECT_ID environment variable not set")
        print("Set it with: export GCP_PROJECT_ID=your-project-id")
        sys.exit(1)
    
    # Safety check for production
    if args.prod:
        print("\n⚠️  WARNING: Creating indexes in PRODUCTION Firestore!")
        print(f"Project ID: {project_id}")
        response = input("Are you sure you want to continue? (yes/no): ")
        if response.lower() != "yes":
            print("Aborted.")
            sys.exit(0)
    
    database_id = args.database
    
    print(f"\n{'='*60}")
    print("FIRESTORE INDEX CREATION")
    print(f"{'='*60}")
    print(f"Project: {project_id}")
    print(f"Database: {database_id}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"{'='*60}\n")
    
    # Create Firestore Admin client
    try:
        client = firestore_admin_v1.FirestoreAdminClient()
        print("✓ Connected to Firestore Admin API")
    except Exception as e:
        print(f"ERROR: Failed to connect to Firestore Admin API: {e}")
        print("\nMake sure you have:")
        print("1. Authenticated with: gcloud auth application-default login")
        print("2. Installed: pip install google-cloud-firestore-admin")
        print("3. Have permissions: roles/datastore.indexAdmin or roles/datastore.owner")
        sys.exit(1)
    
    # Create all indexes
    success_count = 0
    failed_count = 0
    
    for index_def in INDEXES:
        print(f"\n{index_def['description']}")
        success = create_index(
            client=client,
            project_id=project_id,
            database_id=database_id,
            collection=index_def["collection"],
            fields=index_def["fields"],
            dry_run=args.dry_run,
        )
        if success:
            success_count += 1
        else:
            failed_count += 1
    
    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    if args.dry_run:
        print(f"Would create: {len(INDEXES)} indexes")
    else:
        print(f"Success: {success_count}")
        if failed_count > 0:
            print(f"Failed: {failed_count}")
        print(f"Total: {len(INDEXES)} indexes")
    
    if not args.dry_run and failed_count == 0:
        print("\n✅ All indexes created successfully!")
        print("   Note: Index creation may take a few minutes to complete.")
        print("   Check status in Firebase Console:")
        print(f"   https://console.firebase.google.com/v1/r/project/{project_id}/firestore/indexes")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
