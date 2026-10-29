"""Every test starts from a fresh scan target and configuration: the working directory, no
`--scan`, no `--config`."""

from __future__ import annotations

import pytest

from pystatic import config, files


@pytest.fixture(autouse=True)
def _fresh_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "_TARGET", files._Target())
    monkeypatch.setattr(config, "_STATE", config._State())
