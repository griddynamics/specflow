"""Fixture: the deterministic-core scope (`**/src/core/**`). The determinism rules fire
here, and `kit.security.environment-read-outside-settings` leaves the core to them."""

from __future__ import annotations

import os


def read_cap(default: str) -> str | None:
    # ruleid: kit.determinism.no-environment-or-io
    # ok: kit.security.environment-read-outside-settings
    return os.getenv("MAX_DEPTH", default)
