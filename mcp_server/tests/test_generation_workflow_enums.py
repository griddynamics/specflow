"""
Regression guard for GenerationStatus and GenerationCheckpoint enum values.

These enums are defined independently in the MCP server package (cannot import
from backend). If the backend values ever change, these tests will catch the drift.

Expected values are written out as plain strings so drift in the enum itself
doesn't silently pass the test.
"""

from schemas.generation_workflow_enums import GenerationCheckpoint, GenerationStatus


class TestGenerationStatusValues:
    def test_pending(self):
        assert GenerationStatus.PENDING == "pending"

    def test_initializing(self):
        assert GenerationStatus.INITIALIZING == "initializing"

    def test_running(self):
        assert GenerationStatus.RUNNING == "running"

    def test_completed(self):
        assert GenerationStatus.COMPLETED == "completed"

    def test_failed(self):
        assert GenerationStatus.FAILED == "failed"

    def test_cancelled(self):
        assert GenerationStatus.CANCELLED == "cancelled"

    def test_no_extra_values(self):
        expected = {"pending", "initializing", "running", "completed", "failed", "cancelled"}
        assert {s.value for s in GenerationStatus} == expected


class TestGenerationCheckpointValues:
    def test_files_uploaded(self):
        assert GenerationCheckpoint.FILES_UPLOADED == "files_uploaded"

    def test_contract_validated(self):
        assert GenerationCheckpoint.CONTRACT_VALIDATED == "contract_validated"

    def test_kb_init_done(self):
        assert GenerationCheckpoint.KB_INIT_DONE == "kb_init_done"

    def test_generation_started(self):
        assert GenerationCheckpoint.GENERATION_STARTED == "generation_started"

    def test_generation_done(self):
        assert GenerationCheckpoint.GENERATION_DONE == "generation_done"

    def test_deploy_and_e2e_done(self):
        assert GenerationCheckpoint.DEPLOY_AND_E2E_DONE == "deploy_and_e2e_done"

    def test_outputs_archived(self):
        assert GenerationCheckpoint.OUTPUTS_ARCHIVED == "outputs_archived"

    def test_estimation_done(self):
        assert GenerationCheckpoint.ESTIMATION_DONE == "estimation_done"

    def test_no_extra_values(self):
        expected = {
            "files_uploaded", "contract_validated", "kb_init_done",
            "generation_started", "generation_done", "deploy_and_e2e_done",
            "outputs_archived", "estimation_done",
        }
        assert {c.value for c in GenerationCheckpoint} == expected


class TestStrEnumBehaviour:
    """Verify str-Enum comparison semantics that the server relies on."""

    def test_enum_equals_plain_string(self):
        assert GenerationStatus.RUNNING == "running"
        assert "running" == GenerationStatus.RUNNING

    def test_enum_in_plain_string_tuple(self):
        statuses = ("running", "initializing")
        assert GenerationStatus.RUNNING in statuses
        assert GenerationStatus.INITIALIZING in statuses
        assert GenerationStatus.COMPLETED not in statuses

    def test_plain_string_in_enum_tuple(self):
        assert "running" in (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING)
        assert "completed" not in (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING)

    def test_enum_as_dict_key_plain_string_lookup(self):
        d = {GenerationCheckpoint.KB_INIT_DONE: "label"}
        assert d.get("kb_init_done") == "label"
        assert d.get("unknown") is None

    def test_json_dumps_serializes_as_string(self):
        import json
        result = json.dumps({"status": GenerationStatus.RUNNING})
        assert result == '{"status": "running"}'
