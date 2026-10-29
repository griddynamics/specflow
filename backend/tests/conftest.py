from __future__ import annotations

import os

# onnxruntime (imported by the PDF parser) sends telemetry and its exit-time thread can
# abort the interpreter (rare exit-134 after all tests passed): opt out for the suite.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

# Central-logger env guard. Force these before any
# other import runs, so this beats both the shell environment and any
# `.env`/`load_dotenv` a test might trigger later. The logger writes only
# to stdout (one JSON object per line), so there is no log directory to
# guard; the `central_log` harness lowers `LOGS_STDOUT_LEVEL` per test.
# `LOGS_RAISE_EXCEPTIONS` turns on the logger's development mode (U4): a
# failing handler or a malformed record dumps loudly to stderr.
os.environ.update(
    {
        "LOGS_LEVEL": "INFO",
        "LOGS_STDOUT_LEVEL": "WARNING",
        "LOGS_RAISE_EXCEPTIONS": "1",
    }
)

from collections.abc import Iterator
from pathlib import Path

import pytest

import llm.sandbox as sandbox_module
import logger._stderr as stderr_module
import logger.correlation as correlation_module

# Registers the `central_log` harness fixture and the tier-2 `temporal_live` fixture
# for every test module (plain imports of a fixture into a test module are unused-import
# and redefinition findings).
pytest_plugins = [
    "logger_harness",
    "specflow_live",
    "specflow_api_support",
    "specflow_api_live_support",
    "specflow_auth_support",
    "specflow_db_live_support",
    "specflow_sources_support",
]


@pytest.fixture(autouse=True)
def available_native_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep offline tests independent from optional host sandbox tooling."""

    monkeypatch.setattr(
        sandbox_module,
        "check_native_sandbox_available",
        lambda: None,
    )


@pytest.fixture(autouse=True)
def _worker_diagnostics_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TEMPORARY (worker-parse freeze investigation): the worker runner's
    diagnostics state file goes to the test's tmp dir, never to /tmp."""

    monkeypatch.setenv("WORKER_DIAG_STATE", str(tmp_path / "worker-diag-state.json"))


@pytest.fixture(autouse=True)
def _reset_logger_correlation() -> Iterator[None]:
    """Reset central-logger correlation state after every test."""

    yield
    correlation_module._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_logger_stderr() -> Iterator[None]:
    """Reset central-logger `_stderr` dedup/rate-limit state after every test.

    Needed because several LOG-2x tests (LOG-21, LOG-22, LOG-27) depend on
    their warning firing for the *first* time within their own test —
    without this reset, the second test in a run to hit the same
    dedup/rate-limit key would see no warning at all.
    """

    yield
    stderr_module._reset()


# --- specflow settings and live-tier support (hello-stack S1) -------------

# Prefixes of every variable the specflow settings read. A test starts from an
# environment without any of them, so the developer's shell or `--env-file`
# never leaks in. Written out here, not read from settings.
_SPECFLOW_ENV_PREFIXES = (
    "APP_",
    "API_",
    "DATABASE_",
    "AUTH_",
    "OIDC_",
    "DRIVE_",
    "STORAGE_",
    "GOOGLE_",
    "TEMPORAL_",
    "WORKER_",
    "NEO4J_",
    "AGENT_",
    "LLM_GATEWAY_",
    "LANGFUSE_",
    "KUBERNETES_",
    "AWS_",
)
# Exact names outside those prefixes. The cloud profile file: the root Makefile points host
# targets at config/cloud.local.yaml, and a profile in the environment changes every storage check.
_SPECFLOW_ENV_NAMES = frozenset({"SPECFLOW_CONFIG_FILE"})


@pytest.fixture
def clean_specflow_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every specflow setting from the process environment."""

    for name in list(os.environ):
        if name.startswith(_SPECFLOW_ENV_PREFIXES) or name in _SPECFLOW_ENV_NAMES:
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def valid_env() -> dict[str, str]:
    """A fresh, valid local environment for every role (dummy values only)."""

    return {
        "APP_ENV": "local",
        "API_PORT": "8000",
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5433",
        "DATABASE_NAME": "specgraph",
        "DATABASE_USER": "app_user",
        "DATABASE_PASSWORD": "db-pass-9f1c",
        "DATABASE_AGENT_USER": "agent_user",
        "DATABASE_AGENT_PASSWORD": "agent-pass-7b2d",
        "AUTH_MODE": "dev",
        "OIDC_ISSUER": "specflow-dev-login",
        "AUTH_ALLOWED_EMAIL_DOMAINS": "example.com",
        "AUTH_DEV_SIGNING_KEY": "k" * 40,
        "STORAGE_BACKEND": "gcs",
        "STORAGE_BUCKET": "test-bucket",
        "STORAGE_PREFIX": "dev/local/",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "TEMPORAL_ADDRESS": "localhost:7233",
        "TEMPORAL_NAMESPACE": "specflow-local",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "neo-pass-4e8a",
        "AGENT_MODEL": "test/model",
        "LLM_GATEWAY_URL": "https://gateway.example.test/openai",
        "LLM_GATEWAY_API_KEY": "gateway-key-3c6e",
    }
