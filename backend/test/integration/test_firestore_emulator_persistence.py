"""Round-trip persistence test for the local-quickstart Firestore emulator.

Proves the behaviour that ``docker-compose.yml`` + ``specflow-init.sh`` only assert
*textually* elsewhere: a document written to the emulator survives a full
``docker compose down`` / ``up`` cycle via the native ``--export-on-exit`` /
``--import-data`` flags over the host bind mount.

This is a real system test — it drives the actual emulator container — so it is
gated behind ``RUN_FIRESTORE_PERSISTENCE_TEST=1`` and never runs in ``make
unit-tests``. It also needs port 8080 free, so it skips if a dev emulator is
already up to avoid clobbering it.

Run it with::

    RUN_FIRESTORE_PERSISTENCE_TEST=1 uv run pytest \\
        test/integration/test_firestore_emulator_persistence.py -v
"""
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from google.cloud import firestore

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_SERVICE = "firestore-emulator"
_EMULATOR_HOST = "localhost:8080"
_PROJECT_ID = "local-dev"
_DATABASE_ID = "specflow"
_COLLECTION = "persistence_probe"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FIRESTORE_PERSISTENCE_TEST") != "1",
    reason="System test; set RUN_FIRESTORE_PERSISTENCE_TEST=1 (needs docker + free port 8080).",
)


def _port_open(host: str = "localhost", port: int = 8080) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _compose(*args: str, env: dict) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args],
        cwd=str(_REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _wait_until_ready(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.run(
                ["curl", "-sf", "--max-time", "2", f"http://{_EMULATOR_HOST}"],
                check=True,
                capture_output=True,
            )
            return
        except subprocess.CalledProcessError:
            time.sleep(2)
    raise TimeoutError(f"Emulator not ready within {timeout}s")


def _latest_export_mtime(export_dir: Path) -> float:
    metadata_files = list(export_dir.rglob("*overall_export_metadata"))
    if not metadata_files:
        return 0.0
    return max(path.stat().st_mtime for path in metadata_files)


def _wait_for_export(export_dir: Path, newer_than: float = 0.0, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _latest_export_mtime(export_dir) > newer_than:
            return
        time.sleep(1)
    raise TimeoutError(f"Export metadata not written under {export_dir}")


def _client() -> firestore.Client:
    os.environ["FIRESTORE_EMULATOR_HOST"] = _EMULATOR_HOST
    return firestore.Client(project=_PROJECT_ID, database=_DATABASE_ID)


def test_document_survives_compose_down_up():
    if _port_open():
        pytest.skip("Port 8080 already in use — refusing to clobber a running emulator.")

    with tempfile.TemporaryDirectory(prefix="fs-persist-") as tmp:
        # Isolate the bind mount from the developer's real ./workspaces.
        env = {
            **os.environ,
            "WORKSPACE_MOUNT_PATH": tmp,
            "FIRESTORE_EMULATOR_HOST": _EMULATOR_HOST,
            "GCP_PROJECT_ID": _PROJECT_ID,
            "FIRESTORE_DATABASE_NAME": _DATABASE_ID,
        }
        export_dir = Path(tmp) / "firestore_emulator" / "current"
        doc_id = "probe-doc"
        sentinel = f"persisted-{int(time.time())}"

        try:
            # --- First boot: empty DB, write a sentinel doc ---
            _compose("up", "-d", _SERVICE, env=env)
            _wait_until_ready()

            client = _client()
            client.collection(_COLLECTION).document(doc_id).set({"value": sentinel})

            # --- Graceful teardown: --export-on-exit must flush to the host dir ---
            _compose("down", env=env)
            assert any(export_dir.rglob("*overall_export_metadata")), (
                "Emulator did not export on graceful shutdown — export-on-exit / "
                "stop_grace_period / init regression."
            )

            # --- Second boot: must --import-data and serve the doc again ---
            _compose("up", "-d", _SERVICE, env=env)
            _wait_until_ready()

            restored = _client().collection(_COLLECTION).document(doc_id).get()
            assert restored.exists, "Document was lost across compose down/up."
            assert restored.to_dict()["value"] == sentinel
        finally:
            _compose("down", env=env)


def test_document_survives_emulator_kill_after_periodic_export():
    if _port_open():
        pytest.skip("Port 8080 already in use — refusing to clobber a running emulator.")

    with tempfile.TemporaryDirectory(prefix="fs-periodic-") as tmp:
        env = {
            **os.environ,
            "WORKSPACE_MOUNT_PATH": tmp,
            "FIRESTORE_EMULATOR_HOST": _EMULATOR_HOST,
            "GCP_PROJECT_ID": _PROJECT_ID,
            "FIRESTORE_DATABASE_NAME": _DATABASE_ID,
            "FIRESTORE_EXPORT_INTERVAL_SECONDS": "2",
        }
        export_dir = Path(tmp) / "firestore_emulator" / "current"
        doc_id = "periodic-probe-doc"
        sentinel = f"periodic-{int(time.time())}"

        try:
            _compose("up", "-d", _SERVICE, "firestore-exporter", env=env)
            _wait_until_ready()

            last_export_mtime = _latest_export_mtime(export_dir)
            _client().collection(_COLLECTION).document(doc_id).set({"value": sentinel})
            _wait_for_export(export_dir, newer_than=last_export_mtime)

            # SIGKILL bypasses gcloud's --export-on-exit path; restore must use the sidecar snapshot.
            _compose("kill", "-s", "KILL", _SERVICE, env=env)
            _wait_until_ready()

            restored = _client().collection(_COLLECTION).document(doc_id).get()
            assert restored.exists, "Document was lost after emulator kill/restart."
            assert restored.to_dict()["value"] == sentinel
        finally:
            _compose("down", env=env)
