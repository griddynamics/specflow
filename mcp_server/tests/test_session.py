"""Unit tests for MCP session/path resolution (services/session.py)."""

import json

import pytest
from pydantic import BaseModel, FileUrl, ValidationError

from services import session
from services.file_sync import SESSION_FILENAME


@pytest.fixture(autouse=True)
def _reset_project_root():
    """session._project_root is module-global; isolate every test."""
    saved = session._project_root
    session._project_root = None
    yield
    session._project_root = saved


class _Root(BaseModel):
    """Stand-in for an MCP ListRoots entry whose uri is a real FileUrl."""

    uri: FileUrl


def _file_url(path: str) -> FileUrl:
    return _Root(uri=f"file://{path}").uri


class _Ctx:
    def __init__(self, *, roots=None, raises=None):
        self._roots = roots or []
        self._raises = raises

    async def list_roots(self):
        if self._raises is not None:
            raise self._raises
        return self._roots


# --------------------------------------------------------------------------- #
# session_file / set_project_root
# --------------------------------------------------------------------------- #


def test_session_file_uses_explicit_root(tmp_path):
    assert session.session_file(tmp_path) == tmp_path / SESSION_FILENAME


def test_session_file_uses_global_root(tmp_path):
    session.set_project_root(tmp_path)
    assert session.session_file() == tmp_path / SESSION_FILENAME


def test_session_file_raises_when_root_unknown():
    with pytest.raises(RuntimeError, match="project_root is not known"):
        session.session_file()


# --------------------------------------------------------------------------- #
# write_session / load_session round-trip
# --------------------------------------------------------------------------- #


def test_write_then_load_round_trip(tmp_path):
    written = session.write_session("gen-123", tmp_path)
    assert written == tmp_path / SESSION_FILENAME
    body = json.loads(written.read_text())
    assert body == {"generation_id": "gen-123", "project_root": str(tmp_path)}

    assert session.load_session(tmp_path) == "gen-123"


def test_load_session_missing_file_returns_none(tmp_path):
    assert session.load_session(tmp_path) is None


def test_load_session_restores_global_root_after_restart(tmp_path):
    session.write_session("gen-9", tmp_path)
    # Simulate a fresh MCP process: global root unknown, no arg passed... but
    # load_session needs *some* root to find the file, so seed then clear.
    session.set_project_root(tmp_path)
    eid = session.load_session()
    session._project_root = None
    # When the global is None, load_session restores it from the file's project_root.
    eid2 = session.load_session(tmp_path)
    assert eid == "gen-9"
    assert eid2 == "gen-9"
    assert session._project_root == tmp_path


def test_load_session_malformed_json_returns_none(tmp_path):
    (tmp_path / SESSION_FILENAME).write_text("{not valid json")
    assert session.load_session(tmp_path) is None


def test_load_session_without_generation_id_returns_none(tmp_path):
    (tmp_path / SESSION_FILENAME).write_text(json.dumps({"project_root": str(tmp_path)}))
    assert session.load_session(tmp_path) is None


# --------------------------------------------------------------------------- #
# resolve_generation_id
# --------------------------------------------------------------------------- #


def test_resolve_generation_id_prefers_explicit_arg(tmp_path):
    session.write_session("from-file", tmp_path)
    assert session.resolve_generation_id("explicit", tmp_path) == "explicit"


def test_resolve_generation_id_falls_back_to_session(tmp_path):
    session.write_session("from-file", tmp_path)
    assert session.resolve_generation_id(None, tmp_path) == "from-file"


# --------------------------------------------------------------------------- #
# resolve_path
# --------------------------------------------------------------------------- #


def test_resolve_path_absolute_returned_as_is(tmp_path):
    abs_path = tmp_path / "specs"
    assert session.resolve_path(str(abs_path)) == abs_path


def test_resolve_path_relative_uses_global_root(tmp_path):
    session.set_project_root(tmp_path)
    assert session.resolve_path("specs") == tmp_path / "specs"


def test_resolve_path_relative_without_root_raises():
    with pytest.raises(ValueError, match="project root is not known"):
        session.resolve_path("specs")


# --------------------------------------------------------------------------- #
# ensure_directory_exists
# --------------------------------------------------------------------------- #


def test_ensure_directory_exists_returns_none_when_present(tmp_path):
    assert session.ensure_directory_exists(tmp_path, "specs") is None


def test_ensure_directory_exists_error_for_missing(tmp_path):
    missing = tmp_path / "nope"
    err = session.ensure_directory_exists(missing, "specs")
    data = json.loads(err)
    assert data["error"] == "specs not found"
    assert str(missing) in data["message"]
    assert data["hint"] == "Verify the path exists and is accessible."


def test_ensure_directory_exists_relative_hint(tmp_path):
    missing = tmp_path / "nope"
    err = session.ensure_directory_exists(missing, "specs", original_path="specs")
    data = json.loads(err)
    assert "relative path" in data["hint"]


# --------------------------------------------------------------------------- #
# apply_project_root_from_context (async)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_context_short_circuits_when_already_set(tmp_path):
    session.set_project_root(tmp_path)

    class _Boom:
        async def list_roots(self):
            raise AssertionError("must not be called when root already known")

    await session.apply_project_root_from_context(_Boom())
    assert session._project_root == tmp_path


@pytest.mark.asyncio
async def test_context_sets_root_from_file_url(tmp_path):
    root = type("R", (), {"uri": _file_url(str(tmp_path))})()
    await session.apply_project_root_from_context(_Ctx(roots=[root]))
    assert session._project_root == tmp_path


@pytest.mark.asyncio
async def test_context_no_roots_leaves_root_unset(tmp_path):
    await session.apply_project_root_from_context(_Ctx(roots=[]))
    assert session._project_root is None


@pytest.mark.asyncio
async def test_context_non_file_url_uri_ignored(tmp_path):
    root = type("R", (), {"uri": "not-a-file-url"})()
    await session.apply_project_root_from_context(_Ctx(roots=[root]))
    assert session._project_root is None


@pytest.mark.asyncio
async def test_context_extracts_raw_path_from_validation_error(tmp_path):
    # Cursor sends raw paths instead of file:// URLs → pydantic ValidationError
    # whose error 'input' is the raw path; the code recovers it.
    try:
        _Root(uri=str(tmp_path))
        raise AssertionError("expected ValidationError")
    except ValidationError as ve:
        err = ve

    await session.apply_project_root_from_context(_Ctx(raises=err))
    assert session._project_root == tmp_path


@pytest.mark.asyncio
async def test_context_generic_exception_swallowed(tmp_path):
    await session.apply_project_root_from_context(_Ctx(raises=RuntimeError("boom")))
    assert session._project_root is None
