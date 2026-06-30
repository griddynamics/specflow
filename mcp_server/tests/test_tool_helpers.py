"""
Tests for tool_helpers: resolve_spec_context, guard_if_running, check_status_safe,
and resolve_workspace_count.

Global _project_root state in services.session is reset around each test via the
reset_project_root fixture.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.session as session_mod
from schemas.gain_json import SpecFlow_JSON_FILENAME
from services.tool_helpers import (
    check_status_safe,
    guard_if_running,
    is_generation_in_progress,
    resolve_spec_context,
    resolve_workspace_count,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_project_root():
    """Isolate _project_root global between tests."""
    saved = session_mod._project_root
    session_mod._project_root = None
    yield
    session_mod._project_root = saved


@pytest.fixture
def mock_ctx():
    """A no-op MCP context — apply_project_root_from_context is patched out."""
    return MagicMock()


# ---------------------------------------------------------------------------
# resolve_spec_context
# ---------------------------------------------------------------------------


class TestResolveSpecContext:
    @pytest.mark.asyncio
    async def test_spec_exists_sets_project_root(self, tmp_path, mock_ctx):
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            sd, pr, src, error = await resolve_spec_context(str(spec_dir), "src", mock_ctx)

        assert error is None
        assert sd == spec_dir
        assert pr == tmp_path
        assert session_mod._project_root == tmp_path

    @pytest.mark.asyncio
    async def test_spec_exists_src_dir_defaults_to_project_root_src(self, tmp_path, mock_ctx):
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            _, pr, src, error = await resolve_spec_context(str(spec_dir), "src", mock_ctx)

        assert error is None
        assert src == tmp_path / "src"

    @pytest.mark.asyncio
    async def test_spec_exists_relative_src_dir_resolved_against_project_root(self, tmp_path, mock_ctx):
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            _, pr, src, error = await resolve_spec_context(str(spec_dir), "my_src", mock_ctx)

        assert error is None
        assert src == tmp_path / "my_src"

    @pytest.mark.asyncio
    async def test_spec_exists_absolute_src_dir_used_as_is(self, tmp_path, mock_ctx):
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()
        abs_src = tmp_path / "elsewhere" / "src"

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            _, _, src, error = await resolve_spec_context(str(spec_dir), str(abs_src), mock_ctx)

        assert error is None
        assert src == abs_src

    @pytest.mark.asyncio
    async def test_spec_missing_returns_error_json(self, tmp_path, mock_ctx):
        missing_spec = tmp_path / "specs"  # not created

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            _, _, _, error = await resolve_spec_context(str(missing_spec), "src", mock_ctx)

        assert error is not None
        parsed = json.loads(error)
        assert "not found" in parsed["error"].lower() or "not found" in parsed.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_spec_missing_does_not_set_project_root(self, tmp_path, mock_ctx):
        """SRP fix: _project_root must not be polluted when spec directory does not exist."""
        missing_spec = tmp_path / "specs"

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            await resolve_spec_context(str(missing_spec), "src", mock_ctx)

        assert session_mod._project_root is None

    @pytest.mark.asyncio
    async def test_spec_missing_with_relative_src_dir_no_exception(self, tmp_path, mock_ctx):
        """Even with a relative src_dir and no _project_root, missing spec must return error JSON."""
        missing_spec = tmp_path / "specs"

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            _, _, _, error = await resolve_spec_context(str(missing_spec), "src", mock_ctx)

        assert error is not None  # must not raise ValueError

    @pytest.mark.asyncio
    async def test_spec_missing_preserves_existing_project_root(self, tmp_path, mock_ctx):
        """A prior valid project root must survive a subsequent failed spec lookup."""
        prior_root = tmp_path / "prior"
        prior_root.mkdir()
        session_mod._project_root = prior_root

        missing_spec = tmp_path / "other" / "specs"

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            await resolve_spec_context(str(missing_spec), "src", mock_ctx)

        assert session_mod._project_root == prior_root

    @pytest.mark.asyncio
    async def test_spec_exists_creates_gain_json(self, tmp_path, mock_ctx):
        """gain.json is created at project_root on first successful call."""
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            await resolve_spec_context(str(spec_dir), "src", mock_ctx)

        gain_json = tmp_path / SpecFlow_JSON_FILENAME
        assert gain_json.exists()
        data = json.loads(gain_json.read_text())
        assert "description" in data
        assert "codingAgents" in data
        assert "versions" in data
        assert "specflow" in data["versions"]
        assert "rosetta" not in data["versions"]

    @pytest.mark.asyncio
    async def test_spec_exists_merges_gain_json_preserves_non_schema(self, tmp_path, mock_ctx):
        """Pre-existing keys stay; our schema is applied (and overwrites if present)."""
        spec_dir = tmp_path / "specs"
        spec_dir.mkdir()
        existing = {
            "custom": "value",
            "description": "old",
            "servicesDescription": {
                "extra": "kept",
                "specflow": "will be replaced",
            },
            "versions": {
                "specflow": "0.0.0",
                "rosetta": "old",
                "unknownTool": 1,
            },
        }
        (tmp_path / SpecFlow_JSON_FILENAME).write_text(json.dumps(existing))

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            await resolve_spec_context(str(spec_dir), "src", mock_ctx)

        data = json.loads((tmp_path / SpecFlow_JSON_FILENAME).read_text())
        assert data["custom"] == "value"
        assert "description" in data and data["description"] != "old"
        assert data["servicesDescription"]["extra"] == "kept"
        assert data["servicesDescription"]["specflow"] != "will be replaced"
        assert data["versions"]["unknownTool"] == 1
        assert "codingAgents" in data
        assert "specflow" in data["versions"] and "rosetta" in data["versions"]

    @pytest.mark.asyncio
    async def test_spec_missing_does_not_create_gain_json(self, tmp_path, mock_ctx):
        """gain.json must not be created when the spec directory is missing."""
        missing_spec = tmp_path / "specs"

        with patch("services.tool_helpers.apply_project_root_from_context", new_callable=AsyncMock):
            await resolve_spec_context(str(missing_spec), "src", mock_ctx)

        assert not (tmp_path / SpecFlow_JSON_FILENAME).exists()


# ---------------------------------------------------------------------------
# guard_if_running
# ---------------------------------------------------------------------------


class TestGuardIfRunning:
    @pytest.mark.asyncio
    async def test_running_status_returns_error_json(self):
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "running"}
            result = await guard_if_running("est-1")

        assert result is not None
        parsed = json.loads(result)
        assert parsed["status"] == "running"
        assert "est-1" in parsed.get("generation_id", "")

    @pytest.mark.asyncio
    async def test_initializing_status_returns_error_json(self):
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "initializing"}
            result = await guard_if_running("est-2")

        assert result is not None
        parsed = json.loads(result)
        assert parsed["status"] == "initializing"

    @pytest.mark.asyncio
    async def test_completed_status_returns_none(self):
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "completed"}
            result = await guard_if_running("est-3")

        assert result is None

    @pytest.mark.asyncio
    async def test_failed_status_returns_none(self):
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "failed"}
            result = await guard_if_running("est-4")

        assert result is None

    @pytest.mark.asyncio
    async def test_backend_unavailable_returns_none(self):
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = None
            result = await guard_if_running("est-5")

        assert result is None

    @pytest.mark.asyncio
    async def test_custom_statuses_respected(self):
        """guard_if_running should only block on explicitly passed statuses."""
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "analysis"}
            result = await guard_if_running("est-6", statuses=("analysis",))

        assert result is not None
        parsed = json.loads(result)
        assert parsed["status"] == "analysis"

    @pytest.mark.asyncio
    async def test_uppercase_status_from_backend_handled(self):
        """Status comparison is lowercased — guard must still fire on 'RUNNING'."""
        with patch("services.tool_helpers.check_status_safe", new_callable=AsyncMock) as mock_cs:
            mock_cs.return_value = {"status": "RUNNING"}
            result = await guard_if_running("est-7")

        assert result is not None


# ---------------------------------------------------------------------------
# is_generation_in_progress — shared detection used by guard_if_running AND
# the run_generation / retry contract guards in server.py (PR255 Bug 5).
# ---------------------------------------------------------------------------


class TestIsGenerationInProgress:
    def test_running_blocks(self):
        assert is_generation_in_progress({"status": "running"}) is True

    def test_initializing_blocks(self):
        # The PR255 bug: INITIALIZING must count as in-progress so a second
        # run_generation cannot slip in between session creation and allocation.
        assert is_generation_in_progress({"status": "initializing"}) is True

    def test_uppercase_blocks(self):
        # Detection is case-insensitive; a backend that returns 'RUNNING' still blocks.
        assert is_generation_in_progress({"status": "RUNNING"}) is True

    def test_pending_passes(self):
        assert is_generation_in_progress({"status": "pending"}) is False

    def test_completed_passes(self):
        assert is_generation_in_progress({"status": "completed"}) is False

    def test_failed_passes(self):
        assert is_generation_in_progress({"status": "failed"}) is False

    def test_none_status_data_passes(self):
        assert is_generation_in_progress(None) is False

    def test_empty_status_data_passes(self):
        assert is_generation_in_progress({}) is False

    def test_custom_statuses_respected(self):
        assert is_generation_in_progress({"status": "analysis"}, statuses=("analysis",)) is True
        assert is_generation_in_progress({"status": "running"}, statuses=("analysis",)) is False


# ---------------------------------------------------------------------------
# check_status_safe
# ---------------------------------------------------------------------------


class TestCheckStatusSafe:
    @pytest.mark.asyncio
    async def test_success_returns_dict(self):
        with patch("services.tool_helpers.call_backend_endpoint", new_callable=AsyncMock) as mock_ep:
            mock_ep.return_value = json.dumps({"status": "running", "checkpoint": "generation_done"})
            result = await check_status_safe("est-ok")

        assert result == {"status": "running", "checkpoint": "generation_done"}

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        import httpx
        with patch("services.tool_helpers.call_backend_endpoint", new_callable=AsyncMock) as mock_ep:
            mock_ep.side_effect = httpx.HTTPError("connection refused")
            result = await check_status_safe("est-err")

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        with patch("services.tool_helpers.call_backend_endpoint", new_callable=AsyncMock) as mock_ep:
            mock_ep.return_value = "Error calling backend: 503 Service Unavailable"
            result = await check_status_safe("est-bad-json")

        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self):
        with patch("services.tool_helpers.call_backend_endpoint", new_callable=AsyncMock) as mock_ep:
            mock_ep.side_effect = RuntimeError("unexpected")
            result = await check_status_safe("est-exc")

        assert result is None


# ---------------------------------------------------------------------------
# resolve_workspace_count
# ---------------------------------------------------------------------------


class TestResolveWorkspaceCount:
    def test_returns_env_default(self, monkeypatch):
        import services.tool_helpers as th
        monkeypatch.setattr(th, "_DEFAULT_WORKSPACE_COUNT", 3)
        assert resolve_workspace_count() == 3

    def test_unset_env_returns_none(self, monkeypatch):
        import services.tool_helpers as th
        monkeypatch.setattr(th, "_DEFAULT_WORKSPACE_COUNT", None)
        assert resolve_workspace_count() is None
