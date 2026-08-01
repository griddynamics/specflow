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

from app.core.workspace_pool_names import WORKSPACES_PER_SET
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
        set_number = (idx // WORKSPACES_PER_SET) + 1
        workspace_index = (idx % WORKSPACES_PER_SET) + 1
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


@dataclass(frozen=True)
class PoolNamingScheme:
    """How an existing pool names its repositories, so expansion can extend it.

    Derived from the pool's current ``repo_url`` values rather than from a configured
    default: the provisioning script's default prefix (``generation-workspace``) and
    ``settings.WORKSPACE_REPO_PREFIX`` (``specflow-workspace``) disagree, so trusting a
    default would create a second, differently-named family of repos alongside the first.
    """

    github_org: str
    prefix: str
    highest_repo_number: int
    highest_set_number: int

    @property
    def next_set_number(self) -> int:
        return self.highest_set_number + 1

    def repo_names_for_sets(self, set_count: int) -> List[str]:
        """Names for ``set_count`` new sets, continuing the existing numbering."""
        start = self.highest_repo_number + 1
        total = set_count * WORKSPACES_PER_SET
        return [f"{self.prefix}{num}" for num in range(start, start + total)]


def split_repo_url(repo_url: str) -> Optional[tuple[str, str]]:
    """``https://github.com/acme/specflow-workspace7`` → ``("acme", "specflow-workspace7")``."""
    trimmed = (repo_url or "").strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[:-4]
    parts = [p for p in trimmed.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def split_repo_name(repo_name: str) -> Optional[tuple[str, int]]:
    """``specflow-workspace7`` → ``("specflow-workspace", 7)``; None when unnumbered.

    The trailing number is what fixes a repo's position, and therefore its workspace id — see
    :func:`assign_pool_entries`.
    """
    digits = ""
    for char in reversed(repo_name or ""):
        if char.isdigit():
            digits = char + digits
        else:
            break
    if not digits:
        return None
    return repo_name[: len(repo_name) - len(digits)], int(digits)


def derive_naming_scheme(
    workspaces: List[Dict[str, Any]],
    default_github_org: Optional[str] = None,
    default_prefix: Optional[str] = None,
) -> Optional[PoolNamingScheme]:
    """Infer a pool's naming scheme from its existing workspaces.

    Uses the most common (org, prefix) pair so one hand-added oddity cannot redirect where new
    repos are created. Returns None for an empty pool unless defaults are supplied, in which
    case numbering starts from scratch.
    """
    combos: Dict[tuple[str, str], int] = {}
    highest_repo = 0
    highest_set = 0

    for ws in workspaces:
        highest_set = max(highest_set, int(ws.get("set_number") or 0))
        parsed_url = split_repo_url(str(ws.get("repo_url") or ""))
        if not parsed_url:
            continue
        org, repo = parsed_url
        parsed_name = split_repo_name(repo)
        if not parsed_name:
            continue
        prefix, number = parsed_name
        combos[(org, prefix)] = combos.get((org, prefix), 0) + 1
        highest_repo = max(highest_repo, number)

    if not combos:
        if not default_github_org or not default_prefix:
            return None
        return PoolNamingScheme(default_github_org, default_prefix, highest_repo, highest_set)

    # Most frequent wins; ties broken alphabetically so the result is deterministic.
    (org, prefix), _ = sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return PoolNamingScheme(org, prefix, highest_repo, highest_set)


def build_expansion_entries(
    scheme: PoolNamingScheme,
    repo_id_map: Dict[str, int],
    repo_names: List[str],
    workspace_pool: str,
) -> List[WorkspacePoolEntry]:
    """Build pool entries for newly provisioned repos, continuing existing set numbering.

    Set/index assignment mirrors :func:`assign_pool_entries` (3 per set, position-derived),
    but offset past the sets that already exist so nothing is renumbered.
    """
    entries: List[WorkspacePoolEntry] = []
    for offset, repo_name in enumerate(repo_names):
        set_number = scheme.next_set_number + (offset // WORKSPACES_PER_SET)
        workspace_index = (offset % WORKSPACES_PER_SET) + 1
        entries.append(
            WorkspacePoolEntry(
                workspace_id=f"ws-{set_number:02d}-{workspace_index}",
                repo_url=f"https://github.com/{scheme.github_org}/{repo_name}",
                p10y_repository_id=int(repo_id_map[repo_name]),
                workspace_pool=workspace_pool,
                set_number=set_number,
            )
        )
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
        set_number = (
            entry.set_number if entry.set_number is not None else (i // WORKSPACES_PER_SET) + 1
        )
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
