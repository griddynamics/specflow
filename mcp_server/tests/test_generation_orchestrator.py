"""Unit tests for GenerationOrchestrator helpers."""

from services.generation_orchestrator import AuthMeSnapshot, resolve_relative_outputs_dir


class TestAuthMeSnapshotFromJson:
    def test_parses_full_payload(self):
        snap = AuthMeSnapshot.from_json(
            {
                "current_process": "est-1",
                "at_capacity": True,
                "max_concurrent_sessions": 3,
                "active_generation_sessions": [{"generation_id": "est-1"}],
            }
        )
        assert snap.recoverable_session == "est-1"
        assert snap.at_capacity is True
        assert snap.max_concurrent_sessions == 3
        assert snap.active_generation_sessions == ({"generation_id": "est-1"},)

    def test_defaults_when_fields_missing(self):
        snap = AuthMeSnapshot.from_json({})
        assert snap.recoverable_session is None
        assert snap.at_capacity is False
        # Falls back to the documented default of 5 concurrent sessions.
        assert snap.max_concurrent_sessions == 5
        assert snap.active_generation_sessions == ()

    def test_filters_non_dict_session_rows(self):
        snap = AuthMeSnapshot.from_json(
            {"active_generation_sessions": [{"generation_id": "a"}, "garbage", None]}
        )
        assert snap.active_generation_sessions == ({"generation_id": "a"},)

    def test_null_max_concurrent_uses_default(self):
        snap = AuthMeSnapshot.from_json({"max_concurrent_sessions": None})
        assert snap.max_concurrent_sessions == 5


class TestResolveRelativeOutputsDir:
    """The backend must receive a workspace-relative outputs path that does not lose the
    parent segment of a nested outputs_dir (regression: Path(...).name truncation)."""

    def test_prefers_sync_computed_value(self):
        # The archive-relative value from sync always wins, even when nested.
        assert resolve_relative_outputs_dir("build/docs", "docs") == "build/docs"
        assert resolve_relative_outputs_dir("docs", "/abs/docs") == "docs"

    def test_fallback_preserves_nested_relative_path(self):
        # No sync value → must NOT collapse "build/docs" to "docs".
        assert resolve_relative_outputs_dir(None, "build/docs") == "build/docs"
        assert resolve_relative_outputs_dir("", "build/docs") == "build/docs"

    def test_fallback_strips_leading_dot_slash(self):
        assert resolve_relative_outputs_dir(None, "./docs") == "docs"

    def test_fallback_basenames_absolute_path(self):
        assert resolve_relative_outputs_dir(None, "/Users/x/project/docs") == "docs"

    def test_fallback_simple_relative_unchanged(self):
        assert resolve_relative_outputs_dir(None, "docs") == "docs"
