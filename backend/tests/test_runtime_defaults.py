"""The committed runtime-defaults file matches the defaults the code declares (pystatic runtime-defaults)."""

from __future__ import annotations

from runtime_defaults_support import DEFAULTS_FILE, RUNTIME_DEFAULTS, declared_defaults, render


def test_the_runtime_defaults_file_is_generated_from_the_code() -> None:
    assert DEFAULTS_FILE.read_text(encoding="utf-8") == render(declared_defaults()), (
        "tests/fixtures/runtime-defaults.json is stale: run `uv run python tests/runtime_defaults_support.py`"
    )


def test_the_runtime_defaults_cover_the_process_settings_and_build_options() -> None:
    assert {"API_PORT", "WORKER_HEALTH_PORT", "KNOWN_TERMS_LIMIT", "CATALOG_LIMIT"} <= RUNTIME_DEFAULTS.keys()
