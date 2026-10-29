"""Fixtures for semgrep/production.yml, named to match the config's own stem so
`semgrep --test` discovers it (`--test` ignores `paths:`, so `paths.include` itself is not
exercised here)."""

import os
import sys

from pydantic_settings import BaseSettings

# --- kit.production.test-justified-comment ---

# ruleid: kit.production.test-justified-comment
# The order holds across the test suite.
ORDERED = True

# ruleid: kit.production.test-justified-comment
# Keep the value the recorded provider exchanges were authored with.
TEMPERATURE = 0.3

# ruleid: kit.production.test-justified-comment
# A VCR recording replays this request.
REPLAYED = True

# ruleid: kit.production.test-justified-comment
# Only used in tests, so the default stays empty.
EMPTY = ""

# ruleid: kit.production.test-justified-comment
# The parsed-fixture model answers here.
ANSWERED = True

# ok: kit.production.test-justified-comment
# An idempotent replay answers the stored body.
STORED = True

# ok: kit.production.test-justified-comment
# The recording is request-scoped: its bytes never outlive the call.
SCOPED = True

# ok: kit.production.test-justified-comment
# The validator that tests the input is paired with it.
PAIRED = True

# --- kit.production.test-model-import ---

# ruleid: kit.production.test-model-import
from pydantic_ai.models.test import TestModel

# ruleid: kit.production.test-model-import
from pydantic_ai.models.function import FunctionModel

# ok: kit.production.test-model-import
from pydantic_ai.models.google import GoogleModel

# --- kit.production.test-runner-branch ---


def under_runner() -> bool:
    # ruleid: kit.production.test-runner-branch
    return "PYTEST_VERSION" in os.environ


def runner_value() -> str | None:
    # ruleid: kit.production.test-runner-branch
    return os.environ.get("PYTEST_CURRENT_TEST")


def runner_loaded() -> bool:
    # ruleid: kit.production.test-runner-branch
    return "pytest" in sys.modules


def configured_home() -> str | None:
    # ok: kit.production.test-runner-branch
    return os.environ.get("HOME")


# --- kit.production.fixture-setting ---


class Settings(BaseSettings):
    # ruleid: kit.production.fixture-setting
    model_fixture_dir: str | None = None
    # ruleid: kit.production.fixture-setting
    export_worker_fixture_dir: str | None = None
    # ruleid: kit.production.fixture-setting
    precomputed_seed_path: str | None = None
    # ruleid: kit.production.fixture-setting
    replay_dir: str | None = None
    # ok: kit.production.fixture-setting
    order_command_replay_limit: int = 32
    # ok: kit.production.fixture-setting
    transcription_max_recording_seconds: int = 60


MODELS = (TestModel, FunctionModel, GoogleModel)
