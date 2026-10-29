"""Drive tests never touch the network: any socket connect fails the test."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in a Drive test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def google_samples(tmp_path: Path) -> Path:
    """Placeholder Google JSON (the converters' real samples come from another module)."""

    folder = tmp_path / "samples"
    folder.mkdir()
    for name in ("document", "presentation", "spreadsheet"):
        (folder / f"{name}.json").write_text(json.dumps({"placeholder": name}))
    return folder
