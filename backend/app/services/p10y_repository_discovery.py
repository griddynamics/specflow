"""P10Y/Compass repository discovery for newly provisioned workspace repos.

P10Y has **no create-repository endpoint**. A repository row is created by Compass itself
when it ingests the GitHub *connection* that owns the repo, so provisioning is inherently
eventual:

    create on GitHub → sync_repositories(connection) → poll until the git_url appears
    → enable_metrics(ids) → poll until each repo reports ready

Extracted from ``scripts/create_generation_session_repos.py`` so the bootstrap script and the
pool-expansion endpoint share one implementation of that dance. Progress is reported through
an optional ``on_progress`` callback rather than printed, so the script can pass ``print``
and the endpoint can stream into a job record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from app.services.p10y.p10y_api_client import P10YInternalAPIClient

logger = logging.getLogger(__name__)

LIVE_REPOSITORY_STATUS = "Live"
LIVE_INTERNAL_STATUS = 1  # internal_status value P10Y sets after enable/metrics succeeds

# How long to wait for Compass to surface brand-new repos after a connection re-fetch.
P10Y_REFETCH_POLL_SECONDS = 5
P10Y_REFETCH_TIMEOUT_SECONDS = 60

ProgressCallback = Optional[Callable[[str], Any]]


class P10YDiscoveryError(Exception):
    """Raised when repositories never become visible or ready in P10Y."""


def _report(on_progress: ProgressCallback, message: str) -> None:
    logger.info(message)
    if on_progress is not None:
        on_progress(message)


def normalize_git_url(git_url: str) -> str:
    """Reduce a P10Y ``git_url`` to a lowercase ``<org>/<name>`` tail for matching.

    Strips any scheme/host and a trailing ``.git`` so comparison is provider-format agnostic.
    """
    s = (git_url or "").strip().lower()
    if s.endswith(".git"):
        s = s[:-4]
    if "://" in s:
        s = s.split("://", 1)[1]
        s = s.split("/", 1)[1] if "/" in s else s
    return s


def repository_search(prefix: str, github_org: Optional[str]) -> str:
    """Build the narrowest P10Y repository search value available."""
    clean_prefix = (prefix or "").strip()
    clean_org = (github_org or "").strip().strip("/")
    if clean_prefix and clean_org:
        return f"{clean_org}/{clean_prefix}"
    return clean_prefix


def repository_id(repo_data: Dict[str, Any]) -> Optional[int]:
    """Extract a P10Y repository id, rejecting bools masquerading as ints."""
    raw = repo_data.get("id_repository", repo_data.get("id"))
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def repo_is_ready(status_dict: Dict[str, Any]) -> bool:
    """True when a repo can be used for estimation.

    P10Y sets ``status='Live'`` once metrics have fully processed, but ``internal_status=1``
    is set immediately after enable/metrics and is sufficient for provisioning.
    """
    return (
        status_dict.get("status") == LIVE_REPOSITORY_STATUS
        or (status_dict.get("internal_status") or 0) >= LIVE_INTERNAL_STATUS
    )


async def discover_repository_ids(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_names: List[str],
    search: Optional[str] = None,
    github_org: Optional[str] = None,
    on_progress: ProgressCallback = None,
) -> Dict[str, int]:
    """Map repository names to P10Y ids.

    P10Y's ``repository_name`` is the BARE repo name and is **not unique** within a Compass
    organisation — the same bare name can exist under several GitHub orgs, distinguished only
    by ``git_url``. Matching on the bare name lets a same-named repo from another org
    overwrite the correct id (last-write-wins), so when the owning org is known this matches
    on the fully-qualified ``git_url`` instead.
    """
    repos = await p10y_client.list_repositories_paginated(org_id, search=search)

    expected_by_git_url = (
        {normalize_git_url(f"{github_org}/{name}"): name for name in repo_names}
        if github_org
        else {}
    )
    repo_name_set = set(repo_names)

    repo_id_map: Dict[str, int] = {}
    for repo_data in repos:
        if expected_by_git_url:
            matched_name = expected_by_git_url.get(normalize_git_url(repo_data.get("git_url", "")))
            if matched_name is None:
                continue
        else:
            name = repo_data.get("repository_name", "")
            if name not in repo_name_set:
                continue
            matched_name = name

        found_id = repository_id(repo_data)
        if found_id is not None:
            repo_id_map[matched_name] = found_id

    missing = sorted(repo_name_set - set(repo_id_map))
    if missing:
        _report(on_progress, f"Not yet visible in P10Y: {', '.join(missing)}")
    return repo_id_map


async def trigger_repository_refetch(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    github_org: Optional[str],
    on_progress: ProgressCallback = None,
) -> None:
    """Trigger Compass's re-fetch on the connection(s) owning ``github_org``.

    A Compass connection is per GitHub org/account, not per repo, so any repo already
    ingested under ``github_org`` reveals the right connection — the brand-new repos being
    provisioned are never yet visible in P10Y (that is why this is being called), so matching
    only their exact names would always miss and force a broadcast re-fetch across every
    active GitHub connection instead of just the one that owns them.
    """
    search = github_org.strip().strip("/") if github_org else None
    repos = await p10y_client.list_repositories_paginated(org_id, search=search)
    org_prefix = f"{normalize_git_url(github_org)}/" if github_org else None

    conn_ids: set[int] = set()
    for repo_data in repos:
        git_url = normalize_git_url(repo_data.get("git_url", ""))
        if org_prefix is not None and not git_url.startswith(org_prefix):
            continue
        cid = (repo_data.get("_embedded", {}).get("connection") or {}).get("id_connection")
        if cid:
            conn_ids.add(cid)

    if not conn_ids:
        conns = (await p10y_client.list_connections(org_id)).get("data", [])
        conn_ids = {
            c["connection_id"]
            for c in conns
            if c.get("connection_type") == "github"
            and c.get("connection_status") == "active"
            and c.get("connection_id")
        }

    if not conn_ids:
        _report(on_progress, "No active GitHub connection found to re-fetch.")
        return

    for cid in sorted(conn_ids):
        await p10y_client.sync_repositories(org_id, connection_id=cid)
        _report(on_progress, f"Compass re-fetch triggered for connection {cid}")


async def await_repository_ids(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_names: List[str],
    search: Optional[str] = None,
    github_org: Optional[str] = None,
    timeout_seconds: int = P10Y_REFETCH_TIMEOUT_SECONDS,
    poll_seconds: int = P10Y_REFETCH_POLL_SECONDS,
    on_progress: ProgressCallback = None,
) -> Dict[str, int]:
    """Resolve ids for every name, re-fetching the connection if any are missing.

    Skipping the re-fetch is what previously let an expansion run seed a too-small pool: the
    new repos simply were not in Compass yet, so their ids came back empty and were silently
    dropped.

    Raises:
        P10YDiscoveryError: some repositories never appeared within the timeout.
    """
    repo_id_map = await discover_repository_ids(
        p10y_client, org_id, repo_names, search, github_org, on_progress
    )
    missing = [name for name in repo_names if name not in repo_id_map]
    if not missing:
        return repo_id_map

    _report(on_progress, f"Asking Compass to re-fetch {len(missing)} new repository(ies)")
    await trigger_repository_refetch(p10y_client, org_id, github_org, on_progress)

    deadline = time.monotonic() + timeout_seconds
    while missing and time.monotonic() < deadline:
        await asyncio.sleep(poll_seconds)
        repo_id_map = await discover_repository_ids(
            p10y_client, org_id, repo_names, search, github_org, on_progress
        )
        missing = [name for name in repo_names if name not in repo_id_map]

    if missing:
        raise P10YDiscoveryError(
            f"P10Y never surfaced these repositories within {timeout_seconds}s: "
            f"{', '.join(missing)}. They exist on GitHub — re-run expansion once Compass has "
            f"ingested them."
        )
    return repo_id_map


async def get_repository_statuses(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int],
    search: Optional[str] = None,
) -> Dict[int, Dict[str, Any]]:
    """Fetch current P10Y statuses for the target repository ids."""
    if not repo_ids:
        return {}

    repos = await p10y_client.list_repositories_paginated(org_id, search=search)
    target_ids = set(repo_ids)
    statuses: Dict[int, Dict[str, Any]] = {}
    for repo_data in repos:
        found_id = repository_id(repo_data)
        if found_id in target_ids:
            statuses[found_id] = {
                "status": repo_data.get("status"),
                "internal_status": repo_data.get("internal_status"),
                "last_checked": time.time(),
                "repo_name": repo_data.get("repository_name", f"ID:{found_id}"),
            }
    return statuses


def repository_ids_requiring_metrics(
    repo_ids: List[int], repo_statuses: Dict[int, Dict[str, Any]]
) -> List[int]:
    """Repo ids that are not yet ready in P10Y."""
    return [rid for rid in repo_ids if not repo_is_ready(repo_statuses.get(rid, {}))]


async def poll_repository_status(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int],
    timeout_minutes: int = 5,
    poll_interval: int = 15,
    search: Optional[str] = None,
    on_progress: ProgressCallback = None,
) -> Dict[int, Dict[str, Any]]:
    """Poll until every repository reports ready, or the timeout elapses.

    Returns the last-known statuses either way — the caller decides whether a not-yet-ready
    repository is fatal. A structural failure (e.g. pagination cap) is raised immediately
    rather than retried until timeout.
    """
    if not repo_ids:
        return {}

    _report(
        on_progress,
        f"Waiting for P10Y metrics on {len(repo_ids)} repository(ies) "
        f"(up to {timeout_minutes} min)",
    )

    start = time.monotonic()
    timeout_seconds = timeout_minutes * 60
    statuses: Dict[int, Dict[str, Any]] = {
        rid: {"status": "pending", "last_checked": None} for rid in repo_ids
    }

    while time.monotonic() - start <= timeout_seconds:
        try:
            repos = await p10y_client.list_repositories_paginated(org_id, search=search)
            for repo_data in repos:
                found_id = repository_id(repo_data)
                if found_id not in statuses:
                    continue
                previous = statuses[found_id].get("status")
                statuses[found_id] = {
                    "status": repo_data.get("status"),
                    "internal_status": repo_data.get("internal_status"),
                    "last_checked": time.time(),
                    "repo_name": repo_data.get("repository_name", f"ID:{found_id}"),
                }
                if previous != statuses[found_id]["status"]:
                    _report(
                        on_progress,
                        f"{statuses[found_id]['repo_name']}: "
                        f"{previous} -> {statuses[found_id]['status']}",
                    )

            if all(repo_is_ready(s) for s in statuses.values()):
                _report(on_progress, "All repositories are ready in P10Y")
                return statuses

            await asyncio.sleep(poll_interval)
        except RuntimeError:
            # Structural failure (e.g. pagination cap exceeded) — not transient.
            raise
        except Exception as exc:  # noqa: BLE001 - polling tolerates transient API errors
            _report(on_progress, f"Error polling P10Y status: {exc}")
            await asyncio.sleep(poll_interval)

    _report(on_progress, f"Timed out after {timeout_minutes} min waiting for P10Y metrics")
    return statuses


async def enable_metrics_and_wait(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int],
    search: Optional[str] = None,
    timeout_minutes: int = 5,
    poll_interval: int = 15,
    on_progress: ProgressCallback = None,
) -> Dict[int, Dict[str, Any]]:
    """Enable metrics for whichever repositories are not ready yet, then wait for them.

    Repositories already reporting ready are left alone, so re-running expansion after a
    partial failure does not re-trigger metric runs that already succeeded.
    """
    if not repo_ids:
        return {}

    statuses = await get_repository_statuses(p10y_client, org_id, repo_ids, search=search)
    pending = repository_ids_requiring_metrics(repo_ids, statuses)
    if not pending:
        _report(on_progress, "All repositories already have P10Y metrics enabled")
        return statuses

    _report(on_progress, f"Enabling P10Y metrics for {len(pending)} repository(ies)")
    await p10y_client.enable_metrics(org_id, pending)
    return await poll_repository_status(
        p10y_client,
        org_id,
        pending,
        timeout_minutes=timeout_minutes,
        poll_interval=poll_interval,
        search=search,
        on_progress=on_progress,
    )
