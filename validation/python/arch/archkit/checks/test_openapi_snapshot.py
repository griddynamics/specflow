"""The committed OpenAPI snapshot equals the running app's own document.

Parsed JSON is compared with `==`; a mismatch shows a unified diff of the canonical text.
Only `archkit export-openapi` (or the project's own exporter) writes the snapshot.

`[openapi_snapshot]`: `path` (the committed snapshot) and `regenerate` (the command shown
in the failure, default `archkit export-openapi --config archkit.toml`).
"""

from __future__ import annotations

import json
from typing import Any

from archkit.analysis import snapshot_diff
from archkit.config import Config
from archkit.support import section

RULE = "openapi_snapshot"


def test_the_committed_snapshot_equals_the_running_app(
    archkit_config: Config, archkit_app: Any
) -> None:
    table = section(archkit_config, RULE)
    path = archkit_config.resolve_path(table["path"])
    regenerate = table.get("regenerate", "archkit export-openapi --config archkit.toml")
    assert path.is_file(), f"{path} is missing (fix: {regenerate})"
    committed = json.loads(path.read_text(encoding="utf-8"))
    live = json.loads(json.dumps(archkit_app.openapi()))
    diff = snapshot_diff(
        committed, live, committed_label=f"{table['path']} (committed)", live_label="app (live)"
    )
    assert diff is None, f"{table['path']} is stale (fix: {regenerate}, then commit it)\n{diff}"
