"""
Tests for backend/scripts/create_generation_session_repos.py — Phase 6.

Coverage:
  (a) emit_workspace_config writes JSON whose entries each construct a WorkspacePoolEntry
      without error, proving schema parity with init_db.py --workspace-config.
  (b) get_repository_ids calls list_repositories WITHOUT project_ids (no filter).
  (c) No code path calls add_repositories_to_project — the function is absent from the
      module and the compiled AST contains no reference to it.
  (d) The Makefile create-repos target references scripts/create_generation_session_repos.py
      (no ../) and that file exists relative to the backend/ cwd; the old
      create_estimation_repos.py is no longer referenced by that target.
"""

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import scripts.create_generation_session_repos as cgsr
from app.services.git_provider import GitProvider
from app.services.p10y.p10y_api_client import P10YInternalAPIClient
from app.services.workspace_pool_seeding import WorkspacePoolEntry

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).parent.parent.parent  # backend/
_SCRIPT_FILE = _BACKEND_DIR / "scripts" / "create_generation_session_repos.py"
_MAKEFILE = _BACKEND_DIR.parent / "Makefile"


def _make_p10y_client() -> P10YInternalAPIClient:
    """A real client so list_repositories_paginated runs its actual pagination loop
    against a mocked list_repositories, instead of being auto-mocked away."""
    return P10YInternalAPIClient(base_url="https://p10y.test")


# ===========================================================================
# (a) emit_workspace_config — schema parity with WorkspacePoolEntry
# ===========================================================================

class TestEmitWorkspaceConfig:
    """emit_workspace_config emits JSON that round-trips through WorkspacePoolEntry."""

    def _make_repo_id_map(self, prefix: str = "specflow-workspace", start: int = 1, count: int = 3) -> dict:
        return {f"{prefix}{i}": 10000 + i for i in range(start, start + count)}

    def test_roundtrip_through_workspace_config(self, tmp_path):
        """Each emitted entry must construct WorkspacePoolEntry without error."""
        repo_id_map = self._make_repo_id_map(start=1, count=3)
        output = str(tmp_path / "workspaces.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="test-org",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
        )

        with open(output, encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, list), "Output must be a JSON array"
        assert len(data) == 3, "Should emit one entry per repo"
        for entry in data:
            # This will raise if any field is missing or p10y_repository_id is not an int
            wc = WorkspacePoolEntry(**entry)
            assert isinstance(wc.p10y_repository_id, int)
            assert not isinstance(wc.p10y_repository_id, bool)

    def test_all_four_required_fields_present(self, tmp_path):
        """All four required fields must appear in every entry."""
        repo_id_map = {"specflow-workspace1": 99999}
        output = str(tmp_path / "out.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="my-org",
            prefix="specflow-workspace",
            workspace_pool="testpool",
            output_path=output,
        )

        data = json.load(open(output, encoding="utf-8"))
        assert len(data) == 1
        entry = data[0]
        assert set(entry.keys()) >= {"workspace_id", "repo_url", "p10y_repository_id", "workspace_pool"}

    def test_workspace_id_derivation(self, tmp_path):
        """workspace_id must follow ws-{set:02d}-{index} convention."""
        # repos 1,2,3 → set 1, indices 1,2,3
        repo_id_map = {
            "specflow-workspace1": 1001,
            "specflow-workspace2": 1002,
            "specflow-workspace3": 1003,
        }
        output = str(tmp_path / "out.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="org",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
        )

        data = sorted(json.load(open(output, encoding="utf-8")), key=lambda e: e["workspace_id"])
        assert data[0]["workspace_id"] == "ws-01-1"
        assert data[1]["workspace_id"] == "ws-01-2"
        assert data[2]["workspace_id"] == "ws-01-3"

    def test_repo_url_uses_github_org(self, tmp_path):
        """repo_url must be https://github.com/{org}/{repo_name}."""
        repo_id_map = {"specflow-workspace4": 77777}
        output = str(tmp_path / "out.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="my-org",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
        )

        data = json.load(open(output, encoding="utf-8"))
        assert data[0]["repo_url"] == "https://github.com/my-org/specflow-workspace4"

    def test_p10y_repository_id_is_int_not_bool(self, tmp_path):
        """p10y_repository_id must be a plain int (WorkspacePoolEntry rejects bool)."""
        repo_id_map = {"specflow-workspace1": 12345}
        output = str(tmp_path / "out.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="org",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
        )

        data = json.load(open(output, encoding="utf-8"))
        pid = data[0]["p10y_repository_id"]
        assert isinstance(pid, int)
        assert not isinstance(pid, bool)

    def test_workspace_pool_propagated(self, tmp_path):
        """workspace_pool in emitted JSON must equal the argument passed."""
        repo_id_map = {"specflow-workspace1": 1}
        output = str(tmp_path / "out.json")

        cgsr.emit_workspace_config(
            repo_id_map=repo_id_map,
            owner="org",
            prefix="specflow-workspace",
            workspace_pool="custom-pool",
            output_path=output,
        )

        data = json.load(open(output, encoding="utf-8"))
        assert data[0]["workspace_pool"] == "custom-pool"

    def test_output_file_created_in_subdir(self, tmp_path):
        """emit_workspace_config must create parent dirs if they do not exist."""
        output = str(tmp_path / "subdir" / "nested" / "workspaces.json")

        cgsr.emit_workspace_config(
            repo_id_map={"specflow-workspace1": 1},
            owner="org",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
        )

        assert Path(output).exists()


# ===========================================================================
# (b) get_repository_ids calls list_repositories WITHOUT project_ids
# ===========================================================================

class TestGetRepositoryIdsNoProjectFilter:
    """list_repositories must NOT receive project_ids in the read path."""

    @pytest.mark.asyncio
    async def test_list_repositories_called_without_project_ids(self):
        """get_repository_ids must not pass project_ids to list_repositories."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={"data": []})

        await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1"],
            search="specflow-workspace",
        )

        mock_client.list_repositories.assert_called_once()
        call_kwargs = mock_client.list_repositories.call_args.kwargs
        assert "project_ids" not in call_kwargs, (
            "get_repository_ids must not pass project_ids to list_repositories"
        )

    @pytest.mark.asyncio
    async def test_list_repositories_forwards_qualified_search_verbatim(self):
        """get_repository_ids forwards the caller-supplied search string as-is (it must not
        re-qualify it — that double-qualifies when a caller passes an already-qualified
        value, e.g. main()'s post-refetch retry loop)."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={"data": []})

        qualified_search = cgsr._p10y_repository_search("specflow-workspace", "myorg")

        await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1"],
            search=qualified_search,
            github_org="myorg",
        )

        call_kwargs = mock_client.list_repositories.call_args.kwargs
        assert call_kwargs["search"] == "myorg/specflow-workspace"

    @pytest.mark.asyncio
    async def test_repeated_calls_do_not_double_qualify_search(self):
        """Calling get_repository_ids twice with the same pre-qualified search (as main()'s
        initial lookup and its post-refetch retry loop both do) must send the identical
        search string both times, not a progressively re-qualified one."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={"data": []})

        qualified_search = cgsr._p10y_repository_search("specflow-workspace", "myorg")

        await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1"],
            search=qualified_search,
            github_org="myorg",
        )
        await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1"],
            search=qualified_search,
            github_org="myorg",
        )

        searches = [c.kwargs["search"] for c in mock_client.list_repositories.call_args_list]
        assert searches == ["myorg/specflow-workspace", "myorg/specflow-workspace"]

    @pytest.mark.asyncio
    async def test_list_repositories_returns_repo_mapping(self):
        """get_repository_ids returns a mapping of repo_name -> id."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={
            "data": [
                {"repository_name": "specflow-workspace1", "id": 55555},
                {"repository_name": "specflow-workspace2", "id": 55556},
            ]
        })

        result = await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1", "specflow-workspace2"],
            search="specflow-workspace",
        )

        assert result == {"specflow-workspace1": 55555, "specflow-workspace2": 55556}

    @pytest.mark.asyncio
    async def test_list_repositories_paginates_until_target_repo_found(self):
        """Repository lookup must continue past the first P10Y page."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(side_effect=[
            {
                "data": [
                    {
                        "repository_name": "specflow-workspace1",
                        "git_url": "https://github.com/myorg/specflow-workspace1",
                        "id": 55555,
                    }
                ],
                "totalPages": 2,
            },
            {
                "data": [
                    {
                        "repository_name": "specflow-workspace1001",
                        "git_url": "https://github.com/myorg/specflow-workspace1001",
                        "id": 56555,
                    }
                ],
                "totalPages": 2,
            },
        ])

        result = await cgsr.get_repository_ids(
            p10y_client=mock_client,
            org_id=42,
            repo_names=["specflow-workspace1001"],
            search="myorg/specflow-workspace",
            github_org="myorg",
        )

        assert result == {"specflow-workspace1001": 56555}
        pages = [call.kwargs["page"] for call in mock_client.list_repositories.await_args_list]
        assert pages == [1, 2]


# ===========================================================================
# (b2) trigger_repository_refetch — scope the re-fetch to the owning connection
# ===========================================================================

class TestTriggerRepositoryRefetch:
    """A no-arg /repository/sync 400s, so the re-fetch must target the connection that
    owns the workspace repos (resolved from already-ingested repos, else active GitHub
    connections)."""

    @pytest.mark.asyncio
    async def test_uses_connection_id_of_matching_repo(self):
        """The connection id is taken from an already-ingested repo matched by git_url."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={
            "data": [
                {
                    "repository_name": "specflow-workspace1",
                    "git_url": "https://github.com/myorg/specflow-workspace1",
                    "_embedded": {"connection": {"id_connection": 555}},
                },
            ]
        })
        mock_client.list_connections = AsyncMock()
        mock_client.sync_repositories = AsyncMock()

        await cgsr.trigger_repository_refetch(mock_client, org_id=42, github_org="myorg")

        mock_client.sync_repositories.assert_awaited_once_with(42, connection_id=555)
        mock_client.list_connections.assert_not_called()

    @pytest.mark.asyncio
    async def test_matches_any_repo_under_same_org_not_just_the_new_ones(self):
        """The brand-new repos being provisioned are never yet visible in P10Y — that's
        why this is called — so the connection must be discoverable from ANY already
        ingested repo under the same GitHub org/account, not just an exact match to the
        new repo names, or every real-world call would fall through to broadcasting the
        re-fetch across every active GitHub connection."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={
            "data": [
                {
                    "repository_name": "some-unrelated-older-repo",
                    "git_url": "https://github.com/myorg/some-unrelated-older-repo",
                    "_embedded": {"connection": {"id_connection": 555}},
                },
            ]
        })
        mock_client.list_connections = AsyncMock()
        mock_client.sync_repositories = AsyncMock()

        await cgsr.trigger_repository_refetch(mock_client, org_id=42, github_org="myorg")

        mock_client.sync_repositories.assert_awaited_once_with(42, connection_id=555)
        mock_client.list_connections.assert_not_called()
        assert mock_client.list_repositories.call_args.kwargs["search"] == "myorg"

    @pytest.mark.asyncio
    async def test_falls_back_to_active_github_connections(self):
        """When no repo matches, sync every active GitHub connection."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={"data": []})
        mock_client.list_connections = AsyncMock(return_value={
            "data": [
                {"connection_id": 777, "connection_type": "github", "connection_status": "active"},
                {"connection_id": 888, "connection_type": "gitlab", "connection_status": "active"},
                {"connection_id": 999, "connection_type": "github", "connection_status": "inactive"},
            ]
        })
        mock_client.sync_repositories = AsyncMock()

        await cgsr.trigger_repository_refetch(mock_client, org_id=42, github_org="myorg")

        mock_client.sync_repositories.assert_awaited_once_with(42, connection_id=777)

    @pytest.mark.asyncio
    async def test_no_op_when_no_active_github_connections(self):
        """When list_connections returns no active GitHub connections, skip sync and return."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={"data": []})
        mock_client.list_connections = AsyncMock(return_value={
            "data": [
                {"connection_id": 111, "connection_type": "gitlab", "connection_status": "active"},
                {"connection_id": 222, "connection_type": "github", "connection_status": "inactive"},
            ]
        })
        mock_client.sync_repositories = AsyncMock()

        await cgsr.trigger_repository_refetch(mock_client, org_id=42, github_org="myorg")

        mock_client.sync_repositories.assert_not_called()


# ===========================================================================
# (c) P10Y status gate — only non-Live repos trigger metrics
# ===========================================================================

class TestP10YMetricsStatusGate:
    """P10Y metrics should not be re-triggered for repositories already Live."""

    @pytest.mark.asyncio
    async def test_get_repository_statuses_accepts_id_field(self):
        """Status lookup accepts the list_repositories id field used by ID lookup."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={
            "data": [
                {
                    "id": 101,
                    "repository_name": "specflow-workspace1",
                    "status": "Live",
                    "internal_status": "ready",
                },
                {
                    "id": 102,
                    "repository_name": "specflow-workspace2",
                    "status": "Pending",
                    "internal_status": "queued",
                },
            ]
        })

        result = await cgsr.get_repository_statuses(mock_client, 42, [101, 102])

        assert result[101]["status"] == "Live"
        assert result[102]["status"] == "Pending"

    @pytest.mark.asyncio
    async def test_get_repository_statuses_accepts_id_repository_field(self):
        """Status lookup also accepts id_repository from P10Y status responses."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(return_value={
            "data": [
                {
                    "id_repository": 201,
                    "repository_name": "specflow-workspace1",
                    "status": "Live",
                    "internal_status": "ready",
                },
            ]
        })

        result = await cgsr.get_repository_statuses(mock_client, 42, [201])

        assert result[201]["repo_name"] == "specflow-workspace1"
        assert result[201]["status"] == "Live"

    @pytest.mark.asyncio
    async def test_get_repository_statuses_paginates(self):
        """Status lookup must not miss target repo IDs beyond the first P10Y page."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories = AsyncMock(side_effect=[
            {"data": [{"id": 101, "repository_name": "specflow-workspace1"}], "totalPages": 2},
            {
                "data": [
                    {
                        "id": 102,
                        "repository_name": "specflow-workspace2",
                        "status": "Live",
                    }
                ],
                "totalPages": 2,
            },
        ])

        result = await cgsr.get_repository_statuses(
            mock_client,
            42,
            [102],
            search="myorg/specflow-workspace",
        )

        assert result[102]["repo_name"] == "specflow-workspace2"
        assert result[102]["status"] == "Live"
        pages = [call.kwargs["page"] for call in mock_client.list_repositories.await_args_list]
        assert pages == [1, 2]

    def test_repository_ids_requiring_metrics_skips_live_repos(self):
        """Only repositories not already Live should be passed to enable_metrics."""
        repo_statuses = {
            1: {"status": "Live"},
            2: {"status": "Pending"},
            3: {"status": "Failed"},
        }

        result = cgsr.repository_ids_requiring_metrics([1, 2, 3, 4], repo_statuses)

        assert result == [2, 3, 4]


# ===========================================================================
# (c2) poll_repository_status — a pagination-cap RuntimeError must fail fast,
# not be treated as a transient polling error and retried until timeout.
# ===========================================================================

class TestPollRepositoryStatusFailsFastOnRuntimeError:
    @pytest.mark.asyncio
    async def test_runtime_error_propagates_instead_of_being_retried(self):
        """A RuntimeError from list_repositories_paginated (e.g. pagination cap exceeded)
        must propagate immediately, not be swallowed by the generic transient-error handler."""
        mock_client = _make_p10y_client()
        mock_client.list_repositories_paginated = AsyncMock(
            side_effect=RuntimeError("P10Y repository listing exceeded 1000 pages")
        )

        with pytest.raises(RuntimeError, match="exceeded 1000 pages"):
            await cgsr.poll_repository_status(
                mock_client,
                org_id=42,
                repo_ids=[101],
                timeout_minutes=5,
                poll_interval=0,
            )

        mock_client.list_repositories_paginated.assert_awaited_once()


# ===========================================================================
# (d) add_repositories_to_project is not callable from the module
# ===========================================================================

class TestNoAddRepositoriesToProject:
    """The dead add_repositories_to_project function must have been removed."""

    def test_function_not_in_module(self):
        """The module must not expose add_repositories_to_project."""
        assert not hasattr(cgsr, "add_repositories_to_project"), (
            "add_repositories_to_project was not removed from the module"
        )

    def test_function_not_in_ast(self):
        """The source AST must contain no FunctionDef named add_repositories_to_project."""
        source = _SCRIPT_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "add_repositories_to_project" not in func_names, (
            "add_repositories_to_project still exists as a function definition in the source"
        )

    def test_no_call_to_add_repositories_to_project_in_source(self):
        """No call to add_repositories_to_project must appear in the source."""
        source = _SCRIPT_FILE.read_text(encoding="utf-8")
        assert "add_repositories_to_project" not in source, (
            "Source still contains a reference to add_repositories_to_project"
        )


# ===========================================================================
# (e) Makefile target references correct script path; old name is gone
# ===========================================================================

class TestMakefileCreateReposTarget:
    """create-repos target must reference the new script, not the old one."""

    def _get_create_repos_block(self) -> str:
        """Extract the lines belonging to the create-repos target."""
        makefile_text = _MAKEFILE.read_text(encoding="utf-8")
        lines = makefile_text.splitlines()
        in_target = False
        block: list = []
        for line in lines:
            if re.match(r"^create-repos\s*:", line):
                in_target = True
            if in_target:
                block.append(line)
                # A new non-indented, non-empty, non-comment target line ends the block
                if block and line and not line.startswith(("\t", " ", "#", "create-repos")) and len(block) > 1:
                    break
        return "\n".join(block)

    def test_new_script_referenced(self):
        """Makefile create-repos must reference scripts/create_generation_session_repos.py."""
        block = self._get_create_repos_block()
        assert "scripts/create_generation_session_repos.py" in block, (
            "create-repos target does not reference scripts/create_generation_session_repos.py"
        )

    def test_old_script_not_referenced(self):
        """Makefile create-repos must NOT reference create_estimation_repos.py."""
        block = self._get_create_repos_block()
        assert "create_estimation_repos.py" not in block, (
            "create-repos target still references old create_estimation_repos.py"
        )

    def test_no_dotdot_path(self):
        """The script path must not start with ../ (cwd is backend/ after cd backend)."""
        block = self._get_create_repos_block()
        assert "../scripts/" not in block, (
            "create-repos target uses ../scripts/ but cwd is already backend/ — use scripts/ instead"
        )

    def test_script_file_exists_on_disk(self):
        """backend/scripts/create_generation_session_repos.py must exist on disk."""
        assert _SCRIPT_FILE.exists(), (
            f"Script not found at expected path: {_SCRIPT_FILE}"
        )


# ===========================================================================
# (f) _repo_url — provider-generic URL construction
# ===========================================================================

class TestRepoUrl:
    def test_github(self):
        assert cgsr._repo_url(GitProvider.GITHUB, "my-org", "repo1") == "https://github.com/my-org/repo1"

    def test_bitbucket(self):
        assert cgsr._repo_url(GitProvider.BITBUCKET_CLOUD, "my-ws", "repo1") == "https://bitbucket.org/my-ws/repo1"


# ===========================================================================
# (g) emit_workspace_config — BitBucket owner/provider + null p10y id
# ===========================================================================

class TestEmitWorkspaceConfigBitbucket:
    def test_bitbucket_repo_url_and_null_p10y_id(self, tmp_path):
        output = str(tmp_path / "workspaces.json")

        cgsr.emit_workspace_config(
            repo_id_map={"specflow-workspace1": None},
            owner="my-ws",
            prefix="specflow-workspace",
            workspace_pool="default",
            output_path=output,
            provider=GitProvider.BITBUCKET_CLOUD,
        )

        data = json.load(open(output, encoding="utf-8"))
        assert data[0]["repo_url"] == "https://bitbucket.org/my-ws/specflow-workspace1"
        assert data[0]["p10y_repository_id"] is None


# ===========================================================================
# (h) BitbucketCloudAPIClient — create/exists against mocked httpx
# ===========================================================================

class TestBitbucketCloudAPIClient:
    @pytest.mark.asyncio
    async def test_repository_exists_true(self):
        client = cgsr.BitbucketCloudAPIClient("tok", "my-ws")
        with patch.object(client.client, "get", new=AsyncMock(return_value=MagicMock(status_code=200))):
            assert await client.repository_exists("repo1") is True
        await client.close()

    @pytest.mark.asyncio
    async def test_repository_exists_false(self):
        client = cgsr.BitbucketCloudAPIClient("tok", "my-ws")
        with patch.object(client.client, "get", new=AsyncMock(return_value=MagicMock(status_code=404))):
            assert await client.repository_exists("repo1") is False
        await client.close()

    @pytest.mark.asyncio
    async def test_create_repository_success(self):
        client = cgsr.BitbucketCloudAPIClient("tok", "my-ws")
        response = MagicMock(status_code=201)
        response.raise_for_status = MagicMock()
        response.json.return_value = {"name": "repo1"}
        with patch.object(client.client, "post", new=AsyncMock(return_value=response)):
            result = await client.create_repository("repo1")
        assert result == {"name": "repo1"}
        await client.close()

    @pytest.mark.asyncio
    async def test_create_repository_already_exists_is_idempotent(self):
        client = cgsr.BitbucketCloudAPIClient("tok", "my-ws")
        create_response = MagicMock(status_code=400, text="Repository already exists")
        get_response = MagicMock(status_code=200)
        get_response.raise_for_status = MagicMock()
        get_response.json.return_value = {"name": "repo1", "already_existed": True}
        with (
            patch.object(client.client, "post", new=AsyncMock(return_value=create_response)),
            patch.object(client.client, "get", new=AsyncMock(return_value=get_response)),
        ):
            result = await client.create_repository("repo1")
        assert result["already_existed"] is True
        await client.close()

    @pytest.mark.asyncio
    async def test_create_repository_other_error_raises(self):
        client = cgsr.BitbucketCloudAPIClient("tok", "my-ws")
        response = MagicMock(status_code=403, text="Forbidden")
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=response)
        )
        with patch.object(client.client, "post", new=AsyncMock(return_value=response)):
            with pytest.raises(httpx.HTTPStatusError):
                await client.create_repository("repo1")
        await client.close()
