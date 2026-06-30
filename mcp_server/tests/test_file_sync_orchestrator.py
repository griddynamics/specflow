"""
Tests for FileSyncOrchestrator — focused on src_dir-absent behavior.

Validates that both the planning and spec-analysis flows behave identically
when the user doesn't provide (or the default) src_dir doesn't exist on disk:
- Archive is built without src (spec-only or spec+outputs)
- Backend receives correct workspace-relative paths
- No exception is raised
"""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.file_sync_orchestrator import FileSyncOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tar_names(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return set(tar.getnames())


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _backend_response(generation_id: str = "est-abc") -> str:
    return json.dumps({
        "generation_id": generation_id,
        "workspace_ids": ["ws-01"],
        "status": "ok",
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """Minimal project tree: specs/ exists, src/ does NOT."""
    spec_dir = tmp_path / "project" / "specs"
    _write(spec_dir / "requirements.md")
    return tmp_path / "project"


@pytest.fixture
def project_with_src(tmp_path):
    """Project tree: specs/ and src/ both exist."""
    root = tmp_path / "project"
    _write(root / "specs" / "requirements.md")
    _write(root / "src" / "main.py")
    return root


@pytest.fixture
def project_with_outputs(tmp_path):
    """Project tree: specs/ and docs/ (outputs) exist, src/ does NOT."""
    root = tmp_path / "project"
    _write(root / "specs" / "requirements.md")
    _write(root / "docs" / "plan.md")
    return root


# ---------------------------------------------------------------------------
# Core: no src_dir on disk — spec-only archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_spec_only_when_src_absent(project):
    """When src/ doesn't exist, archive contains only specs — no error."""
    spec_dir = project / "specs"
    src_dir = project / "src"  # does not exist

    captured: dict = {}

    async def fake_upload(endpoint, file_data, filename, form_data, timeout_seconds):
        captured["archive"] = file_data
        captured["params"] = json.loads(form_data["params"])
        return _backend_response()

    mock_backend = MagicMock()
    mock_backend.upload_file = fake_upload

    with patch("services.file_sync_orchestrator.SpecFlowBackendService", return_value=mock_backend):
        result = await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir,
        )

    names = _tar_names(captured["archive"])
    assert "specs/requirements.md" in names
    assert not any(n.startswith("src/") for n in names)

    params = captured["params"]
    assert params["spec_path"] == "specs"
    # src_dir is a placeholder name — the backend won't find files there but won't crash
    assert params["src_dir"] == "src"

    assert result["generation_id"] == "est-abc"
    assert result["spec_path"] == "specs"


# ---------------------------------------------------------------------------
# Planning: src_dir absent + outputs present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_spec_and_outputs_when_src_absent(project_with_outputs):
    """Planning: spec + outputs archived when src is absent."""
    root = project_with_outputs
    spec_dir = root / "specs"
    src_dir = root / "src"        # does not exist
    outputs_dir = root / "docs"   # exists

    captured: dict = {}

    async def fake_upload(endpoint, file_data, filename, form_data, timeout_seconds):
        captured["archive"] = file_data
        captured["params"] = json.loads(form_data["params"])
        return _backend_response()

    mock_backend = MagicMock()
    mock_backend.upload_file = fake_upload

    with patch("services.file_sync_orchestrator.SpecFlowBackendService", return_value=mock_backend):
        await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir,
            outputs_dir=outputs_dir,
        )

    names = _tar_names(captured["archive"])
    assert "specs/requirements.md" in names
    assert "docs/plan.md" in names
    assert not any(n.startswith("src/") for n in names)

    params = captured["params"]
    assert params["spec_path"] == "specs"
    assert params["src_dir"] == "src"  # placeholder
    assert params["outputs_dir"] == "docs"


# ---------------------------------------------------------------------------
# When src DOES exist: included in archive (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_includes_src_when_present(project_with_src):
    """When src/ exists, it IS included in the archive."""
    root = project_with_src
    spec_dir = root / "specs"
    src_dir = root / "src"

    captured: dict = {}

    async def fake_upload(endpoint, file_data, filename, form_data, timeout_seconds):
        captured["archive"] = file_data
        captured["params"] = json.loads(form_data["params"])
        return _backend_response()

    mock_backend = MagicMock()
    mock_backend.upload_file = fake_upload

    with patch("services.file_sync_orchestrator.SpecFlowBackendService", return_value=mock_backend):
        await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir,
        )

    names = _tar_names(captured["archive"])
    assert "specs/requirements.md" in names
    assert "src/main.py" in names

    params = captured["params"]
    assert params["spec_path"] == "specs"
    assert params["src_dir"] == "src"


# ---------------------------------------------------------------------------
# Parity: spec-check flow vs planning flow produce identical archives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_check_and_planning_flows_identical_when_no_src(project_with_outputs):
    """
    full_spec_checklist resolves src_dir="src" → project_root/src.
    run_planning resolves src_dir=None → project_root/src.
    Both paths are identical; this test confirms the archive content is the same.
    """
    root = project_with_outputs
    spec_dir = root / "specs"
    src_dir_default = root / "src"   # does not exist in this fixture

    archives = {}

    async def make_fake_upload(key):
        async def fake_upload(endpoint, file_data, filename, form_data, timeout_seconds):
            archives[key] = file_data
            return _backend_response()
        return fake_upload

    mock_backend_spec = MagicMock()
    mock_backend_spec.upload_file = await make_fake_upload("spec_check")

    mock_backend_plan = MagicMock()
    mock_backend_plan.upload_file = await make_fake_upload("planning")

    # Simulate full_spec_checklist path: src_dir always resolved, may not exist
    with patch("services.file_sync_orchestrator.SpecFlowBackendService", return_value=mock_backend_spec):
        await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir_default,   # resolved from default "src" string
            workflow_type="spec_completeness_check",
        )

    # Simulate run_planning path: src_dir=None → project_root/src (same path)
    with patch("services.file_sync_orchestrator.SpecFlowBackendService", return_value=mock_backend_plan):
        await FileSyncOrchestrator.sync_files_to_backend(
            spec_dir=spec_dir,
            src_dir=src_dir_default,   # resolved from None → project_root/"src"
            workflow_type="planning",
        )

    assert _tar_names(archives["spec_check"]) == _tar_names(archives["planning"])
