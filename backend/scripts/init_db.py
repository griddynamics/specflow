#!/usr/bin/env python3
"""
Database Initialization Script (DB-type aware)

TO BE USED WITH LOCAL TESTING ONLY - `make e2e-setup`

Seeds the active state backend (selected by DATABASE_TYPE) with the workspace pool,
API keys, and the local-auth identity sentinel. The seeding body is backend-agnostic —
it goes through the IDatabase abstraction (get_database()) — so the same script works
for every backend. Only the per-type precheck differs:

    DATABASE_TYPE=sqlite     -> single local file (no Docker); path = SQLITE_DB_PATH
    DATABASE_TYPE=emulator   -> requires FIRESTORE_EMULATOR_HOST (manually-run emulator)
    DATABASE_TYPE=firestore  -> requires GCP_PROJECT_ID (--prod; production, or an
                                 already-hosted GCP-managed instance)

This script:
1. Creates a default API key if none exists (solves chicken-and-egg problem)
2. Creates workspace documents from --workspace-config
3. Sets all workspaces to "available" status with clean_verified=True
4. Adds P10Y repository IDs from the workspace config
5. Is idempotent (safe to run multiple times)

Usage:
    # Local SQLite (default)
    python scripts/init_db.py --workspace-config repos.json --yes

    # Manually-run Firestore emulator
    export FIRESTORE_EMULATOR_HOST=localhost:8080
    python scripts/init_db.py --workspace-config repos.json --yes

    # Real / already-hosted GCP Firestore (be careful!)
    export GCP_PROJECT_ID=your-project-id
    python scripts/init_db.py --prod --workspace-config repos.json
"""

import sys
import os
import argparse
import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List


# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set DATABASE_TYPE early (before importing settings): emulator auto-detect takes
# priority (manually-run emulator), otherwise default to sqlite (the local/Docker-dev
# default) rather than the ephemeral in-memory fallback.
if os.getenv("FIRESTORE_EMULATOR_HOST") and not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "emulator"
elif not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "sqlite"

import httpx

from app.core.config import settings
from app.core.local_identity import LOCAL_API_KEY_DOC_ID, LOCAL_KEY_UID
from app.database.factory import get_database
from app.database.interface import IDatabase


# Example "extra" workspace pool: lets you exercise multi-pool segregation locally. The
# extra_pool_user key (below) allocates from it. It is OPTIONAL — delete the matching
# `"workspace_pool": "testpool"` entries from your --workspace-config file and the key is not
# seeded (see initialize_api_key). Must be one of ALLOWED_WORKSPACE_POOLS.
EXTRA_WORKSPACE_POOL = "testpool"
EXTRA_POOL_KEY_UID = "00000000-e2e0-0000-0000-000000000002"


@dataclass
class WorkspaceConfig:
    """Typed workspace configuration entry parsed from ``--workspace-config`` JSON."""

    workspace_id: str
    repo_url: str
    p10y_repository_id: int
    workspace_pool: str

    def __post_init__(self) -> None:
        if not isinstance(self.p10y_repository_id, int) or isinstance(self.p10y_repository_id, bool):
            raise ValueError(
                f"'p10y_repository_id' must be an integer, got: {self.p10y_repository_id!r}"
            )

    def to_pool_entry(self) -> dict:
        """Normalise to the dict shape ``initialize_workspace_pool`` consumes."""
        return {
            "workspace_id": self.workspace_id,
            "repo_url": self.repo_url,
            "p10y_id": self.p10y_repository_id,
            "workspace_pool": self.workspace_pool,
        }


# CONFIGURATION: Workspace Repository Mapping
#
# There are intentionally NO default repos. Workspace allocation clones every repo_url even in
# SKIP_MODE, so the pool must point at repos you control. Supply them via --workspace-config; the
# script refuses to run without it. See e2e-workspace-config.example.json (repo root, next to
# .env.example) for the schema, and load_workspace_configs_from_file below for parsing.
#
#   cp e2e-workspace-config.example.json my-test-repos.json   # then edit repo_url / p10y_repository_id
#   make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json
#
# Populated from --workspace-config in main(); empty otherwise.
WORKSPACE_CONFIGS: List[dict] = []


def initialize_api_key(db: IDatabase, dry_run: bool = False) -> None:
    """
    Initialize a default API key if none exists.
    
    This solves the chicken-and-egg problem where creating an API key
    requires an API key in the header. This function creates at least
    one key during database initialization.
    
    The user_id (email) is read from USER_EMAIL environment variable.
    This ensures the API key matches the email used for testing and
    email notifications.
    
    Args:
        db: Database interface
        dry_run: If True, print what would be done without modifying database
    """
    print(f"\n{'='*60}")
    print("API KEY INITIALIZATION")
    print(f"{'='*60}\n")
    
    # Check if any API keys exist
    try:
        existing_keys = db.query("api_keys")
        print(f"DEBUG: Query returned {len(existing_keys)} key(s)")
        if existing_keys:
            print(f"DEBUG: Sample keys: {[k.get('api_key', 'N/A')[:20] + '...' for k in existing_keys[:3]]}")
    except Exception as e:
        print(f"WARNING: Error querying api_keys: {e}")
        existing_keys = []
    
    if existing_keys:
        active_keys = [k for k in existing_keys if k.get("is_active", True)]
        inactive_keys = len(existing_keys) - len(active_keys)
        print(f"✓ Found {len(existing_keys)} total API key(s) in database")
        print(f"  - Active: {len(active_keys)}")
        if inactive_keys > 0:
            print(f"  - Inactive: {inactive_keys}")
        if active_keys:
            # Show first key prefix and user
            first_key = active_keys[0].get("api_key", "N/A")
            first_user = active_keys[0].get("user_id", "N/A")
            print(f"  Example: {first_key[:15]}... (user: {first_user})")
        return
    
    # Get user email from environment variable
    user_email = os.getenv("USER_EMAIL", "system@init.local")
    if user_email == "system@init.local":
        print("⚠️  WARNING: USER_EMAIL not set in environment")
        print("   Using default: system@init.local")
        print("   Set USER_EMAIL in .env for proper email notifications")
        print("   Example: USER_EMAIL=your.email@example.com")
    
    now = datetime.now(timezone.utc)

    # Stable UUIDs so key_uid is deterministic across e2e-setup runs.
    keys_to_seed = [
        ("e2e_tests_user", "default", "00000000-e2e0-0000-0000-000000000001"),
    ]
    if extra_pool_configured():
        keys_to_seed.append(("extra_pool_user", EXTRA_WORKSPACE_POOL, EXTRA_POOL_KEY_UID))

    for api_key, pool, key_uid in keys_to_seed:
        key_doc = {
            "api_key": api_key,
            "key_uid": key_uid,
            "user_id": user_email,
            "user_name": "System Initialization",
            "created_at": now,
            "last_used_at": None,
            "expires_at": None,
            "is_active": True,
            "permissions": ["admin"],
            "workspace_pool": pool,
            "metadata": {
                "created_by": "init_db.py",
                "purpose": "bootstrap_key"
            },
            "max_concurrent_sessions": 5,
            "active_generation_sessions": [],
        }

        if dry_run:
            print(f"[DRY RUN] Would create API key: {api_key} (pool: {pool}, key_uid: {key_uid})")
            print(f"  User: {user_email}")
        else:
            db.set("api_keys", api_key, key_doc)
            print(f"✓ Created API key (pool: {pool})")
            print(f"  Key: {api_key}")
            print(f"  key_uid: {key_uid}")
            print(f"  User: {user_email}")

    print(f"{'='*60}\n")


def initialize_local_identity(db: IDatabase, dry_run: bool = False, replace: bool = False) -> None:
    """
    Seed the local-auth sentinel api_keys document (doc-id ``"local"``).

    This is a SEPARATE function from ``initialize_api_key`` so that the sentinel
    is seeded unconditionally — even when ``initialize_api_key`` early-returns
    because e2e_tests_user already exists.  (Reviewer fix B2.)

    Idempotency rules:
    - Without ``replace``: if the ``"local"`` doc already exists, skip.
    - With ``replace``: overwrite unconditionally.

    Args:
        db: Database interface.
        dry_run: If True, print what would be done without modifying the database.
        replace: If True, overwrite an existing sentinel doc.
    """
    print(f"\n{'='*60}")
    print("LOCAL IDENTITY SENTINEL SEEDING")
    print(f"{'='*60}\n")

    existing = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
    if existing and not replace:
        print(f"✓ Local sentinel doc '{LOCAL_API_KEY_DOC_ID}' already exists — skipping (use --replace to overwrite)")
        print(f"{'='*60}\n")
        return

    user_id = (
        settings.LOCAL_USER_EMAIL
        or os.getenv("USER_EMAIL")
        or "system@init.local"
    )
    user_name = settings.LOCAL_USER_NAME or "Local User"
    now = datetime.now(timezone.utc)

    sentinel_doc = {
        "api_key": LOCAL_API_KEY_DOC_ID,
        "key_uid": LOCAL_KEY_UID,
        "user_id": user_id,
        "user_name": user_name,
        "created_at": now,
        "last_used_at": None,
        "expires_at": None,
        "is_active": True,
        "permissions": ["admin"],
        "workspace_pool": "default",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [],
    }

    if dry_run:
        action = "overwrite" if existing else "create"
        print(f"[DRY RUN] Would {action} sentinel doc: {LOCAL_API_KEY_DOC_ID} (key_uid: {LOCAL_KEY_UID})")
        print(f"  User: {user_id} / {user_name}")
    else:
        db.set("api_keys", LOCAL_API_KEY_DOC_ID, sentinel_doc)
        action = "Updated" if existing else "Created"
        print(f"✓ {action} local sentinel doc '{LOCAL_API_KEY_DOC_ID}'")
        print(f"  key_uid: {LOCAL_KEY_UID}")
        print(f"  User: {user_id} / {user_name}")

    print(f"{'='*60}\n")


def load_workspace_configs_from_file(path: str) -> List[dict]:
    """
    Load workspace configs from a JSON file.

    Expected JSON schema::

        [
          {
            "workspace_id": "ws-01-1",
            "repo_url": "https://github.com/org/repo",
            "p10y_repository_id": 12345,
            "workspace_pool": "default"
          },
          ...
        ]

    Returns a list of dicts normalised so ``initialize_workspace_pool`` can
    consume them (``workspace_id``, ``repo_url``, ``p10y_id``, ``workspace_pool``).

    Raises SystemExit on malformed input.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Could not read workspace config file '{path}': {exc}")
        sys.exit(1)

    if not isinstance(data, list):
        print(f"ERROR: Workspace config file must contain a JSON array, got {type(data).__name__}")
        sys.exit(1)

    normalised: List[dict] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            print(f"ERROR: Entry {i} in workspace config is not an object: {entry!r}")
            sys.exit(1)
        try:
            config = WorkspaceConfig(**entry)
        except TypeError as exc:
            # Missing required keys or unexpected keys in the JSON object.
            print(f"ERROR: Entry {i} in workspace config has invalid fields: {exc}")
            sys.exit(1)
        except ValueError as exc:
            print(f"ERROR: Entry {i} in workspace config: {exc}")
            sys.exit(1)
        normalised.append(config.to_pool_entry())

    return normalised


def create_workspace_document(
    workspace_id: str,
    set_number: int,
    repo_url: str,
    p10y_repository_id: int,
    now: datetime,
    workspace_pool: str = "default",
) -> dict:
    """
    Create a workspace document following the schema from state-management.md.
    
    Schema fields:
    - repo_url: GitHub repository URL
    - p10y_repository_id: P10Y repository ID for tracking
    - set_number: Which set this workspace belongs to (1-10)
    - status: Workspace state (available, allocated, cleaning, stuck)
    - locked_by: Which generation session is using this workspace
    - locked_at: When workspace was allocated
    - lease_expires_at: When lease expires (for crash detection)
    - clean_verified: CRITICAL - must be true to allocate
    - last_used_by: Previous generation session (for debugging)
    - last_cleaned_at: Last cleanup timestamp
    - allocation_history: Audit trail of allocations
    - error: Error message if status is stuck
    """
    return {
        # Core fields
        "repo_url": repo_url,
        "p10y_repository_id": p10y_repository_id,
        "set_number": set_number,
        "workspace_pool": workspace_pool,
        
        # Allocation state
        "status": "available",
        "locked_by": None,
        "locked_at": None,
        "lease_expires_at": None,
        "cleaning_started_at": None,
        
        # Safety fields
        "clean_verified": True,  # CRITICAL: must be true to allocate
        "last_used_by": None,
        "last_cleaned_at": now,
        
        # Audit trail
        "allocation_history": [],
        
        # Error tracking
        "error": None,
    }


def initialize_workspace_pool(
    db: IDatabase,
    dry_run: bool = False,
    yes: bool = False,
    replace: bool = False,
) -> None:
    """
    Initialize workspace pool in Firestore.

    Creates workspaces from ``WORKSPACE_CONFIGS`` (populated from
    ``--workspace-config``) all in available state.  Idempotent — safe to run
    multiple times.

    Args:
        db: Database interface.
        dry_run: If True, print what would be done without modifying database.
        yes: If True, skip interactive confirmation prompts.
        replace: If True, overwrite existing workspace docs (old behaviour).
                 If False, skip docs that already exist.
    """
    now = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print("WORKSPACE POOL INITIALIZATION")
    print(f"{'='*60}")
    print(f"Timestamp: {now.isoformat()}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Target: {os.getenv('FIRESTORE_EMULATOR_HOST', 'Production Firestore')}")
    print(f"{'='*60}\n")

    # Check if workspaces already exist
    existing_workspaces = db.query("workspaces")
    if existing_workspaces:
        print(f"⚠️  Found {len(existing_workspaces)} existing workspaces in database")
        if yes:
            response = "yes"
        else:
            response = input("Do you want to update/recreate them? (yes/no): ")
        if response.lower() != "yes":
            print("Aborted.")
            return

    # Create workspaces
    created = 0
    updated = 0
    skipped = 0

    for i, config in enumerate(WORKSPACE_CONFIGS):
        set_number = (i // 3) + 1  # Sets 1-10
        # Prefer explicit workspace_id from config (JSON-loaded); derive otherwise.
        workspace_id = config.get("workspace_id") or f"ws-{set_number:02d}-{(i % 3) + 1}"

        workspace_doc = create_workspace_document(
            workspace_id=workspace_id,
            set_number=set_number,
            repo_url=config["repo_url"],
            p10y_repository_id=config["p10y_id"],
            now=now,
            workspace_pool=config.get("workspace_pool", "default"),
        )

        if dry_run:
            print(f"[DRY RUN] Would create workspace: {workspace_id}")
            print(f"  - Set: {set_number}")
            print(f"  - Repo: {config['repo_url']}")
            print(f"  - P10Y ID: {config['p10y_id']}")
            created += 1
        else:
            # Check if workspace exists
            existing = db.get("workspaces", workspace_id)

            if existing:
                if replace:
                    db.update("workspaces", workspace_id, workspace_doc)
                    print(f"✓ Updated workspace: {workspace_id} (set {set_number})")
                    updated += 1
                else:
                    print(f"  Skipped workspace: {workspace_id} (already exists; use --replace to overwrite)")
                    skipped += 1
            else:
                # Create new workspace
                db.set("workspaces", workspace_id, workspace_doc)
                print(f"✓ Created workspace: {workspace_id} (set {set_number})")
                created += 1

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    if dry_run:
        print(f"Would create: {created} workspaces")
    else:
        print(f"Created: {created} workspaces")
        print(f"Updated: {updated} workspaces")
        if skipped:
            print(f"Skipped: {skipped} workspaces (already exist)")
        print(f"Total: {created + updated + skipped} workspaces")
        expected_sets = (len(WORKSPACE_CONFIGS) + 2) // 3
        print(f"Configured sets: {expected_sets} set(s) of up to 3 workspaces")
    print(f"{'='*60}\n")

    if not dry_run:
        # Verify workspace pool status
        print("Verifying workspace pool...")
        all_workspaces = db.query("workspaces")
        available = [w for w in all_workspaces if w.get("status") == "available"]
        clean_verified = [w for w in all_workspaces if w.get("clean_verified") is True]

        print(f"✓ Total workspaces: {len(all_workspaces)}")
        print(f"✓ Available: {len(available)}")
        print(f"✓ Clean verified: {len(clean_verified)}")

        # Count available complete sets from the configured set numbers.
        configured_set_numbers = sorted({w.get("set_number") for w in all_workspaces if w.get("set_number")})
        available_sets = 0
        for set_num in configured_set_numbers:
            set_workspaces = [
                w for w in all_workspaces
                if w.get("set_number") == set_num
                and w.get("status") == "available"
                and w.get("clean_verified") is True
            ]
            if len(set_workspaces) == 3:
                available_sets += 1

        total_sets = len(configured_set_numbers)
        print(f"✓ Available complete sets: {available_sets}/{total_sets}")
        print()

        if available_sets < total_sets:
            print("⚠️  WARNING: Not all sets are fully available!")
        else:
            print("✅ Workspace pool initialized successfully!")


def extra_pool_configured() -> bool:
    """Return True when the loaded workspace config declares the example extra pool."""
    return any(config.get("workspace_pool") == EXTRA_WORKSPACE_POOL for config in WORKSPACE_CONFIGS)


def attach_github_tokens(dry_run: bool = False) -> None:
    """
    If GITHUB_TOKEN is set, attach it to the extra-pool API key via PUT /api/v1/auth/github-token.

    Uses e2e_tests_user (admin key) with target_key_uid to update extra_pool_user.
    Skipped when GITHUB_TOKEN is not set or the workspace config has no extra pool.
    """
    if not extra_pool_configured():
        print(f"ℹ  No {EXTRA_WORKSPACE_POOL} workspaces configured — skipping extra-pool GitHub token attachment")
        return

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("ℹ  GITHUB_TOKEN not set — skipping GitHub token attachment for extra pool")
        return

    backend_url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
    url = f"{backend_url}/api/v1/auth/github-token"
    user_email = os.getenv("USER_EMAIL", "system@init.local")

    print(f"\n{'='*60}")
    print("GITHUB TOKEN ATTACHMENT")
    print(f"{'='*60}\n")

    if dry_run:
        print(f"[DRY RUN] Would PUT {url}")
        print(f"  target_key_uid: {EXTRA_POOL_KEY_UID} ({EXTRA_WORKSPACE_POOL} pool)")
        print(f"{'='*60}\n")
        return

    try:
        resp = httpx.put(
            url,
            json={"token": token, "target_key_uid": EXTRA_POOL_KEY_UID},
            headers={"X-API-Key": "e2e_tests_user", "X-User-Email": user_email},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"✓ GitHub token attached to extra-pool key (key_uid: {EXTRA_POOL_KEY_UID})")
        print(f"{'='*60}\n")
    except httpx.HTTPStatusError as e:
        print(f"ERROR: Failed to attach GitHub token: HTTP {e.response.status_code} — {e.response.text}")
        print(f"{'='*60}\n")
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"ERROR: Could not reach backend at {backend_url}: {e}")
        print("   Ensure the backend is up before running e2e-setup.")
        print(f"{'='*60}\n")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Initialize Firestore workspace pool"
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Run against production Firestore (requires GCP_PROJECT_ID)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without modifying database"
    )
    parser.add_argument(
        "--workspace-config",
        metavar="FILE",
        default=None,
        help=(
            "Path to a JSON file containing the workspace config list. "
            "Schema: [{\"workspace_id\": str, \"repo_url\": str, "
            "\"p10y_repository_id\": int, \"workspace_pool\": str}, ...]. "
            "When provided, replaces the hardcoded WORKSPACE_CONFIGS."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Answer 'yes' to all interactive confirmation prompts (non-interactive mode)"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing workspace and sentinel docs (default: skip existing)"
    )
    args = parser.parse_args()

    # Workspace configs MUST come from --workspace-config. There are no default repos: workspace
    # allocation clones every repo_url (even in SKIP_MODE), so the pool can only point at repos the
    # user controls. Refuse to run otherwise rather than seeding a broken pool.
    global WORKSPACE_CONFIGS
    if not args.workspace_config:
        print(
            "ERROR: No workspace config provided. There are no default test repos.\n"
            "       Copy the template, point it at repos you control, then pass it via "
            "--workspace-config:\n"
            "         cp e2e-workspace-config.example.json my-test-repos.json\n"
            "         # edit repo_url / p10y_repository_id in my-test-repos.json\n"
            "       Then re-run, e.g.:\n"
            "         make skip-mode-e2e-tests E2E_WORKSPACE_CONFIG=my-test-repos.json"
        )
        sys.exit(1)
    WORKSPACE_CONFIGS = load_workspace_configs_from_file(args.workspace_config)
    print(f"✓ Loaded {len(WORKSPACE_CONFIGS)} workspace configs from {args.workspace_config}")

    # Safety check for production
    if args.prod:
        if not os.getenv("GCP_PROJECT_ID"):
            print("ERROR: GCP_PROJECT_ID environment variable not set")
            sys.exit(1)

        print("\n⚠️  WARNING: Running against PRODUCTION Firestore!")
        print(f"Project ID: {os.getenv('GCP_PROJECT_ID')}")
        if args.yes:
            response = "yes"
        else:
            response = input("Are you sure you want to continue? (yes/no): ")
        if response.lower() != "yes":
            print("Aborted.")
            sys.exit(0)
    else:
        db_type = os.getenv("DATABASE_TYPE", "memory").lower()

        # Emulator mode needs a reachable host:port; sqlite/memory need no external process.
        if db_type == "emulator" and not os.getenv("FIRESTORE_EMULATOR_HOST"):
            print("ERROR: FIRESTORE_EMULATOR_HOST not set")
            print("Run: export FIRESTORE_EMULATOR_HOST=localhost:8080")
            sys.exit(1)

        print(f"✓ Database type: {db_type}")
        if db_type == "memory":
            print("⚠️  WARNING: DATABASE_TYPE=memory will not persist data!")
            print("   Set DATABASE_TYPE=sqlite for a persistent local database")
        elif db_type == "sqlite":
            print(f"✓ Using SQLite at {settings.SQLITE_DB_PATH}")
        elif db_type == "emulator":
            emulator_host = os.getenv("FIRESTORE_EMULATOR_HOST")
            print(f"✓ Using Firestore Emulator at {emulator_host}")

    # Create database connection
    try:
        db = get_database()
        db_type = os.getenv("DATABASE_TYPE", "memory").lower()
        print(f"✓ Connected to database (type: {db_type})")
    except Exception as e:
        print(f"ERROR: Failed to connect to database: {e}")
        sys.exit(1)

    # Initialize e2e_tests_user / optional extra_pool_user API keys (early-returns when any exist).
    try:
        initialize_api_key(db, dry_run=args.dry_run)
    except Exception as e:
        print(f"\nERROR: API key initialization failed: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Seed local-auth sentinel doc UNCONDITIONALLY — not gated by initialize_api_key's
    # early-return.  This ensures the sentinel is present even when e2e_tests_user
    # already exists.  (Reviewer fix B2.)
    try:
        initialize_local_identity(db, dry_run=args.dry_run, replace=args.replace)
    except Exception as e:
        print(f"\nERROR: Local identity sentinel seeding failed: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Initialize workspace pool
    try:
        initialize_workspace_pool(db, dry_run=args.dry_run, yes=args.yes, replace=args.replace)
    except Exception as e:
        print(f"\nERROR: Initialization failed: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Attach GitHub tokens to pool-specific keys
    attach_github_tokens(dry_run=args.dry_run)

    # Reminder about indexes (Firestore production/hosted only; sqlite/emulator need none)
    if not args.dry_run and db_type == "firestore":
        print("\n" + "="*60)
        print("⚠️  IMPORTANT: Create Firestore Indexes")
        print("="*60)
        print("Some queries require composite indexes. Create them with:")
        print("  python scripts/create_firestore_indexes.py --prod")
        print("="*60 + "\n")


if __name__ == "__main__":
    main()
