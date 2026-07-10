"""Single source of truth for workspace-pool seeding.

Both host-side seeding entry points delegate here so the workspace-document schema,
pool-entry validation, workspace-id assignment, and idempotent upsert exist in exactly
one place:

- ``scripts/init_db.py`` — seeds from a ``--workspace-config`` JSON file (e2e / bring-your-own
  repos), plus the bootstrap API key and local-auth identity sentinel.
- ``scripts/create_generation_session_repos.py`` — seeds freshly provisioned repos straight
  into the ``workspaces`` collection.

Previously each script carried its own ``create_workspace_document`` copy (which had already
drifted — one was missing ``cleaning_started_at``) and its own id-assignment/upsert loop. This
module removes that duplication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database.interface import IDatabase

logger = logging.getLogger(__name__)

WORKSPACES_COLLECTION = "workspaces"


@dataclass(frozen=True)
class WorkspacePoolEntry:
    """The durable identity of one workspace pool slot (mirrors the --workspace-config schema).

    ``set_number`` is not part of the file schema — it is assigned by ``assign_pool_entries`` so
    it stays consistent with the workspace_id (both derived from the same index). When absent
    (file-loaded entries), ``seed_workspace_pool`` falls back to entry position.
    """

    workspace_id: str
    repo_url: str
    p10y_repository_id: int
    workspace_pool: str  # required — the --workspace-config file schema mandates all four fields
    set_number: Optional[int] = None

    def __post_init__(self) -> None:
        # bool is an int subclass; reject it so a JSON `true` can't masquerade as an id.
        if not isinstance(self.p10y_repository_id, int) or isinstance(self.p10y_repository_id, bool):
            raise ValueError(
                f"'p10y_repository_id' must be an integer, got: {self.p10y_repository_id!r}"
            )


@dataclass(frozen=True)
class SeedResult:
    """Outcome of an idempotent pool upsert."""

    created: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


def parse_pool_entries(raw: List[Any]) -> List[WorkspacePoolEntry]:
    """Validate a list of raw config dicts into typed entries.

    Raises ValueError (with a 0-based entry index) on any malformed entry so callers can map
    it to their own error channel (init_db.py converts these to a SystemExit).
    """
    entries: List[WorkspacePoolEntry] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} is not an object: {entry!r}")
        try:
            entries.append(WorkspacePoolEntry(**entry))
        except TypeError as exc:
            # Missing required keys or unexpected keys in the JSON object.
            raise ValueError(f"Entry {i} has invalid fields: {exc}") from exc
    return entries


def assign_pool_entries(
    repo_id_map: Dict[str, int],
    github_org: str,
    workspace_pool: str,
    *,
    ordered_repos: Optional[List[str]] = None,
    prefix: Optional[str] = None,
) -> List[WorkspacePoolEntry]:
    """Assign ``ws-{set:02d}-{idx}`` ids to provisioned repos.

    ``ordered_repos`` (the --repos / bring-your-own path): ids follow list position.
    Otherwise (the --start/--end path): the trailing number in each ``{prefix}{num}`` repo name
    fixes the position; names without a parseable number are skipped with a warning.
    """

    def _entry(repo_name: str, idx: int) -> WorkspacePoolEntry:
        set_number = (idx // 3) + 1
        workspace_index = (idx % 3) + 1
        return WorkspacePoolEntry(
            workspace_id=f"ws-{set_number:02d}-{workspace_index}",
            repo_url=f"https://github.com/{github_org}/{repo_name}",
            p10y_repository_id=int(repo_id_map[repo_name]),
            workspace_pool=workspace_pool,
            set_number=set_number,
        )

    entries: List[WorkspacePoolEntry] = []
    if ordered_repos is not None:
        for idx, repo_name in enumerate(ordered_repos):
            entries.append(_entry(repo_name, idx))
        return entries

    if not prefix:
        raise ValueError("prefix is required when ordered_repos is not provided")
    for repo_name in sorted(repo_id_map):
        try:
            num = int(repo_name.split(prefix)[-1])
        except (ValueError, IndexError):
            logger.warning("Could not extract number from repo name: %s — skipping", repo_name)
            continue
        entries.append(_entry(repo_name, num - 1))
    return entries


def build_workspace_document(
    entry: WorkspacePoolEntry, set_number: int, now: datetime
) -> Dict[str, Any]:
    """Build a fresh ``available`` workspace document (schema per state-management.md).

    The single definition of the workspace-document shape — every field a freshly seeded
    workspace must carry, including ``cleaning_started_at`` (which one of the old duplicate
    builders silently omitted).
    """
    return {
        # Core identity
        "repo_url": entry.repo_url,
        "p10y_repository_id": entry.p10y_repository_id,
        "set_number": set_number,
        "workspace_pool": entry.workspace_pool,
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


def seed_workspace_pool(
    db: IDatabase,
    entries: List[WorkspacePoolEntry],
    *,
    replace: bool = False,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> SeedResult:
    """Idempotently upsert pool entries into the ``workspaces`` collection.

    Existing docs are overwritten only when ``replace`` is True; otherwise they are left
    untouched and counted as skipped. ``set_number`` is derived from entry position (groups of
    3), preserving the historical seeding layout.
    """
    now = now or datetime.now(timezone.utc)
    created = updated = skipped = 0

    for i, entry in enumerate(entries):
        # Prefer the entry's assigned set (kept consistent with its workspace_id); fall back to
        # position for file-loaded entries, matching the historical init_db.py layout.
        set_number = entry.set_number if entry.set_number is not None else (i // 3) + 1
        doc = build_workspace_document(entry, set_number, now)

        if dry_run:
            created += 1
            continue

        if db.get(WORKSPACES_COLLECTION, entry.workspace_id) is not None:
            if replace:
                db.update(WORKSPACES_COLLECTION, entry.workspace_id, doc)
                updated += 1
            else:
                skipped += 1
        else:
            db.set(WORKSPACES_COLLECTION, entry.workspace_id, doc)
            created += 1

    return SeedResult(created=created, updated=updated, skipped=skipped)
