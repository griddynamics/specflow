"""Converter tests are offline: the Notion sandbox fixtures are read from disk, any socket use fails the test."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parents[3] / "src" / "notion" / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in a converter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def markdown() -> dict[str, str]:
    """Page id to the NFM of the sandbox pages."""

    return {page_id: raw["markdown"] for page_id, raw in load("markdown").items()}


@pytest.fixture(scope="session")
def rows() -> list[dict[str, Any]]:
    """The pages (database rows) of every query fixture."""

    return [row for page_rows in load("queries").values() for row in page_rows]
