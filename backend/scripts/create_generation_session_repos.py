#!/usr/bin/env python3
"""
Create GitHub repositories for generation workspaces, optionally upsert workspaces into the
active database (sqlite, firestore, or an explicit hosted-Firestore target), and trigger P10y
metrics.

Example:
    uv run python scripts/create_generation_session_repos.py \
    --dry-run --github-org your-org \
    --start 1 --end 3 --prefix specflow-workspace \
    --gcp-project local-dev --firestore-database specflow \
    --workspace-pool default
    
    uv run python scripts/create_generation_session_repos.py \
    --github-org your-org --team your-team-slug \
    --start 4 --end 6 --prefix specflow-workspace \
    --gcp-project your-project --firestore-database your-database

This script:
1. Creates private GitHub repositories under an organization: {ORG}/{PREFIX}{NUMBER}
2. Grants a team in that org Write access (GitHub API permission "push") on each repo
3. Starts metric calculation for the created repositories via the P10y enable/metrics API
4. Polls P10y to check when repositories are live with metrics
5. Upserts the workspace pool into the active database (--gcp-project / --firestore-database
   target a hosted Firestore instance directly; otherwise the active DATABASE_TYPE is used —
   sqlite by default)

Further usage:
    python scripts/create_generation_session_repos.py --github-org MyOrg --team my-team-slug --start 7 --end 9 \\
      --gcp-project my-project --firestore-database default

Environment (optional defaults for flags):
    When both --gcp-project and --firestore-database are set, writes target that hosted
    Firestore instance only (not Settings / not DATABASE_TYPE). Otherwise use get_database(),
    which honors DATABASE_TYPE (sqlite by default; set to firestore + GCP_PROJECT_ID /
    FIRESTORE_DATABASE_NAME in .env or the shell to write against a hosted instance instead).
    GITHUB_ORG or GITHUB_ORG_DEFAULT — organization login (owner of repos)
    GITHUB_TEAM or GITHUB_TEAM_SLUG — team slug within that org
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import httpx


# Add backend to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

print(f"PROJECT_ROOT: {PROJECT_ROOT}")
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent))


# Load .env file explicitly from project root
dotenv_path = PROJECT_ROOT / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

from app.core.enums import DatabaseType  # noqa: E402
from app.database.factory import get_database  # noqa: E402
from app.database.firestore import FirestoreDatabase  # noqa: E402
from app.database.interface import IDatabase  # noqa: E402
from app.services.p10y.p10y_api_client import P10YInternalAPIClient  # noqa: E402
from app.services.workspace_pool_seeding import (  # noqa: E402
    assign_pool_entries,
    seed_workspace_pool,
)

LIVE_REPOSITORY_STATUS = "Live"
LIVE_INTERNAL_STATUS = 1  # internal_status value P10Y sets after enable/metrics succeeds

P10Y_REFETCH_POLL_SECONDS = 5
P10Y_REFETCH_TIMEOUT_SECONDS = 60

def _repo_is_ready(status_dict: Dict[str, Any]) -> bool:
    """Return True when a repo is ready for estimation.

    P10Y sets status='Live' once metrics have fully processed, but internal_status=1
    is set immediately after enable/metrics and is sufficient for provisioning.
    """
    return (
        status_dict.get("status") == LIVE_REPOSITORY_STATUS
        or (status_dict.get("internal_status") or 0) >= LIVE_INTERNAL_STATUS
    )

# Set DATABASE_TYPE early if FIRESTORE_EMULATOR_HOST is set
if os.getenv("FIRESTORE_EMULATOR_HOST") and not os.getenv("DATABASE_TYPE"):
    os.environ["DATABASE_TYPE"] = "emulator"


def refresh_settings_singleton() -> None:
    """Reload Settings from environment (.env already loaded) and rebind the DB factory module."""
    import app.core.config as config_module
    import app.database.factory as db_factory

    config_module.settings = config_module.Settings()
    db_factory.settings = config_module.settings


class GitHubAPIClient:
    """Client for GitHub API operations (organization or personal-account repositories)."""

    def __init__(self, token: str, org: str):
        """
        Initialize GitHub API client.

        Args:
            token: GitHub personal access token (needs repo + org/team scopes as appropriate)
            org: GitHub organization login or personal account login (repository owner / namespace)
        """
        self.token = token
        self.org = org
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        self.client = httpx.AsyncClient(timeout=30.0)
        self._is_user_account: Optional[bool] = None

    async def get_authenticated_user(self) -> Dict[str, Any]:
        """
        Get the authenticated user's information.

        Returns:
            User data from GitHub API
        """
        url = f"{self.base_url}/user"
        response = await self.client.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def _resolve_owner_type(self) -> bool:
        """Return True if self.org is a personal user account (not an org)."""
        if self._is_user_account is None:
            url = f"{self.base_url}/users/{self.org}"
            response = await self.client.get(url, headers=self.headers)
            response.raise_for_status()
            self._is_user_account = response.json().get("type") == "User"
        assert self._is_user_account is not None
        return self._is_user_account

    async def create_repository(self, repo_name: str) -> Dict[str, Any]:
        """
        Create a private repository under the configured owner (org or personal account).

        Args:
            repo_name: Name of the repository to create

        Returns:
            Repository data from GitHub API
        """
        is_user = await self._resolve_owner_type()
        if is_user:
            url = f"{self.base_url}/user/repos"
        else:
            url = f"{self.base_url}/orgs/{self.org}/repos"
        data = {
            "name": repo_name,
            "private": True,
            "auto_init": True,
            "description": f"Generation workspace repository: {repo_name}",
        }

        response = await self.client.post(url, json=data, headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def repository_exists(self, repo_name: str) -> bool:
        """
        Check if a repository already exists under the organization.

        Args:
            repo_name: Name of the repository

        Returns:
            True if repository exists, False otherwise
        """
        url = f"{self.base_url}/repos/{self.org}/{repo_name}"
        response = await self.client.get(url, headers=self.headers)
        return response.status_code == 200

    async def add_team_repository_write(self, team_slug: str, repo_name: str) -> None:
        """
        Grant a team Write access on a repository (GitHub REST permission 'push').

        PUT /orgs/{org}/teams/{team_slug}/repos/{org}/{repo_name}
        """
        url = (
            f"{self.base_url}/orgs/{self.org}/teams/{team_slug}/repos/"
            f"{self.org}/{repo_name}"
        )
        data = {"permission": "push"}
        response = await self.client.put(url, json=data, headers=self.headers)
        response.raise_for_status()

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


async def create_github_repositories(
    github_client: GitHubAPIClient,
    prefix: str,
    start_num: int,
    end_num: int,
    team_slug: str | None,
    delay: float = 0.1,
) -> List[Dict[str, Any]]:
    """
    Create multiple GitHub repositories with sequential numbering under the client's org.

    Args:
        github_client: Initialized GitHub API client (org is github_client.org)
        prefix: Repository name prefix (e.g., "generation-workspace")
        start_num: Starting number (inclusive)
        end_num: Ending number (inclusive)
        team_slug: If set, grant this team Write access (push) on each repo
        delay: Delay between requests in seconds (default: 0.1)

    Returns:
        List of created repository data
    """
    created_repos = []
    org = github_client.org

    for num in range(start_num, end_num + 1):
        repo_name = f"{prefix}{num}"

        # Check if repo already exists
        if await github_client.repository_exists(repo_name):
            print(f"⚠️  Repository '{repo_name}' already exists, skipping creation")
            # Still add to list for P10y sync
            created_repos.append({
                "name": repo_name,
                "full_name": f"{org}/{repo_name}",
                "html_url": f"https://github.com/{org}/{repo_name}",
                "already_existed": True,
            })
        else:
            try:
                print(f"📦 Creating repository: {org}/{repo_name}")
                repo_data = await github_client.create_repository(repo_name)
                created_repos.append(repo_data)
                print(f"✅ Created: {repo_data['html_url']}")
            except httpx.HTTPStatusError as e:
                print(f"❌ Failed to create {repo_name}: {e}")
                print(f"   Response: {e.response.text}")
                raise

        if team_slug:
            try:
                await github_client.add_team_repository_write(team_slug, repo_name)
                print(f"   👥 Team '{team_slug}' granted Write on {repo_name}")
            except httpx.HTTPStatusError as e:
                print(f"❌ Failed to add team {team_slug} to {repo_name}: {e}")
                print(f"   Response: {e.response.text}")
                raise

        # Add delay to avoid rate limiting
        if num < end_num:
            await asyncio.sleep(delay)

    return created_repos


def print_dry_run_plan(
    *,
    token_login: str,
    github_org: str,
    team_slug: str | None,
    skip_team: bool,
    skip_github: bool,
    prefix: str,
    start_num: int,
    end_num: int,
) -> None:
    """Print planned GitHub org, team, token actor, and full repo URLs (no API calls)."""
    print("\n" + "=" * 80)
    print("🔍 DRY RUN — GitHub stage only (no repos created, no team grants, no P10y/Firestore)")
    print("=" * 80)
    print(
        "   Token authenticates as: "
        f"{token_login}\n"
        "   (This identity must have permission to create repos in the org and manage team access.)"
    )
    print(f"   Organization (repo owner): {github_org}")
    if skip_github:
        print("   Team Write (push): — (--skip-github; would not run GitHub API)")
    elif skip_team:
        print("   Team Write (push): — (--skip-team)")
    elif team_slug:
        print(f"   Team Write (push): {team_slug}")
    else:
        print("   Team Write (push): —")
    print()
    print("   Repository full URLs (same as Firestore repo_url):")
    for num in range(start_num, end_num + 1):
        repo_name = f"{prefix}{num}"
        print(f"      https://github.com/{github_org}/{repo_name}")
    print()
    if skip_github:
        print("   Would skip: POST /orgs/{org}/repos (no new repositories)")
    else:
        print(f"   Would create {end_num - start_num + 1} private repos: POST /orgs/{github_org}/repos")
        if not skip_team and team_slug:
            print(
                f"   Would grant team '{team_slug}' Write on each: "
                f"PUT /orgs/{github_org}/teams/{team_slug}/repos/{github_org}/<repo>"
            )
        elif skip_team:
            print("   Would skip: team repository permission updates (--skip-team)")
    print("=" * 80)


def _normalize_git_url(git_url: str) -> str:
    """Reduce a P10Y ``git_url`` to a lowercase ``<org>/<name>`` tail for matching.

    Strips any scheme/host (``https://github.com/org/name``) and a trailing
    ``.git`` so comparison is provider-format agnostic.
    """
    s = (git_url or "").strip().lower()
    if s.endswith(".git"):
        s = s[:-4]
    if "://" in s:
        s = s.split("://", 1)[1]
        s = s.split("/", 1)[1] if "/" in s else s
    return s


async def get_repository_ids(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_names: List[str],
    prefix: str,
    github_org: Optional[str] = None,
) -> Dict[str, int]:
    """
    Get P10y repository IDs for the given repository names.

    Args:
        p10y_client: Initialized P10y API client
        org_id: P10y organization ID
        repo_names: List of repository names to find
        prefix: Search prefix for list_repositories
        github_org: GitHub org owning the repos. When set, matching is done on
            ``git_url`` (``<org>/<name>``) rather than the bare ``repository_name``.
    Returns:
        Dictionary mapping repository names to their P10y IDs
    """
    print("\n🔍 Looking up P10y repository IDs")

    # Fetch all repositories
    repos_response = await p10y_client.list_repositories(
        organisation_id=org_id,
        search=prefix,
        page_size=1000,  # Should be enough for our case,
    )

    # P10Y `repository_name` is the BARE repo name and is NOT unique within a Compass
    # organisation — the same bare name can exist under several GitHub orgs, distinguished
    # only by `git_url` (`<org>/<name>`). Matching on the bare name lets a same-named repo
    # from a different org overwrite the correct ID (last-write-wins). When the owning org
    # is known, match on the fully-qualified git_url instead.
    expected_by_git_url = (
        {_normalize_git_url(f"{github_org}/{name}"): name for name in repo_names}
        if github_org
        else {}
    )
    repo_name_set = set(repo_names)

    repo_id_map: Dict[str, int] = {}
    for repo_data in repos_response.get("data", []):
        if expected_by_git_url:
            matched_name = expected_by_git_url.get(
                _normalize_git_url(repo_data.get("git_url", ""))
            )
            if matched_name is None:
                continue
        else:
            repo_name = repo_data.get("repository_name", "")
            if repo_name not in repo_name_set:
                continue
            matched_name = repo_name

        repo_id = repo_data.get("id")
        repo_id_map[matched_name] = repo_id
        print(f"   {matched_name} -> ID {repo_id} ({repo_data.get('git_url', '?')})")

    # Check if we found all repos
    missing_repos = set(repo_names) - set(repo_id_map.keys())
    if missing_repos:
        print(f"⚠️  Could not find P10y IDs for: {', '.join(missing_repos)}")

    return repo_id_map


async def trigger_repository_refetch(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    github_org: Optional[str],
    repo_names: List[str],
) -> None:
    """Trigger Compass's 'Re-fetch' on the connection(s) owning the workspace repos; omitting connection_id may return 400 but the sync still completes."""
    repos = (await p10y_client.list_repositories(organisation_id=org_id, page_size=1000)).get("data", [])
    expected = (
        {_normalize_git_url(f"{github_org}/{name}") for name in repo_names} if github_org else None
    )
    conn_ids: set[int] = set()
    for repo_data in repos:
        if expected is not None and _normalize_git_url(repo_data.get("git_url", "")) not in expected:
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
        print("   ⚠️  No active GitHub connection found to re-fetch.")
        return

    for cid in sorted(conn_ids):
        await p10y_client.sync_repositories(org_id, connection_id=cid)
        print(f"   ✅ Re-fetch triggered for connection {cid}.")


def _p10y_repository_id(repo_data: Dict[str, Any]) -> Optional[int]:
    repo_id = repo_data.get("id_repository", repo_data.get("id"))
    if isinstance(repo_id, bool) or repo_id is None:
        return None
    try:
        return int(repo_id)
    except (TypeError, ValueError):
        return None


async def get_repository_statuses(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch current P10Y statuses for the target repository IDs."""
    if not repo_ids:
        return {}

    repos_response = await p10y_client.list_repositories(
        organisation_id=org_id,
        page_size=1000,
    )

    target_ids = set(repo_ids)
    statuses: Dict[int, Dict[str, Any]] = {}
    for repo_data in repos_response.get("data", []):
        repo_id = _p10y_repository_id(repo_data)
        if repo_id in target_ids:
            statuses[repo_id] = {
                "status": repo_data.get("status"),
                "internal_status": repo_data.get("internal_status"),
                "last_checked": time.time(),
                "repo_name": repo_data.get("repository_name", f"ID:{repo_id}"),
            }
    return statuses


def repository_ids_requiring_metrics(
    repo_ids: List[int],
    repo_statuses: Dict[int, Dict[str, Any]],
) -> List[int]:
    """Return repo IDs that are not yet ready in P10Y."""
    return [
        repo_id
        for repo_id in repo_ids
        if not _repo_is_ready(repo_statuses.get(repo_id, {}))
    ]


async def start_metrics_calculation(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int]
) -> None:
    """
    Start metric calculation for repositories.
    
    Args:
        p10y_client: Initialized P10y API client
        org_id: P10y organization ID
        repo_ids: List of P10y repository IDs
    """
    if not repo_ids:
        print("\n⚠️  No repository IDs to start metrics for")
        return
    
    print(f"\n📊 Starting metrics calculation for {len(repo_ids)} repositories")
    print(f"   Repository IDs: {repo_ids}")
    
    try:
        result = await p10y_client.enable_metrics(org_id, repo_ids)
        print("✅ Metrics calculation started successfully")
        if result:
            print(f"   Response: {result}")
    except Exception as e:
        print(f"❌ Failed to start metrics calculation: {e}")
        raise


async def poll_repository_status(
    p10y_client: P10YInternalAPIClient,
    org_id: int,
    repo_ids: List[int],
    timeout_minutes: int = 5,
    poll_interval: int = 15
) -> Dict[int, Dict[str, Any]]:
    """
    Poll P10y to check when repositories become live with metrics.
    
    Args:
        p10y_client: Initialized P10y API client
        org_id: P10y organization ID
        repo_ids: List of P10y repository IDs to monitor
        timeout_minutes: Maximum time to poll in minutes (default: 5)
        poll_interval: Seconds between polls (default: 15)
        
    Returns:
        Dictionary mapping repository IDs to their status information
    """
    if not repo_ids:
        print("\n⚠️  No repository IDs to poll")
        return {}
    
    print(f"\n⏱️  Polling repository status (timeout: {timeout_minutes} minutes)")
    print(f"   Checking every {poll_interval} seconds")
    
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    repo_statuses = {repo_id: {"status": "pending", "last_checked": None} for repo_id in repo_ids}
    
    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout_seconds:
            print(f"\n⏱️  Timeout reached ({timeout_minutes} minutes)")
            break
        
        try:
            # Fetch repository details
            repos_response = await p10y_client.list_repositories(
                organisation_id=org_id,
                page_size=1000
            )
            
            # Update status for our repos
            for repo_data in repos_response.get("data", []):
                repo_id = _p10y_repository_id(repo_data)
                if repo_id in repo_ids:
                    internal_status = repo_data.get("internal_status")
                    status_name = repo_data.get("status")
                    repo_name = repo_data.get("repository_name", f"ID:{repo_id}")
                    
                    # Update status
                    old_status = repo_statuses[repo_id].get("status")
                    repo_statuses[repo_id] = {
                        "status": status_name,
                        "internal_status": internal_status,
                        "last_checked": time.time(),
                        "repo_name": repo_name
                    }
                    
                    # Print status change
                    if old_status != status_name:
                        status_emoji = "🟢" if _repo_is_ready(repo_statuses[repo_id]) else "🟡"
                        print(f"   {status_emoji} {repo_name}: {old_status} -> {status_name} (internal: {internal_status})")

            # Check if all repos are ready
            all_live = all(_repo_is_ready(s) for s in repo_statuses.values())

            if all_live:
                print("\n✅ All repositories are ready!")
                break
            
            # Wait before next poll
            await asyncio.sleep(poll_interval)
            
        except Exception as e:
            print(f"⚠️  Error polling status: {e}")
            await asyncio.sleep(poll_interval)
    
    return repo_statuses


async def add_workspaces_to_firestore(
    repo_id_map: Dict[str, int],
    github_org: str,
    prefix: str,
    start_num: int,
    workspace_pool: str = "default",
    firestore_project_id: Optional[str] = None,
    firestore_database_id: Optional[str] = None,
) -> None:
    """
    Add workspace entries to the active database.

    If firestore_project_id and firestore_database_id are both set, target that hosted Firestore
    instance directly; otherwise use get_database() (honors DATABASE_TYPE — sqlite by default).

    Id assignment and the upsert are delegated to app.services.workspace_pool_seeding, the single
    source shared with init_db.py. ``start_num`` is unused (ids come from the repo names) and is
    retained only for call-site stability.
    """
    if not repo_id_map:
        print("\n⚠️  No repository IDs to add")
        return

    print(f"\n📝 Adding {len(repo_id_map)} workspaces to the database")

    try:
        if firestore_project_id is not None and firestore_database_id is not None:
            db: IDatabase = FirestoreDatabase(
                project_id=firestore_project_id,
                database=firestore_database_id,
            )
            print(
                f"   Using Firestore from CLI: project={firestore_project_id!r} "
                f"database={firestore_database_id!r}"
            )
        else:
            db = get_database()

        entries = assign_pool_entries(
            repo_id_map, github_org, workspace_pool, prefix=prefix
        )
        result = seed_workspace_pool(db, entries, replace=True)

        print("\n✅ Database workspace sync complete:")
        print(f"   Created: {result.created}")
        print(f"   Updated: {result.updated}")
        print(f"   Total: {result.total}")

    except Exception as e:
        print(f"\n❌ Failed to add workspaces to the database: {e}")
        import traceback
        traceback.print_exc()
        raise


def emit_workspace_config(
    repo_id_map: Dict[str, int],
    github_org: str,
    prefix: str,
    workspace_pool: str,
    output_path: str,
    ordered_repos: Optional[List[str]] = None,
) -> None:
    """
    Write a JSON workspace-config file in the exact schema consumed by
    ``init_db.py --workspace-config``:

        [{"workspace_id": str, "repo_url": str,
          "p10y_repository_id": int, "workspace_pool": str}, ...]

    Id assignment is delegated to app.services.workspace_pool_seeding.assign_pool_entries (the
    same routine that seeds the DB directly), so the file schema and the direct-seed path can
    never drift. When ordered_repos is provided (the --repos path), ids follow list position;
    otherwise they are derived from the {prefix}{num} repo names.
    """
    entries = assign_pool_entries(
        repo_id_map,
        github_org,
        workspace_pool,
        ordered_repos=ordered_repos,
        prefix=prefix,
    )
    serialised = [
        {
            "workspace_id": e.workspace_id,
            "repo_url": e.repo_url,
            "p10y_repository_id": e.p10y_repository_id,
            "workspace_pool": e.workspace_pool,
        }
        for e in entries
    ]

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(serialised, f, indent=2)
    print(f"\n✅ Workspace config written to: {output_path} ({len(serialised)} entries)")


async def main():
    """Main script execution."""
    parser = argparse.ArgumentParser(
        description="Create GitHub repositories for generation workspaces and trigger P10y metrics"
    )
    parser.add_argument(
        "--start",
        type=int,
        required=False,
        default=None,
        help="Starting number for repository sequence (e.g., 7). Required unless --repos is provided."
    )
    parser.add_argument(
        "--end",
        type=int,
        required=False,
        default=None,
        help="Ending number for repository sequence (e.g., 9). Required unless --repos is provided."
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="generation-workspace",
        help="Repository name prefix (default: generation-workspace)"
    )
    parser.add_argument(
        "--gcp-project",
        type=str,
        default=None,
        metavar="PROJECT_ID",
        help=(
            "GCP project ID. If both this and --firestore-database are set, workspace writes use "
            "only these flags (not Settings or DATABASE_TYPE)."
        ),
    )
    parser.add_argument(
        "--firestore-database",
        type=str,
        default=None,
        metavar="DATABASE_ID",
        help=(
            "Firestore database ID. If both this and --gcp-project are set, workspace writes use "
            "only these flags (not Settings or DATABASE_TYPE)."
        ),
    )
    parser.add_argument(
        "--github-org",
        type=str,
        default=os.getenv("GITHUB_ORG") or os.getenv("GITHUB_ORG_DEFAULT"),
        metavar="ORG",
        help="GitHub organization login; repos are ORG/{PREFIX}N. Env: GITHUB_ORG, GITHUB_ORG_DEFAULT",
    )
    parser.add_argument(
        "--team",
        type=str,
        default=os.getenv("GITHUB_TEAM_SLUG") or os.getenv("GITHUB_TEAM"),
        metavar="TEAM_SLUG",
        help=(
            "Optional team slug inside that org; when set, each repo gets team Write "
            "(REST permission push). Omit to skip team grants. Env: GITHUB_TEAM_SLUG, GITHUB_TEAM"        ),
    )
    parser.add_argument(
        "--skip-team",
        action="store_true",
        help="Do not grant team access (omit team assignment even if --team is set)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between GitHub API requests in seconds (default: 0.1)"
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=5,
        help="Timeout for polling P10y status in minutes (default: 5)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Interval between P10y status polls in seconds (default: 15)"
    )
    parser.add_argument(
        "--repos",
        type=str,
        default=None,
        metavar="REPO_LIST",
        help=(
            "Comma-separated list of existing repository names (bare names, without org prefix) "
            "to use instead of the --start/--end range. Implies --skip-github and --skip-metrics. "
            "Workspace IDs are assigned by position (first 3 → ws-01-{1,2,3}, etc.)."
        ),
    )
    parser.add_argument(
        "--skip-github",
        action="store_true",
        help="Skip GitHub repository creation (only look up IDs and start metrics)"
    )
    parser.add_argument(
        "--skip-firestore",
        action="store_true",
        help="Skip adding workspaces to the active database (sqlite/firestore)"
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Skip starting metrics calculation"
    )
    parser.add_argument(
        "--workspace-pool",
        type=str,
        default=None,
        help="Workspace pool name (default: 'default')"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print token actor, org, team, and full repo URLs; exit before any GitHub mutations, "
            "P10y, or database writes (P10Y_* env not required)"
        ),
    )
    parser.add_argument(
        "--output-workspace-config",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "After repo_id_map resolves (Step 3), write a JSON workspace-config file at FILE "
            "in the exact schema consumed by init_db.py --workspace-config: "
            "[{workspace_id, repo_url, p10y_repository_id (int), workspace_pool}, ...]. "
            "Does not write to Firestore directly."
        ),
    )

    args = parser.parse_args()

    refresh_settings_singleton()

    import app.core.config as config_module

    cfg = config_module.settings

    workspace_pool = args.workspace_pool.lower() if args.workspace_pool else "default"
    github_org = (args.github_org or "").strip() or None
    team_slug = None if args.skip_team else ((args.team or "").strip() or None)

    gcp_cli = (args.gcp_project or "").strip() or None
    fsdb_cli = (args.firestore_database or "").strip() or None
    firestore_target_from_cli = bool(gcp_cli and fsdb_cli)

    # Validate arguments
    if args.start > args.end:
        print("❌ Error: start number must be less than or equal to end number")
        sys.exit(1)

    if args.dry_run and not github_org:
        print(
            "❌ Error: --dry-run requires --github-org (or GITHUB_ORG / GITHUB_ORG_DEFAULT) "
            "to list repository URLs"
        )
        sys.exit(1)

    if not args.skip_github:
        if not github_org:
            print(
                "❌ Error: --github-org is required to create repositories "
                "(or set GITHUB_ORG / GITHUB_ORG_DEFAULT)"
            )
            sys.exit(1)

    if not args.dry_run and not args.skip_firestore and not github_org:
        print(
            "❌ Error: --github-org is required to record workspace repo URLs "
            "(or set GITHUB_ORG / GITHUB_ORG_DEFAULT)"
        )
        sys.exit(1)

    if not args.skip_firestore and not args.dry_run:
        if firestore_target_from_cli:
            pass
        else:
            # These write real GitHub-backed workspace repos into the active database — reject
            # only DatabaseType.MEMORY (throwaway, non-persistent). sqlite (local default) and
            # firestore (production / hosted-GCP) are both valid persistent targets.
            if cfg.DATABASE_TYPE == DatabaseType.MEMORY:
                print(
                    "❌ Error: DATABASE_TYPE must not be memory (throwaway) when writing real "
                    "workspace repos — use sqlite (default), firestore, or pass both "
                    "--gcp-project and --firestore-database for direct Firestore writes"
                )
                sys.exit(1)
            if cfg.DATABASE_TYPE == DatabaseType.FIRESTORE and not (cfg.GCP_PROJECT_ID or "").strip():
                print(
                    "❌ Error: GCP_PROJECT_ID must be set when DATABASE_TYPE=firestore "
                    "(or pass both --gcp-project and --firestore-database for direct writes)"
                )
                sys.exit(1)

    # Check required environment variables
    github_token = cfg.GITHUB_TOKEN_DEFAULT
    p10y_api_key = cfg.P10Y_API_KEY  # gitleaks:allow - variable assignment, not a literal
    p10y_base_url = cfg.P10Y_BASE_URL
    p10y_org_id = cfg.P10Y_ORGANISATION_ID
    git_username = cfg.GIT_USER_NAME_DEFAULT

    if not args.dry_run and not github_token:
        print("❌ Error: GITHUB_TOKEN_DEFAULT (or legacy GITHUB_TOKEN) not set in environment")
        sys.exit(1)

    if not args.dry_run:
        if not p10y_api_key:
            print("❌ Error: P10Y_API_KEY not set in environment")
            sys.exit(1)

        if not p10y_org_id:
            print("❌ Error: P10Y_ORGANISATION_ID not set in environment")
            sys.exit(1)

    if args.dry_run and not github_token:
        print(
            "⚠️  GITHUB_TOKEN_DEFAULT not set — resolve token actor via "
            "GIT_USER_NAME_DEFAULT or add a token for GET /user"
        )

    # Optional: resolve token owner's login for logging (org repos do not use this as owner)
    if not git_username and github_token:
        print("⚠️  GIT_USER_NAME_DEFAULT not set, fetching token owner from GitHub API...")
        try:
            temp_client = GitHubAPIClient(github_token, "_")
            user_data = await temp_client.get_authenticated_user()
            git_username = user_data.get("login")
            await temp_client.close()
            if git_username:
                print(f"✅ Detected GitHub login for token: {git_username}")
        except Exception as e:
            print(f"⚠️  Could not fetch GitHub user for token: {e}")
            git_username = "(unknown)"
    elif not git_username:
        git_username = "(not resolved; set GIT_USER_NAME_DEFAULT or GITHUB_TOKEN_DEFAULT)"

    if args.repos is None and (args.start is None or args.end is None):
        parser.error("--start and --end are required unless --repos is provided")

    print("=" * 80)
    title = "🚀 Generation Workspace Repository Setup"
    if args.dry_run:
        title += " — DRY RUN"
    print(title)
    print("=" * 80)
    if args.repos:
        own_repo_list = [r.strip() for r in args.repos.split(",") if r.strip()]
        print(f"   Repos (provided): {', '.join(own_repo_list)}")
        print(f"   Count: {len(own_repo_list)} repositories")
    else:
        own_repo_list = None
        print(f"   Prefix: {args.prefix}")
        print(f"   Range: {args.start} to {args.end}")
        print(f"   Count: {args.end - args.start + 1} repositories")
    print(f"   GitHub org (repo owner): {github_org or '—'}")
    print(f"   Team Write (slug): {team_slug or '—'}")
    print(f"   Token login (info): {git_username}")
    if not args.dry_run:
        print(f"   P10y Org ID: {p10y_org_id}")
    print(f"   Workspace Pool: {workspace_pool}")
    if firestore_target_from_cli:
        print(f"   GCP project (Firestore, CLI): {gcp_cli}")
        print(f"   Firestore database (CLI): {fsdb_cli}")
    else:
        print(f"   GCP project (Firestore): {cfg.GCP_PROJECT_ID or '—'}")
        print(f"   Firestore database: {cfg.FIRESTORE_DATABASE_NAME}")
        if not args.dry_run:
            print(f"   DATABASE_TYPE: {cfg.DATABASE_TYPE}")
    print("=" * 80)

    if args.dry_run:
        print_dry_run_plan(
            token_login=git_username,
            github_org=github_org or "",
            team_slug=team_slug,
            skip_team=args.skip_team,
            skip_github=args.skip_github,
            prefix=args.prefix,
            start_num=args.start,
            end_num=args.end,
        )
        print("\n✅ Dry run finished — exited before GitHub API calls, P10y, and Firestore.")
        return

    github_client = GitHubAPIClient(github_token, github_org or "_")
    p10y_client = P10YInternalAPIClient(base_url=p10y_base_url, api_key=p10y_api_key)
    
    try:
        # Step 1: Create GitHub repositories
        if own_repo_list is not None:
            # --repos path: repos already exist, skip creation entirely
            print("\n⏭️  Skipping GitHub repository creation (--repos provided)")
            created_repos = []
            repo_names = own_repo_list
        elif not args.skip_github:
            created_repos = await create_github_repositories(
                github_client,
                args.prefix,
                args.start,
                args.end,
                team_slug,
                args.delay,
            )
            print(f"\n✅ Created/found {len(created_repos)} repositories")
            repo_names = [f"{args.prefix}{num}" for num in range(args.start, args.end + 1)]
        else:
            print("\n⏭️  Skipping GitHub repository creation")
            created_repos = []
            repo_names = [f"{args.prefix}{num}" for num in range(args.start, args.end + 1)]

        # Step 2: Get P10y repository IDs
        # For --repos, search with empty prefix to match arbitrary names across the full org.
        p10y_search_prefix = "" if own_repo_list is not None else args.prefix
        repo_id_map = await get_repository_ids(
            p10y_client, p10y_org_id, repo_names, p10y_search_prefix, github_org
        )

        # Newly created GitHub repos are invisible to P10Y until Compass re-fetches the
        # connection that owns them. When some are missing, trigger a re-fetch, wait, and
        # look up again (twice). Failing to do this is what let an expansion run (K=1 → K=3)
        # silently write a too-small workspaces.json and under-seed Firestore.
        missing = [r for r in repo_names if r not in repo_id_map]
        if missing:
            print(f"\n🔄 {len(missing)} repo(s) not in P10Y yet: {', '.join(missing)} — triggering re-fetch ...")
            await trigger_repository_refetch(p10y_client, p10y_org_id, github_org, repo_names)

            deadline = time.time() + P10Y_REFETCH_TIMEOUT_SECONDS
            while missing and time.time() < deadline:
                print(f"   ⏱️  {len(missing)} still missing; re-checking in {P10Y_REFETCH_POLL_SECONDS}s ...")
                await asyncio.sleep(P10Y_REFETCH_POLL_SECONDS)
                repo_id_map = await get_repository_ids(
                    p10y_client, p10y_org_id, repo_names, p10y_search_prefix, github_org
                )
                missing = [r for r in repo_names if r not in repo_id_map]

            if missing:
                print(f"\n❌ Could not resolve P10Y IDs for {len(missing)} repo(s): {', '.join(missing)}.\nExiting the script after {P10Y_REFETCH_TIMEOUT_SECONDS} seconds. Potential debugging: Verify if on P10Y UI repo list, try re-fetching them manually, verify Integration to Github")
                sys.exit(1)

        repo_ids = list(repo_id_map.values())

        # Emit workspace config JSON if requested (schema matches init_db.py --workspace-config)
        if args.output_workspace_config:
            emit_workspace_config(
                repo_id_map=repo_id_map,
                github_org=github_org or "",
                prefix=args.prefix,
                workspace_pool=workspace_pool,
                output_path=args.output_workspace_config,
                ordered_repos=own_repo_list,
            )

        # Step 4: Start metrics calculation only for repos that are not already Live.
        # The --repos path skips metrics: those repos already have history in Compass.
        if not args.skip_metrics and own_repo_list is None:
            current_statuses = await get_repository_statuses(p10y_client, p10y_org_id, repo_ids)
            metrics_repo_ids = repository_ids_requiring_metrics(repo_ids, current_statuses)

            if metrics_repo_ids:
                await start_metrics_calculation(p10y_client, p10y_org_id, metrics_repo_ids)
            else:
                print("\n✅ All repositories are already Live in P10Y; skipping metrics trigger")
            
            # Step 5: Poll for status
            if metrics_repo_ids:
                final_statuses = await poll_repository_status(
                    p10y_client,
                    p10y_org_id,
                    repo_ids,
                    args.poll_timeout,
                    args.poll_interval
                )
            else:
                final_statuses = current_statuses
        else:
            final_statuses = {}
        
        # Step 6: Add workspaces to the active database
        # --repos path: workspace-config JSON is written above; database seeding is done
        # separately by init_db.py --workspace-config. add_workspaces_to_firestore
        # extracts workspace IDs from {prefix}{num} names and cannot handle arbitrary names.
        if not args.skip_firestore and own_repo_list is None:
            await add_workspaces_to_firestore(
                repo_id_map,
                github_org,
                args.prefix,
                args.start,
                workspace_pool,
                firestore_project_id=gcp_cli if firestore_target_from_cli else None,
                firestore_database_id=fsdb_cli if firestore_target_from_cli else None,
            )
        else:
            print("\n⏭️  Skipping Firestore workspace creation")
        
        # Print final summary
        print("\n" + "=" * 80)
        print("📊 Final Status Summary")
        print("=" * 80)
        for repo_id, status_info in final_statuses.items():
            repo_name = status_info.get("repo_name", f"ID:{repo_id}")
            status = status_info.get("status", "unknown")
            internal_status = status_info.get("internal_status", "unknown")
            # P10Y's list endpoint often returns status=None while internal_status=1,
            # which still means ready (see _repo_is_ready).
            emoji = "🟢" if _repo_is_ready(status_info) else "🟡" if status == "Pending" else "🔴"
            print(f"{emoji} {repo_name}: {status} (internal: {internal_status})")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ Script failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await github_client.close()
        await p10y_client.close()
    
    print("\n✅ Script completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
