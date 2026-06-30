"""Static regression tests for the local quickstart bootstrap shell script."""

from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[3] / "specflow-init.sh"
_COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _script_text() -> str:
    return _SCRIPT.read_text()


def _compose_text() -> str:
    return _COMPOSE.read_text()


def test_workspace_config_generation_skips_firestore_write():
    """The quickstart emits workspaces.json, then init_firestore.py seeds emulator state."""
    text = _script_text()

    assert "--output-workspace-config" in text
    assert "--skip-firestore" in text


def test_docker_starts_before_workspace_config_generation():
    """No-arg quickstart starts emulator/backend before GitHub/P10Y workspace prep."""
    text = _script_text()

    compose_pos = text.index('docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d')
    repo_script_pos = text.index("uv run python scripts/create_generation_session_repos.py")
    seed_pos = text.index("uv run scripts/init_firestore.py")

    assert compose_pos < repo_script_pos < seed_pos


def test_subprocess_output_is_sanitized_before_init_log():
    """Subprocess output must pass through log_stream before reaching init.log."""
    text = _script_text()

    assert "log_stream()" in text
    assert "> >(log_stream) 2> >(log_stream)" in text
    assert '>> "${LOG_FILE}" 2>&1' not in text
    assert "input_value=" in text


def test_firestore_emulator_persists_under_workspace_mount():
    """Local quickstart should keep emulator exports beside workspace artifacts."""
    text = _compose_text()
    entrypoint_text = (
        Path(__file__).resolve().parents[3] / "scripts/firestore-emulator-entrypoint.sh"
    ).read_text()

    assert "${WORKSPACE_MOUNT_PATH:-./workspaces}/firestore_emulator:/firestore-data:rw" in text
    assert "firestore-emulator-entrypoint.sh" in text
    assert "firestore-exporter" in text
    assert "firestore-emulator-exporter.sh" in text
    assert "FIRESTORE_EXPORT_INTERVAL_SECONDS" in text
    assert "${FIRESTORE_DATABASE_NAME:-specflow}" in text
    assert "export_metadata_file()" in entrypoint_text
    assert "CURRENT_METADATA_FILE=" in entrypoint_text
    assert "IMPORT_ARGS=\"--import-data=${CURRENT_METADATA_FILE}\"" in entrypoint_text
    assert "--import-data=${CURRENT_EXPORT_DIR}" not in entrypoint_text
    assert "--export-on-exit=\"${CURRENT_EXPORT_DIR}\"" in entrypoint_text


def test_compose_shutdown_order_keeps_exporter_after_backend():
    """Backend must stop before exporter so shutdown state is included in the final export."""
    text = _compose_text()

    backend_pos = text.index("  backend:")
    exporter_dependency_pos = text.index("      firestore-exporter:", backend_pos)
    exporter_pos = text.index("  firestore-exporter:")
    emulator_dependency_pos = text.index("      firestore-emulator:", exporter_pos)

    assert backend_pos < exporter_dependency_pos
    assert exporter_pos < emulator_dependency_pos
    assert text.count("stop_grace_period: 120s") >= 3


def test_makefile_test_stack_uses_separate_names_and_ports():
    """Integration/E2E stacks must not collide with quickstart containers or host ports."""
    makefile_text = (Path(__file__).resolve().parents[3] / "Makefile").read_text()

    assert (
        "$(TEST_STACK_TARGETS): export SPECFLOW_FIRESTORE_EXPORTER_CONTAINER := "
        "specflow-test-firestore-exporter"
    ) in makefile_text
    assert "$(TEST_STACK_TARGETS): export SPECFLOW_BACKEND_PORT := 18000" in makefile_text
    assert "$(TEST_STACK_TARGETS): export SPECFLOW_FIRESTORE_PORT := 18080" in makefile_text
    assert "$(TEST_STACK_TARGETS): export BACKEND_URL := http://localhost:18000" in makefile_text
    assert "$(TEST_STACK_TARGETS): export FIRESTORE_EMULATOR_HOST := localhost:18080" in makefile_text


def test_specflow_init_defaults_local_firestore_database_name():
    """Host-side seeding and Compose must agree on the quickstart database name."""
    text = _script_text()

    assert 'export FIRESTORE_DATABASE_NAME="${FIRESTORE_DATABASE_NAME:-specflow}"' in text


def test_reset_local_db_clears_persisted_emulator_export_only():
    """A reset must clear the host export dir that survives docker compose down -v."""
    text = _script_text()

    assert 'FIRESTORE_EMULATOR_DATA_DIR="${SCRIPT_DIR}/${_WORKSPACE_MOUNT_PATH#./}' in text
    assert 'basename "${FIRESTORE_EMULATOR_DATA_DIR}"' in text
    assert '!= "firestore_emulator"' in text
    assert 'rm -rf "${FIRESTORE_EMULATOR_DATA_DIR}"' in text
