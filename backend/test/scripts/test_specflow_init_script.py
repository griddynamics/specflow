"""Static regression tests for the local quickstart bootstrap shell script."""

from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[3] / "specflow-init.sh"
_COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _script_text() -> str:
    return _SCRIPT.read_text()


def _compose_text() -> str:
    return _COMPOSE.read_text()


def test_quickstart_seeds_pool_directly_without_workspaces_json():
    """The quickstart provisions repos straight into the DB — no workspaces.json handoff.

    Regression guard for the flat-file removal: the provisioner must NOT be invoked with
    --skip-firestore or --output-workspace-config, and .specflow-local/workspaces.json must
    never be referenced.
    """
    text = _script_text()

    assert "--output-workspace-config" not in text
    assert "--skip-firestore" not in text
    assert "workspaces.json" not in text
    # init_db.py still runs (API key + identity), but without a --workspace-config file.
    assert "uv run scripts/init_db.py" in text
    assert "--workspace-config" not in text


def test_docker_starts_before_workspace_config_generation():
    """No-arg quickstart starts backend before GitHub/P10Y workspace prep."""
    text = _script_text()

    compose_pos = text.index('docker compose -f "${SCRIPT_DIR}/docker-compose.yml" up -d')
    repo_script_pos = text.index("uv run python scripts/create_generation_session_repos.py")
    seed_pos = text.index("uv run scripts/init_db.py")

    assert compose_pos < repo_script_pos < seed_pos


def test_subprocess_output_is_sanitized_before_init_log():
    """Subprocess output must pass through log_stream before reaching init.log."""
    text = _script_text()

    assert "log_stream()" in text
    assert "> >(log_stream) 2> >(log_stream)" in text
    assert '>> "${LOG_FILE}" 2>&1' not in text
    assert "input_value=" in text


def test_no_firestore_emulator_services_in_default_stack():
    """The firestore-emulator/exporter services and their entrypoint scripts must be gone —
    sqlite is now the sole local/Docker default; Firestore only connects to an already-hosted
    instance. Regression guard against silently reintroducing the removed local emulator."""
    text = _compose_text()

    assert "firestore-emulator:" not in text
    assert "firestore-exporter:" not in text
    assert not (
        Path(__file__).resolve().parents[3] / "scripts/firestore-emulator-entrypoint.sh"
    ).exists()
    assert not (
        Path(__file__).resolve().parents[3] / "scripts/firestore-emulator-exporter.sh"
    ).exists()


def test_compose_backend_bind_mounts_specflow_home_for_sqlite():
    """Backend must bind-mount the host's ~/.specflow/ directory (central SQLite db, shared
    with the host-side TUI/CLI config) and default DATABASE_TYPE to sqlite."""
    text = _compose_text()

    assert "DATABASE_TYPE=${DATABASE_TYPE:-sqlite}" in text
    # Container path is fixed (not .env-overridable); isolation is via the bind mount below.
    assert "SQLITE_DB_PATH=/root/.specflow/db/specflow.db" in text
    assert "${SPECFLOW_HOME_MOUNT_PATH:-${HOME}/.specflow}:/root/.specflow:rw" in text


def test_makefile_test_stack_uses_separate_names_ports_and_isolated_sqlite_path():
    """Integration/E2E stacks must not collide with quickstart containers, host ports, or the
    real central SQLite database at ~/.specflow/db/specflow.db."""
    makefile_text = (Path(__file__).resolve().parents[3] / "Makefile").read_text()

    assert "$(TEST_STACK_TARGETS): export SPECFLOW_BACKEND_PORT := 18000" in makefile_text
    assert "$(TEST_STACK_TARGETS): export BACKEND_URL := http://localhost:18000" in makefile_text
    assert "$(TEST_STACK_TARGETS): export GCP_PROJECT_ID := $(GCP_PROJECT_ID)" in makefile_text
    assert (
        "$(TEST_STACK_TARGETS): export FIRESTORE_DATABASE_NAME := $(FIRESTORE_DATABASE_NAME)"
        in makefile_text
    )
    assert (
        "$(TEST_STACK_TARGETS): export SPECFLOW_HOME_MOUNT_PATH := $(TEST_SPECFLOW_HOME_PATH)"
        in makefile_text
    )
    assert (
        "$(TEST_STACK_TARGETS): export SQLITE_DB_PATH := $(TEST_SPECFLOW_HOME_PATH)/db/specflow.db"
        in makefile_text
    )
    # The isolated sqlite-home path must nest under the test workspace mount so `make stop`'s
    # existing `rm -rf "$(WORKSPACE_MOUNT_PATH)"` also wipes it — no separate cleanup needed.
    assert (
        "TEST_SPECFLOW_HOME_PATH := $(TEST_WORKSPACE_MOUNT_PATH)/specflow-home" in makefile_text
    )


def test_specflow_init_defaults_local_firestore_database_name():
    """Host-side seeding and Compose must agree on the hosted-Firestore database name
    (only relevant when DATABASE_TYPE=firestore/emulator; sqlite ignores it)."""
    text = _script_text()

    assert 'export FIRESTORE_DATABASE_NAME="${FIRESTORE_DATABASE_NAME:-specflow}"' in text


def test_reset_local_db_clears_sqlite_file_not_a_directory():
    """A reset must clear the central SQLite file (+ WAL/SHM sidecars) that survives
    docker compose down -v, guarded against clearing an unexpected path."""
    text = _script_text()

    assert 'SPECFLOW_HOME_PATH="${SPECFLOW_HOME_MOUNT_PATH:-${HOME}/.specflow}"' in text
    assert 'basename "${SPECFLOW_HOME_PATH}"' in text
    assert '!= ".specflow"' in text
    assert 'rm -f "${SPECFLOW_HOME_PATH}/db/specflow.db"' in text


def test_host_side_seed_targets_host_db_path_not_container_internal():
    """Host-side seeding runs via `uv run` on the host, so it must target the host
    bind-mount SOURCE derived from SPECFLOW_HOME_PATH — never the container-internal
    SQLITE_DB_PATH from .env (/root/...), which is unwritable for non-root users and
    would seed a file the container never reads. Regression guard for the quickstart
    seeding-path bug."""
    text = _script_text()

    # The host seed path is derived from SPECFLOW_HOME_PATH, not inherited from the
    # container-internal SQLITE_DB_PATH env var.
    assert '_SQLITE_DB_PATH="${SPECFLOW_HOME_PATH}/db/specflow.db"' in text
    assert '_SQLITE_DB_PATH="${SQLITE_DB_PATH:-' not in text
