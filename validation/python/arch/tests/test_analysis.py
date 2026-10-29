"""Self-tests of `archkit.analysis`: each function on synthetic positive and negative input."""

from __future__ import annotations

import json
from pathlib import Path

from archkit.analysis import snapshot_diff, uncovered_handler_classes

FIXTURES = Path(__file__).parent / "fixtures"


def test_uncovered_handler_classes() -> None:
    class AppError(Exception):
        pass

    class NotFoundError(AppError):
        pass

    class SocketOnlyError(Exception):
        pass

    handlers = [AppError, KeyError, SocketOnlyError, 500]
    assert uncovered_handler_classes(handlers, [NotFoundError], [SocketOnlyError]) == ["KeyError"]


def _snapshot(name: str) -> object:
    return json.loads((FIXTURES / "openapi_snapshot" / name).read_text(encoding="utf-8"))


def test_snapshot_diff_is_none_for_identical_documents() -> None:
    document = _snapshot("base.json")
    assert snapshot_diff(document, json.loads(json.dumps(document))) is None


def test_snapshot_diff_renders_a_readable_diff() -> None:
    diff = snapshot_diff(_snapshot("base.json"), _snapshot("changed.json"))
    assert diff is not None
    assert '-    "title": "Base",' in diff
    assert '+    "title": "Changed",' in diff
