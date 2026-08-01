"""Grow the workspace pool by N sets: create repos, get them P10Y-ready, seed them.

Runs as a background job because the P10Y half is inherently slow — Compass has no
create-repository API, so a new repo only becomes usable after a connection re-fetch surfaces
it (~60s) and metrics are enabled (up to 5 min). A synchronous request would time out.

Every step is idempotent, which is what makes an in-memory job registry sufficient: GitHub
creation skips repos that exist, P10Y discovery matches on ``git_url``, and seeding skips
workspace ids already present. A backend restart mid-expansion is recovered by re-running it.

Ordering matters and is deliberate: **seeding happens last**. A workspace document is only
written once its repo exists on GitHub *and* has a P10Y id, so the pool never advertises a
slot that allocation would then fail to clone or estimation could not measure.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL, WORKSPACES_PER_SET
from app.database.interface import IDatabase
from app.schemas.workspace_pool_management import classify_removal
from app.services import p10y_repository_discovery as p10y_discovery
from app.services.github_repo_provisioner import (
    GitHubAPIClient,
    GitHubProvisioningError,
    provision_repositories,
)
from app.services.p10y.p10y_api_client import P10YInternalAPIClient
from app.services.workspace_pool_seeding import (
    PoolNamingScheme,
    WORKSPACES_COLLECTION,
    build_expansion_entries,
    derive_naming_scheme,
    seed_workspace_pool,
)

logger = logging.getLogger(__name__)

# Upper bound on one request, so a typo ("300" for "3") cannot create 900 repositories.
MAX_SETS_PER_EXPANSION = 20


class ExpansionPhase(str, Enum):
    """Where an expansion job has got to. Ordered as the job progresses."""

    QUEUED = "queued"
    CREATING_REPOS = "creating_repos"
    AWAITING_P10Y = "awaiting_p10y"
    ENABLING_METRICS = "enabling_metrics"
    SEEDING = "seeding"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ExpansionPhase.DONE, ExpansionPhase.FAILED)


class WorkspacePoolExpansionError(Exception):
    """Raised for configuration problems detected before any work starts."""


@dataclass
class ExpansionJob:
    """Live state of one expansion, polled by the client."""

    job_id: str
    workspace_pool: str
    sets_requested: int
    phase: ExpansionPhase = ExpansionPhase.QUEUED
    repo_names: List[str] = field(default_factory=list)
    repos_ready: int = 0
    workspaces_created: int = 0
    set_numbers: List[int] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def log(self, message: str) -> None:
        logger.info("[expand %s] %s", self.job_id, message)
        self.messages.append(message)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_pool": self.workspace_pool,
            "sets_requested": self.sets_requested,
            "phase": self.phase.value,
            "done": self.phase.is_terminal,
            "repo_names": list(self.repo_names),
            "repos_ready": self.repos_ready,
            "workspaces_created": self.workspaces_created,
            "set_numbers": list(self.set_numbers),
            "messages": list(self.messages),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class PoolExpansionRegistry:
    """Process-local registry of expansion jobs, mirroring ``GenerationTaskRegistry``.

    Deliberately in-memory: expansion is idempotent, so losing job state on restart costs a
    re-run, not correctness. Persisting it would mean a new SQLite table for no gain.

    Also enforces one live expansion per pool — two concurrent runs would derive the same
    "next free" repo numbers and collide.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, ExpansionJob] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def get(self, job_id: str) -> Optional[ExpansionJob]:
        return self._jobs.get(job_id)

    def active_for_pool(self, workspace_pool: str) -> Optional[ExpansionJob]:
        for job in self._jobs.values():
            if job.workspace_pool == workspace_pool and not job.phase.is_terminal:
                return job
        return None

    def register(self, job: ExpansionJob, task: asyncio.Task) -> None:
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = task

    def new_job(self, workspace_pool: str, sets_requested: int) -> ExpansionJob:
        return ExpansionJob(
            job_id=f"exp_{uuid.uuid4().hex[:12]}",
            workspace_pool=workspace_pool,
            sets_requested=sets_requested,
        )


def resolve_naming_scheme(
    workspaces: List[Dict[str, Any]],
    github_org: Optional[str] = None,
    repo_prefix: Optional[str] = None,
) -> PoolNamingScheme:
    """Work out where new repos go, preferring the pool's own convention.

    Raises:
        WorkspacePoolExpansionError: the pool is empty and no org is configured, so there is
            nothing to infer from and nowhere to create repositories.
    """
    scheme = derive_naming_scheme(
        workspaces,
        default_github_org=github_org or settings.GITHUB_ORG,
        default_prefix=repo_prefix or settings.WORKSPACE_REPO_PREFIX,
    )
    if scheme is None:
        raise WorkspacePoolExpansionError(
            "Cannot work out where to create workspace repositories: the pool is empty and "
            "GITHUB_ORG is not set. Set GITHUB_ORG (and optionally WORKSPACE_REPO_PREFIX), or "
            "seed the pool once with scripts/create_generation_session_repos.py."
        )
    return scheme


def validate_expansion_request(sets: int) -> None:
    """Reject an out-of-range set count before any GitHub call."""
    if sets < 1:
        raise WorkspacePoolExpansionError("Number of sets to add must be at least 1.")
    if sets > MAX_SETS_PER_EXPANSION:
        raise WorkspacePoolExpansionError(
            f"Refusing to add {sets} sets in one go (limit {MAX_SETS_PER_EXPANSION}, "
            f"= {MAX_SETS_PER_EXPANSION * WORKSPACES_PER_SET} repositories). Expand in smaller steps."
        )


def _require_p10y_config() -> tuple[str, str, int]:
    """Return (base_url, api_key, organisation_id) or explain what is missing."""
    base_url = settings.P10Y_BASE_URL
    api_key = settings.P10Y_API_KEY
    org_id = settings.P10Y_ORGANISATION_ID

    missing = [
        name
        for name, value in (
            ("P10Y_BASE_URL", base_url),
            ("P10Y_API_KEY", api_key),
            ("P10Y_ORGANISATION_ID", org_id),
        )
        if not value
    ]
    if missing:
        raise WorkspacePoolExpansionError(
            f"P10Y is not configured ({', '.join(missing)} unset). New workspaces need a P10Y "
            f"repository id to be measurable, so expansion cannot proceed."
        )
    # The guard above proves all three are set; bind them explicitly so the types are concrete.
    assert base_url is not None and api_key is not None and org_id is not None
    return base_url, api_key, int(org_id)


def _require_github_token() -> str:
    if not settings.GITHUB_TOKEN_DEFAULT:
        raise WorkspacePoolExpansionError(
            "GITHUB_TOKEN is not set, so workspace repositories cannot be created."
        )
    return settings.GITHUB_TOKEN_DEFAULT


async def expand_pool(
    db: IDatabase,
    job: ExpansionJob,
    github_org: Optional[str] = None,
    repo_prefix: Optional[str] = None,
    team_slug: Optional[str] = None,
    metrics_timeout_minutes: int = 5,
    github_client: Optional[GitHubAPIClient] = None,
    p10y_client: Optional[P10YInternalAPIClient] = None,
) -> ExpansionJob:
    """Run one expansion to completion, recording progress on ``job``.

    Never raises: any failure is recorded as ``phase=FAILED`` with ``error`` set, because the
    caller is a fire-and-forget background task whose only channel back is the job record.
    """
    job.started_at = datetime.now(timezone.utc)
    owns_github = github_client is None
    owns_p10y = p10y_client is None

    try:
        existing = db.query(
            WORKSPACES_COLLECTION, filters=[("workspace_pool", "==", job.workspace_pool)]
        )
        scheme = resolve_naming_scheme(existing, github_org, repo_prefix)
        job.repo_names = scheme.repo_names_for_sets(job.sets_requested)
        job.set_numbers = [
            scheme.next_set_number + offset for offset in range(job.sets_requested)
        ]
        job.log(
            f"Adding {job.sets_requested} set(s) to pool '{job.workspace_pool}' as "
            f"{scheme.github_org}/{job.repo_names[0]}..{job.repo_names[-1]} "
            f"(sets {job.set_numbers[0]}-{job.set_numbers[-1]})"
        )

        base_url, api_key, org_id = _require_p10y_config()
        token = _require_github_token()

        if github_client is None:
            github_client = GitHubAPIClient(token=token, owner=scheme.github_org)
        if p10y_client is None:
            # organisation_id is a per-call argument on this client, not constructor state.
            p10y_client = P10YInternalAPIClient(base_url=base_url, api_key=api_key)

        # 1. GitHub repositories.
        job.phase = ExpansionPhase.CREATING_REPOS
        provisioned = await provision_repositories(
            github_client,
            job.repo_names,
            team_slug=team_slug or settings.GITHUB_TEAM_SLUG,
            on_progress=job.log,
        )
        job.repos_ready = len(provisioned)

        # 2. Wait for Compass to see them and hand back ids.
        job.phase = ExpansionPhase.AWAITING_P10Y
        search = p10y_discovery.repository_search(scheme.prefix, scheme.github_org)
        repo_id_map = await p10y_discovery.await_repository_ids(
            p10y_client,
            org_id,
            job.repo_names,
            search=search,
            github_org=scheme.github_org,
            on_progress=job.log,
        )

        # 3. Enable metrics so P10Y can actually measure the new repos.
        job.phase = ExpansionPhase.ENABLING_METRICS
        await p10y_discovery.enable_metrics_and_wait(
            p10y_client,
            org_id,
            [repo_id_map[name] for name in job.repo_names],
            search=search,
            timeout_minutes=metrics_timeout_minutes,
            on_progress=job.log,
        )

        # 4. Only now publish the slots. replace=False so an existing set is never reset —
        #    overwriting a live document would drop an in-flight allocation back to available.
        job.phase = ExpansionPhase.SEEDING
        entries = build_expansion_entries(
            scheme, repo_id_map, job.repo_names, job.workspace_pool
        )
        result = seed_workspace_pool(db, entries, replace=False)
        job.workspaces_created = result.created
        if result.skipped:
            job.log(
                f"{result.skipped} workspace id(s) already existed and were left untouched"
            )
        job.log(f"Seeded {result.created} workspace(s); pool is ready")

        job.phase = ExpansionPhase.DONE
    except (
        WorkspacePoolExpansionError,
        GitHubProvisioningError,
        p10y_discovery.P10YDiscoveryError,
    ) as exc:
        job.phase = ExpansionPhase.FAILED
        job.error = str(exc)
        job.log(f"Expansion failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - background task: the job record is the only channel
        job.phase = ExpansionPhase.FAILED
        job.error = f"Unexpected failure: {exc}"
        logger.error("Pool expansion %s failed unexpectedly", job.job_id, exc_info=True)
        job.log(job.error)
    finally:
        job.finished_at = datetime.now(timezone.utc)
        if owns_github and github_client is not None:
            await github_client.close()
        if owns_p10y and p10y_client is not None:
            await p10y_client.close()

    return job


async def shrink_pool(
    db: IDatabase,
    workspace_ids: List[str],
    reason: str = "manual_shrink",
    confirmed_by: str = "operator",
) -> Dict[str, Any]:
    """Remove workspace slots from the pool. GitHub repositories are left untouched.

    Deliberately *not* a repo deletion: each workspace repo holds the
    ``archive/{generation_id}`` branches for every generation that ever ran on it — the only
    remote copy of that generated code (Steel Commandment I). Removing the pool row stops the
    set being allocated; re-running expansion re-adopts the same repos.

    Only clean, idle workspaces may be removed. Anything ALLOCATED, CLEANING, or STUCK is
    refused with an instruction to reclaim it first, so shrinking can never strand work.

    Returns:
        ``{total, success, failed, details}`` with the same shape as reclaim.
    """
    details: List[Dict[str, Any]] = []

    for workspace_id in workspace_ids:
        doc = db.get(WORKSPACES_COLLECTION, workspace_id)
        if doc is None:
            details.append(_shrink_result(workspace_id, False, "Workspace not found."))
            continue

        removal = classify_removal({**doc, "id": workspace_id})
        if not removal.removable:
            details.append(
                _shrink_result(
                    workspace_id, False, f"{removal.reason} — reclaim it first, then shrink."
                )
            )
            continue

        try:
            db.delete(WORKSPACES_COLLECTION, workspace_id)
        except Exception as exc:  # noqa: BLE001 - per-member failure must not abort the batch
            logger.error("Shrink of workspace %s failed: %s", workspace_id, exc, exc_info=True)
            details.append(_shrink_result(workspace_id, False, str(exc)))
            continue

        logger.info(
            "Workspace %s removed from pool (reason=%s, confirmed_by=%s); "
            "GitHub repo %s left intact",
            workspace_id,
            reason,
            confirmed_by,
            doc.get("repo_url"),
        )
        details.append(
            _shrink_result(
                workspace_id, True, "Removed from the pool; its GitHub repository is untouched."
            )
        )

    succeeded = sum(1 for d in details if d["success"])
    return {
        "total": len(details),
        "success": succeeded,
        "failed": len(details) - succeeded,
        "details": details,
    }


def _shrink_result(workspace_id: str, success: bool, message: str) -> Dict[str, Any]:
    return {"workspace_id": workspace_id, "success": success, "message": message}


def start_expansion(
    db: IDatabase,
    registry: PoolExpansionRegistry,
    sets: int,
    workspace_pool: str = DEFAULT_WORKSPACE_POOL,
    **kwargs: Any,
) -> ExpansionJob:
    """Validate, then launch an expansion as a background task and return its job.

    Raises:
        WorkspacePoolExpansionError: bad set count, or an expansion is already running for
            this pool (two runs would derive the same repo numbers and collide).
    """
    validate_expansion_request(sets)

    running = registry.active_for_pool(workspace_pool)
    if running is not None:
        raise WorkspacePoolExpansionError(
            f"An expansion is already running for pool '{workspace_pool}' "
            f"(job {running.job_id}, phase {running.phase.value}). Wait for it to finish."
        )

    job = registry.new_job(workspace_pool, sets)
    task = asyncio.create_task(expand_pool(db, job, **kwargs))
    registry.register(job, task)
    return job
