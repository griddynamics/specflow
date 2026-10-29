"""Delete the objects of one or more projects from the configured object store (SPECS AC-4.7, D6).

Run from the repository root:

    uv --directory backend run python examples/storage_cleanup.py --project-id <uuid> [--project-id <uuid> ...]
    uv --directory backend run python examples/storage_cleanup.py --project-id <uuid> --count-only

For every project id it deletes the objects under `{STORAGE_PREFIX}projects/{project_id}/` and
nothing else. It prints one JSON line per project with object counts (`before`, `deleted`,
`after`), never object names or contents.

Safety: the prefix must start with `dev/local/` (anything else is refused, exit 2), the project id
must be a UUID (so the prefix cannot widen), and the listing is always bounded by the full
project prefix. The store comes from the storage factory: STORAGE_BACKEND (gcs or s3), STORAGE_BUCKET
and STORAGE_PREFIX from the environment, the root .env.local or the cloud profile file
(SPECFLOW_CONFIG_FILE). Credentials come from ADC (gcs) or the AWS chain (s3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from settings import ConfigurationError, load_storage_settings
from storage.base import ManagedObjectAdmin
from storage.factory import build_object_admin

SAFE_ROOT = "dev/local/"


def project_prefix(storage_prefix: str, project_id: str) -> str:
    """Return `{storage_prefix}projects/{project_id}/` after validating both parts."""
    if not storage_prefix.startswith(SAFE_ROOT) or not storage_prefix.endswith("/") or ".." in storage_prefix:
        raise ValueError(f"STORAGE_PREFIX must start with {SAFE_ROOT!r} and end with '/'")
    canonical = str(uuid.UUID(project_id))
    if canonical != project_id.lower():
        raise ValueError("project id must be a canonical UUID")
    return f"{storage_prefix}projects/{canonical}/"


async def run(admin: ManagedObjectAdmin, prefixes: list[tuple[str, str]], *, count_only: bool) -> None:
    try:
        for pid, prefix in prefixes:
            before = await admin.count_prefix(prefix)
            deleted = 0 if count_only else await admin.delete_prefix(prefix)
            after = await admin.count_prefix(prefix)
            sys.stdout.write(
                json.dumps({"project_id": pid, "before": before, "deleted": deleted, "after": after}) + "\n"
            )
    finally:
        await admin.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-id", action="append", required=True, help="project UUID (repeatable)")
    parser.add_argument("--count-only", action="store_true", help="count objects, delete nothing")
    args = parser.parse_args(argv)

    load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")
    try:
        settings = load_storage_settings(os.environ)
    except ConfigurationError as exc:
        sys.stderr.write(f"missing or invalid variables: {', '.join(exc.variables)}\n")
        return 2
    try:
        prefixes = [(pid, project_prefix(settings.prefix, pid)) for pid in args.project_id]
    except ValueError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2

    try:
        admin = build_object_admin(settings)
    except ConfigurationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    asyncio.run(run(admin, prefixes, count_only=args.count_only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
