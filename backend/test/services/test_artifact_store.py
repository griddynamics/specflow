"""
Tests for ArtifactStore — new namespaced archive methods.

Covers:
  archive_workspace  — copies workspace, excludes build artifacts
  archive_analysis   — copies spec analysis outputs
  archive_report     — copies combined report (file or directory)
  build_tarball      — packages the full artifact tree as tar.gz
"""
import tarfile
import pytest
from unittest.mock import AsyncMock

from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR, REPORT_SUBDIR
from app.services.artifact_store import ArtifactStore
from app.prompts.agents_claude_code import PARTIAL_OUTPUT_WARNING_FILE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_artifacts(tmp_path, monkeypatch):
    """Redirect ARTIFACTS_BASE to a temp directory for isolation."""
    import app.services.artifact_store as store_module
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    monkeypatch.setattr(store_module, "ARTIFACTS_BASE", artifacts_dir)
    return artifacts_dir


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_generation_session = AsyncMock(return_value={
        "outputs_archived": True,
        "artifact_path": None,  # overridden per test
    })
    db.update_generation_session = AsyncMock()
    return db


@pytest.fixture
def store(mock_db):
    return ArtifactStore(mock_db)


@pytest.fixture
def workspace_dir(tmp_path):
    """A fake workspace with source code and build artifacts to exclude."""
    ws = tmp_path / "ws-01-1"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_text("print('hello')")
    (ws / "src" / "utils.py").write_text("def add(a,b): return a+b")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "react").mkdir()
    (ws / "node_modules" / "react" / "index.js").write_text("module.exports={}")
    (ws / "target").mkdir()
    (ws / "target" / "MyApp.jar").write_bytes(b"\x00" * 100)
    (ws / ".venv").mkdir()
    (ws / ".venv" / "pyvenv.cfg").write_text("[virtualenv]")
    (ws / "vendor").mkdir()
    (ws / "vendor" / "autoload.php").write_text("<?php")
    (ws / "standards").mkdir()
    (ws / "standards" / "coding.md").write_text("# Coding standards")
    (ws / ".angular").mkdir()
    (ws / ".angular" / "cache").mkdir()
    (ws / ".angular" / "cache" / "build.json").write_text("{}")
    (ws / "outputs").mkdir()
    (ws / "outputs" / "generation.md").write_text("# Generation")
    return ws


# ---------------------------------------------------------------------------
# archive_workspace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archive_workspace_copies_source_files(store, tmp_artifacts, workspace_dir):
    await store.archive_workspace("est-1", "ws-01-1", workspace_dir)
    dst = tmp_artifacts / "est-1" / "ws-01-1"
    assert (dst / "src" / "main.py").exists()
    assert (dst / "src" / "utils.py").exists()
    assert (dst / "outputs" / "generation.md").exists()


@pytest.mark.asyncio
async def test_archive_workspace_excludes_build_artifacts(store, tmp_artifacts, workspace_dir):
    await store.archive_workspace("est-1", "ws-01-1", workspace_dir)
    dst = tmp_artifacts / "est-1" / "ws-01-1"
    assert not (dst / ".git").exists()
    assert not (dst / "node_modules").exists()
    assert not (dst / "target").exists()
    assert not (dst / ".venv").exists()
    assert not (dst / "vendor").exists()
    assert not (dst / "standards").exists()
    assert not (dst / ".angular").exists()


@pytest.mark.asyncio
async def test_archive_workspace_idempotent(store, tmp_artifacts, workspace_dir):
    """Calling twice with same args should not raise and should overwrite."""
    await store.archive_workspace("est-1", "ws-01-1", workspace_dir)
    await store.archive_workspace("est-1", "ws-01-1", workspace_dir)
    dst = tmp_artifacts / "est-1" / "ws-01-1"
    assert (dst / "src" / "main.py").exists()


@pytest.mark.asyncio
async def test_archive_workspace_raises_if_source_missing(store, tmp_artifacts):
    with pytest.raises(FileNotFoundError, match="workspace path does not exist"):
        await store.archive_workspace("est-1", "ws-01-1", "/nonexistent/path")


@pytest.mark.asyncio
async def test_archive_workspace_namespaced_per_workspace(store, tmp_artifacts, workspace_dir):
    """Two workspaces land in separate subdirectories."""
    ws2 = workspace_dir.parent / "ws-01-2"
    ws2.mkdir()
    (ws2 / "app.go").write_text("package main")

    await store.archive_workspace("est-1", "ws-01-1", workspace_dir)
    await store.archive_workspace("est-1", "ws-01-2", ws2)

    assert (tmp_artifacts / "est-1" / "ws-01-1" / "src" / "main.py").exists()
    assert (tmp_artifacts / "est-1" / "ws-01-2" / "app.go").exists()


# ---------------------------------------------------------------------------
# archive_analysis
# ---------------------------------------------------------------------------

@pytest.fixture
def analysis_dir(tmp_path):
    d = tmp_path / "outputs"
    d.mkdir()
    (d / "specification_completeness.md").write_text("# Completeness")
    (d / "specification_index.md").write_text("# Index")
    return d


@pytest.mark.asyncio
async def test_archive_analysis_creates_analysis_subdir(store, tmp_artifacts, analysis_dir):
    await store.archive_analysis("est-1", analysis_dir)
    dst = tmp_artifacts / "est-1" / ANALYSIS_SUBDIR
    assert (dst / "specification_completeness.md").exists()
    assert (dst / "specification_index.md").exists()


@pytest.mark.asyncio
async def test_archive_analysis_idempotent(store, tmp_artifacts, analysis_dir):
    await store.archive_analysis("est-1", analysis_dir)
    await store.archive_analysis("est-1", analysis_dir)
    dst = tmp_artifacts / "est-1" / ANALYSIS_SUBDIR
    assert (dst / "specification_completeness.md").exists()


@pytest.mark.asyncio
async def test_archive_analysis_missing_source_logs_warning_no_raise(store, tmp_artifacts, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        result = await store.archive_analysis("est-1", "/nonexistent/path")
    assert "skipping" in caplog.text
    # Should return dst path without raising
    assert result == tmp_artifacts / "est-1" / ANALYSIS_SUBDIR


# ---------------------------------------------------------------------------
# archive_report
# ---------------------------------------------------------------------------

@pytest.fixture
def report_dir(tmp_path):
    d = tmp_path / "report_outputs"
    d.mkdir()
    (d / "multi-workspace-generation-report.md").write_text("# Report")
    return d


@pytest.mark.asyncio
async def test_archive_report_directory(store, tmp_artifacts, report_dir):
    await store.archive_report("est-1", report_dir)
    dst = tmp_artifacts / "est-1" / REPORT_SUBDIR
    assert (dst / "multi-workspace-generation-report.md").exists()


@pytest.mark.asyncio
async def test_archive_report_single_file(store, tmp_artifacts, tmp_path):
    report_file = tmp_path / "report.md"
    report_file.write_text("# Report")
    await store.archive_report("est-1", report_file)
    dst = tmp_artifacts / "est-1" / REPORT_SUBDIR / "report.md"
    assert dst.exists()


@pytest.mark.asyncio
async def test_archive_report_missing_source_no_raise(store, tmp_artifacts, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        result = await store.archive_report("est-1", "/nonexistent")
    assert "skipping" in caplog.text
    assert result == tmp_artifacts / "est-1" / REPORT_SUBDIR


# ---------------------------------------------------------------------------
# build_tarball_from_dir — partial downloads (analysis-only or planning-only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_tarball_from_dir_returns_none_when_no_subdirs(store, tmp_artifacts):
    """No analysis/ or planning/ dir → returns None."""
    (tmp_artifacts / "est-1").mkdir()
    result = await store.build_tarball_from_dir("est-1")
    assert result is None


@pytest.mark.asyncio
async def test_build_tarball_from_dir_returns_none_when_dirs_empty(store, tmp_artifacts):
    """analysis/ and planning/ exist but are empty → returns None."""
    est_dir = tmp_artifacts / "est-1"
    (est_dir / ANALYSIS_SUBDIR).mkdir(parents=True)
    (est_dir / PLANNING_SUBDIR).mkdir(parents=True)
    result = await store.build_tarball_from_dir("est-1")
    assert result is None


@pytest.mark.asyncio
async def test_build_tarball_from_dir_includes_analysis_files_with_prefix(store, tmp_artifacts):
    """analysis/ with files → tarball members have analysis/ prefix."""
    est_dir = tmp_artifacts / "est-1"
    analysis_dir = est_dir / ANALYSIS_SUBDIR
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "specification_index.md").write_text("# Index")
    (analysis_dir / "repo_summary.md").write_text("# Summary")

    tarball_path = await store.build_tarball_from_dir("est-1")
    assert tarball_path is not None

    with tarfile.open(tarball_path, "r:gz") as tar:
        names = tar.getnames()

    assert "analysis/specification_index.md" in names
    assert "analysis/repo_summary.md" in names
    tarball_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_build_tarball_from_dir_includes_planning_files_with_prefix(store, tmp_artifacts):
    """planning/ with files → tarball members have planning/ prefix."""
    est_dir = tmp_artifacts / "est-1"
    planning_dir = est_dir / PLANNING_SUBDIR
    planning_dir.mkdir(parents=True)
    (planning_dir / "IMPLEMENTATION_PLAN.md").write_text("# Plan")

    tarball_path = await store.build_tarball_from_dir("est-1")
    assert tarball_path is not None

    with tarfile.open(tarball_path, "r:gz") as tar:
        names = tar.getnames()

    assert "planning/IMPLEMENTATION_PLAN.md" in names
    tarball_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_build_tarball_from_dir_includes_both_subdirs(store, tmp_artifacts):
    """Both analysis/ and planning/ exist → both included in tarball."""
    est_dir = tmp_artifacts / "est-1"
    (est_dir / ANALYSIS_SUBDIR).mkdir(parents=True)
    (est_dir / ANALYSIS_SUBDIR / "specification_index.md").write_text("# Index")
    (est_dir / PLANNING_SUBDIR).mkdir(parents=True)
    (est_dir / PLANNING_SUBDIR / "IMPLEMENTATION_PLAN.md").write_text("# Plan")

    tarball_path = await store.build_tarball_from_dir("est-1")
    assert tarball_path is not None

    with tarfile.open(tarball_path, "r:gz") as tar:
        names = tar.getnames()

    assert "analysis/specification_index.md" in names
    assert "planning/IMPLEMENTATION_PLAN.md" in names
    tarball_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_build_tarball_from_dir_returns_none_when_generation_dir_missing(store, tmp_artifacts):
    """No artifact dir at all → returns None (not a FileNotFoundError)."""
    result = await store.build_tarball_from_dir("est-nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# build_tarball — contains workspace subdirectories
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_tarball_includes_workspace_subdirs(store, tmp_artifacts, mock_db, workspace_dir):
    # Set up artifact directory as if all archive methods ran
    est_dir = tmp_artifacts / "est-1"
    ws_dst = est_dir / "ws-01-1"
    ws_dst.mkdir(parents=True)
    (ws_dst / "src").mkdir()
    (ws_dst / "src" / "main.py").write_text("print('hello')")

    analysis_dst = est_dir / ANALYSIS_SUBDIR
    analysis_dst.mkdir()
    (analysis_dst / "specification_completeness.md").write_text("# Completeness")

    report_dst = est_dir / REPORT_SUBDIR
    report_dst.mkdir()
    (report_dst / "multi-workspace-generation-report.md").write_text("# Report")

    mock_db.get_generation_session.return_value = {
        "outputs_archived": True,
        "artifact_path": str(est_dir),
    }

    tarball_path = await store.build_tarball("est-1")
    assert tarball_path.exists()

    with tarfile.open(tarball_path, "r:gz") as tar:
        names = tar.getnames()

    assert any("ws-01-1/src/main.py" in n for n in names)
    assert any("analysis/specification_completeness.md" in n for n in names)
    assert any("report/multi-workspace-generation-report.md" in n for n in names)


@pytest.mark.asyncio
async def test_build_tarball_raises_if_not_archived(store, mock_db):
    mock_db.get_generation_session.return_value = {"outputs_archived": False}
    with pytest.raises(ValueError, match="not been archived yet"):
        await store.build_tarball("est-1")


@pytest.mark.asyncio
async def test_build_tarball_accepts_emergency_archived(store, tmp_artifacts, mock_db):
    """emergency_archived=True (without outputs_archived) is sufficient for build_tarball."""
    est_dir = tmp_artifacts / "est-1"
    est_dir.mkdir(parents=True)
    (est_dir / "PARTIAL_OUTPUT_WARNING.txt").write_text("partial")

    mock_db.get_generation_session.return_value = {
        "outputs_archived": False,
        "emergency_archived": True,
        "artifact_path": str(est_dir),
    }

    tarball_path = await store.build_tarball("est-1")
    assert tarball_path.exists()
    tarball_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# emergency_archive
# ---------------------------------------------------------------------------

class TestEmergencyArchive:
    """Tests for ArtifactStore.emergency_archive — idempotency, robustness, warnings."""

    @pytest.fixture
    def allocated_ws_doc(self):
        return {"status": "allocated"}

    def _make_store(self, mock_db):
        return ArtifactStore(mock_db)

    @pytest.mark.asyncio
    async def test_skips_if_outputs_archived_true(self, tmp_artifacts, mock_db):
        """If outputs_archived=True (full generation archive), skip to protect it."""
        mock_db.get_generation_session.return_value = {
            "outputs_archived": True,
            "partial_archive_warnings": ["ws-1: was empty"],
        }
        store = self._make_store(mock_db)
        result = await store.emergency_archive("est-1")
        assert result == {"workspace_warnings": ["ws-1: was empty"]}
        mock_db.update_generation_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_reruns_if_emergency_archived_true(self, tmp_path, tmp_artifacts, mock_db, allocated_ws_doc):
        """emergency_archived=True alone must NOT prevent re-archiving (re-runnable)."""
        ws = tmp_path / "ws-01-1"
        ws.mkdir()
        (ws / "app.py").write_text("# code v2")

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "emergency_archived": True,  # already snapshotted once
            "workspace_ids": ["ws-01-1"],
            "status": "failed",
            "checkpoint": "generation_done",
        }
        mock_db.get_workspace.return_value = allocated_ws_doc

        store = self._make_store(mock_db)
        result = await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        assert result["workspace_warnings"] == []
        mock_db.update_generation_session.assert_called_once()
        update = mock_db.update_generation_session.call_args[0][1]
        assert update["emergency_archived"] is True
        assert "outputs_archived" not in update

    @pytest.mark.asyncio
    async def test_archives_all_allocated_workspaces(self, tmp_path, tmp_artifacts, mock_db, allocated_ws_doc):
        """Happy path: all workspaces ALLOCATED with content → all archived, warning file written."""
        ws1 = tmp_path / "ws-01-1"
        ws2 = tmp_path / "ws-01-2"
        for ws in (ws1, ws2):
            ws.mkdir()
            (ws / "app.py").write_text("# code")

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "status": "failed",
            "checkpoint": "generation_done",
        }
        mock_db.get_workspace.return_value = allocated_ws_doc

        store = self._make_store(mock_db)
        result = await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        assert result["workspace_warnings"] == []
        assert (tmp_artifacts / "est-1" / "ws-01-1" / "app.py").exists()
        assert (tmp_artifacts / "est-1" / "ws-01-2" / "app.py").exists()
        assert (tmp_artifacts / "est-1" / PARTIAL_OUTPUT_WARNING_FILE).exists()

        warning_text = (tmp_artifacts / "est-1" / PARTIAL_OUTPUT_WARNING_FILE).read_text()
        assert "PARTIAL OUTPUT WARNING" in warning_text
        assert "failed" in warning_text
        assert "generation_done" in warning_text

        mock_db.update_generation_session.assert_called_once()
        update_call = mock_db.update_generation_session.call_args[0][1]
        assert update_call["emergency_archived"] is True
        assert "outputs_archived" not in update_call
        assert "artifact_path" in update_call
        assert update_call["partial_archive_warnings"] == []

    @pytest.mark.asyncio
    async def test_skips_non_allocated_workspace_with_warning(
        self, tmp_path, tmp_artifacts, mock_db
    ):
        """One workspace not ALLOCATED → skipped with warning; other workspace archived."""
        ws1 = tmp_path / "ws-01-1"
        ws1.mkdir()
        (ws1 / "app.py").write_text("# code")

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "status": "failed",
            "checkpoint": "generation_done",
        }

        async def mock_get_workspace(ws_id):
            if ws_id == "ws-01-1":
                return {"status": "allocated"}
            return {"status": "cleaning"}

        mock_db.get_workspace.side_effect = mock_get_workspace

        store = self._make_store(mock_db)
        result = await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        assert len(result["workspace_warnings"]) == 1
        assert "ws-01-2" in result["workspace_warnings"][0]
        assert "cleaning" in result["workspace_warnings"][0]
        assert (tmp_artifacts / "est-1" / "ws-01-1" / "app.py").exists()

    @pytest.mark.asyncio
    async def test_skips_empty_workspace_with_warning(
        self, tmp_path, tmp_artifacts, mock_db, allocated_ws_doc
    ):
        """Workspace ALLOCATED but filesystem is empty → skipped with warning."""
        ws1 = tmp_path / "ws-01-1"
        ws1.mkdir()  # Empty directory

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1"],
            "status": "failed",
            "checkpoint": "generation_done",
        }
        mock_db.get_workspace.return_value = allocated_ws_doc

        store = self._make_store(mock_db)
        with pytest.raises(RuntimeError, match="could not archive any workspace"):
            await store.emergency_archive("est-1", workspace_base_path=tmp_path)

    @pytest.mark.asyncio
    async def test_skips_missing_workspace_filesystem_with_warning(
        self, tmp_path, tmp_artifacts, mock_db, allocated_ws_doc
    ):
        """Workspace ALLOCATED but directory does not exist → skipped with warning, raises if only one."""
        # ws-01-1 directory intentionally not created
        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1"],
            "status": "failed",
            "checkpoint": "generation_done",
        }
        mock_db.get_workspace.return_value = allocated_ws_doc

        store = self._make_store(mock_db)
        with pytest.raises(RuntimeError, match="could not archive any workspace"):
            await store.emergency_archive("est-1", workspace_base_path=tmp_path)

    @pytest.mark.asyncio
    async def test_raises_if_no_workspace_ids(self, tmp_artifacts, mock_db):
        """Generation with no workspace_ids → RuntimeError."""
        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": [],
            "status": "failed",
        }
        store = self._make_store(mock_db)
        with pytest.raises(RuntimeError, match="no workspace_ids"):
            await store.emergency_archive("est-1")

    @pytest.mark.asyncio
    async def test_warning_file_lists_workspace_issues(
        self, tmp_path, tmp_artifacts, mock_db
    ):
        """PARTIAL_OUTPUT_WARNING.txt should list per-workspace issues."""
        ws1 = tmp_path / "ws-01-1"
        ws1.mkdir()
        (ws1 / "app.py").write_text("# code")

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "status": "failed",
            "checkpoint": "deploy_and_e2e_done",
        }

        async def mock_get_workspace(ws_id):
            if ws_id == "ws-01-1":
                return {"status": "allocated"}
            return {"status": "cleaning"}

        mock_db.get_workspace.side_effect = mock_get_workspace

        store = self._make_store(mock_db)
        await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        warning_text = (tmp_artifacts / "est-1" / PARTIAL_OUTPUT_WARNING_FILE).read_text()
        assert "ws-01-2" in warning_text
        assert "deploy_and_e2e_done" in warning_text

    @pytest.mark.asyncio
    async def test_partial_archive_does_not_set_outputs_archived(
        self, tmp_path, tmp_artifacts, mock_db
    ):
        """
        Phase 2 regression: outputs_archived MUST NOT be True when any workspace
        was skipped. Commandment IV — archive is a hard precondition for completion.
        Setting it True on a partial archive would allow complete() to run with
        silently lost generated code.
        """
        ws1 = tmp_path / "ws-01-1"
        ws1.mkdir()
        (ws1 / "app.py").write_text("# code")
        # ws-01-2 directory intentionally absent (not even a dir — different from empty)

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "status": "failed",
            "checkpoint": "generation_done",
        }

        async def mock_get_workspace(ws_id):
            if ws_id == "ws-01-1":
                return {"status": "allocated"}
            return {"status": "cleaning"}  # non-ALLOCATED → skipped

        mock_db.get_workspace.side_effect = mock_get_workspace

        store = self._make_store(mock_db)
        await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        # outputs_archived must NOT be written as True when any workspace was skipped
        for call in mock_db.update_generation_session.call_args_list:
            update = call[0][1]
            assert update.get("outputs_archived") is not True, (
                "outputs_archived must not be True when ws-01-2 was skipped"
            )

    @pytest.mark.asyncio
    async def test_sets_emergency_archived_never_outputs_archived(
        self, tmp_path, tmp_artifacts, mock_db
    ):
        """emergency_archive always sets emergency_archived=True; never sets outputs_archived."""
        for ws_id in ("ws-01-1", "ws-01-2"):
            ws = tmp_path / ws_id
            ws.mkdir()
            (ws / "app.py").write_text("# code")

        mock_db.get_generation_session.return_value = {
            "outputs_archived": False,
            "workspace_ids": ["ws-01-1", "ws-01-2"],
            "status": "failed",
            "checkpoint": "generation_done",
        }
        mock_db.get_workspace.return_value = {"status": "allocated"}

        store = self._make_store(mock_db)
        await store.emergency_archive("est-1", workspace_base_path=tmp_path)

        mock_db.update_generation_session.assert_called_once()
        update = mock_db.update_generation_session.call_args[0][1]
        assert update["emergency_archived"] is True
        assert "outputs_archived" not in update
