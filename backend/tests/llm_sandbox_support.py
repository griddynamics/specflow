"""Offline-test fixture that needs the `llm` package (it imports `claude_agent_sdk`, an extra of
`parse`). `conftest.py` registers this plugin only when that SDK is installed, so a light
environment (the `rss-test` container) still collects the tests that do not need it."""

from __future__ import annotations

import pytest

import llm.sandbox as sandbox_module


@pytest.fixture(autouse=True)
def available_native_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep offline tests independent from optional host sandbox tooling."""

    monkeypatch.setattr(
        sandbox_module,
        "check_native_sandbox_available",
        lambda: None,
    )
