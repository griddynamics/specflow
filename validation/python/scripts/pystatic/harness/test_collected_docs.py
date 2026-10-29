"""Self-test for `collected_docs`: anchor declarations and citation problems of collected
test modules. `ANCHOR` below is this module's own citation, which the helper reads."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from pystatic.harness import collected_docs

ANCHOR = "/README.md#layout"


def _module(name: str, path: Path, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_declared_anchors_reads_anchor_and_doc_anchors(tmp_path: Path) -> None:
    many = ["/b.md#y", "/c.md#z"]
    module = _module("m", tmp_path / "m.py", ANCHOR="/a.md#x", DOC_ANCHORS=many)
    assert collected_docs.declared_anchors(module) == {"/a.md#x", "/b.md#y", "/c.md#z"}
    bare = _module("n", tmp_path / "n.py", ANCHOR="no-hash")
    assert collected_docs.declared_anchors(bare) == set()


def test_citation_problems_reports_undeclared_modules_and_dead_anchors(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "test_doc_anchors.py"
    cited = _module("cited", tmp_path / "test_cited.py", ANCHOR="/README.md#layout")
    dead = _module("dead", tmp_path / "test_dead.py", ANCHOR="/README.md#no-such-heading")
    silent = _module("silent", tmp_path / "test_silent.py")
    outside = _module("outside", tmp_path.parent / "test_outside.py")
    itself = _module("itself", owner)
    items = [types.SimpleNamespace(module=m) for m in (cited, dead, silent, outside, itself)]
    monkeypatch.setattr(request.session, "items", items)

    undeclared, dead_anchors = collected_docs.citation_problems(request, owner.resolve())

    assert undeclared == ["silent"]
    assert dead_anchors == ["/README.md#no-such-heading"]
